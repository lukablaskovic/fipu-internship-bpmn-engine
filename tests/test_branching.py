"""Token routing checks with isolated model files and in-memory event capture."""
import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
env = ModuleType("env")
env.DS = {}
env.SYSTEM_VARS = {}
database = ModuleType("db_connector")


def load_module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


with patch.dict(sys.modules, {"env": env, "db_connector": database}):
    types = load_module("branch_types", "bpmn_types.py")
    with patch.dict(sys.modules, {"bpmn_types": types}):
        engine = load_module("branch_engine", "bpmn_model.py")


class BranchingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.records = []
        database.add_event = lambda **event: self.records.append(deepcopy(event))
        self.runner = None

    async def asyncTearDown(self):
        if self.runner and not self.runner.done():
            self.runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await self.runner

    async def start(self, content, variables=None):
        path = Path(self.temp.name) / "branching.bpmn"
        path.write_text(f'''<bpmn:definitions xmlns:bpmn="{types.NS['bpmn']}">
          <bpmn:process id="branching" name="Synthetic branching" isExecutable="true">
            {content}
          </bpmn:process></bpmn:definitions>''')
        model = engine.BpmnModel(str(path))
        self.instance = await model.create_instance("synthetic", variables or {})
        self.runner = asyncio.create_task(self.instance.run())
        await asyncio.sleep(0)

    async def test_reloading_source_preserves_live_graph_and_uses_current_file_for_new_instances(self):
        path = Path(self.temp.name) / "source.bpmn"
        xml = f'<bpmn:definitions xmlns:bpmn="{types.NS["bpmn"]}"><bpmn:process id="p" name="Before"><bpmn:startEvent id="start"/></bpmn:process></bpmn:definitions>'
        path.write_text(xml)
        models = {str(path): engine.BpmnModel(str(path))}
        old = await models[str(path)].create_instance("old-source", {})
        path.write_text(xml.replace('name="Before"', 'name="After"'))
        current = engine.reload_model(models, str(path))
        new = await current.create_instance("new-source", {})
        self.assertEqual(old.model.main_process.name, "Before")
        self.assertEqual(new.model.main_process.name, "After")
        self.assertIs(current.instances[old._id], old)
        self.assertIs(engine.get_model_for_instance(old._id), old.model)
        path.write_text("invalid XML")
        with self.assertRaises(Exception):
            engine.reload_model(models, str(path))
        self.assertIs(models[str(path)], current)

    async def test_end_event_preserves_other_parallel_tokens_and_checkpoint(self):
        await self.start('''
          <bpmn:startEvent id="start"/>
          <bpmn:parallelGateway id="split"><bpmn:incoming>begin</bpmn:incoming></bpmn:parallelGateway>
          <bpmn:endEvent id="notification_done"/>
          <bpmn:userTask id="diary"/>
          <bpmn:endEvent id="internship_done"/>
          <bpmn:sequenceFlow id="begin" sourceRef="start" targetRef="split"/>
          <bpmn:sequenceFlow id="notification" sourceRef="split" targetRef="notification_done"/>
          <bpmn:sequenceFlow id="student" sourceRef="split" targetRef="diary"/>
          <bpmn:sequenceFlow id="finish" sourceRef="diary" targetRef="internship_done"/>
        ''')
        self.assertFalse(self.runner.done())
        self.assertEqual(self.instance.state, "running")
        self.assertEqual([task._id for task in self.instance.pending], ["diary"])
        end = next(event for event in self.records if event["activity_id"] == "notification_done")
        self.assertEqual(end["pending"], ["diary"])
        # The persisted end-event checkpoint must restore the unfinished branch.
        restored = await self.instance.model.create_instance("restored", {})
        await restored.run_from_log(self.records)
        self.assertEqual([task._id for task in restored.pending], ["diary"])
        await self.instance.in_queue.put(engine.UserFormMessage("diary", {}))
        await asyncio.wait_for(self.runner, 1)
        self.assertEqual(self.instance.state, "finished")
        self.assertEqual(self.records[-1]["activity_id"], "internship_done")
        self.assertEqual(self.records[-1]["pending"], [])

    async def test_exclusive_gateway_selects_first_match_or_default_only(self):
        content = '''
          <bpmn:startEvent id="start"/>
          <bpmn:exclusiveGateway id="decision" default="fallback"/>
          <bpmn:userTask id="first"/><bpmn:userTask id="second"/><bpmn:userTask id="default"/>
          <bpmn:sequenceFlow id="begin" sourceRef="start" targetRef="decision"/>
          <bpmn:sequenceFlow id="fallback" sourceRef="decision" targetRef="default"/>
          <bpmn:sequenceFlow id="match_first" sourceRef="decision" targetRef="first"><bpmn:conditionExpression>approved:yes</bpmn:conditionExpression></bpmn:sequenceFlow>
          <bpmn:sequenceFlow id="match_second" sourceRef="decision" targetRef="second"><bpmn:conditionExpression>approved:yes</bpmn:conditionExpression></bpmn:sequenceFlow>
        '''
        for approved, expected in (("yes", "first"), ("no", "default")):
            with self.subTest(approved=approved):
                await self.start(content, {"approved": approved})
                self.assertFalse(self.runner.done())
                self.assertEqual([task._id for task in self.instance.pending], [expected])
                self.assertEqual(self.records[-1]["activity_id"], "decision")
                self.assertEqual(self.records[-1]["pending"], [expected])
                self.runner.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await self.runner


if __name__ == "__main__":
    unittest.main()
