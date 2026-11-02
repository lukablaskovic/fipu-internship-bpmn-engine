from types import SimpleNamespace
import xml.etree.ElementTree as ET
from bpmn_types import *
from pprint import pprint
from copy import deepcopy
from collections import defaultdict, deque
from functools import partial
import time
import asyncio
import requests
import db_connector
from datetime import datetime
import os
from uuid import uuid4
import env

instance_models = {}


def get_model_for_instance(iid):
    return instance_models.get(iid, None)


def forget_instance(iid):
    model = instance_models.pop(iid, None)
    if model:
        model.instances.pop(iid, None)


def reload_model(models, name):
    current = BpmnModel(name)
    # Existing instances keep their graph; new instances use the current source file.
    current.instances = models[name].instances
    models[name] = current
    return current


class UserFormMessage:
    def __init__(self, task_id, form_data=None, submission_id=None):
        self.task_id = task_id
        self.form_data = form_data if form_data is not None else {}
        self.submission_id = submission_id


class BpmnModel:
    def __init__(self, model_path, xml=None):
        self.pending = []
        self.elements = {}
        self.flow = defaultdict(list)
        self.instances = {}
        self.process_elements = {}
        self.process_pending = defaultdict(list)
        self.main_collaboration_process = None
        self.model_path = model_path
        self.subprocesses = {}
        self.main_process = SimpleNamespace()

        if xml is None:
            with open(os.path.join("models", self.model_path), encoding="utf-8") as source:
                xml = source.read()
        self.xml = xml
        model_root = ET.fromstring(xml)
        self.version = int(model_root.get("processVersion", "1"))
        processes = model_root.findall("bpmn:process", NS)
        for process in processes:
            p = BPMN_MAPPINGS["bpmn:process"]()
            p.parse(process)
            self.process_elements[p._id] = {}
            # Check for Collaboration
            if len(processes) > 1 and p.is_main_in_collaboration:
                self.main_collaboration_process = p._id
                self.main_process.name = p.name
                self.main_process.id = p._id
            else:
                self.main_process.name = p.name
                self.main_process.id = p._id
            # Parse all elements in the process
            for tag, _type in BPMN_MAPPINGS.items():
                for e in process.findall(f"{tag}", NS):
                    t = TimerCatchEvent() if _type is IntermediateCatchEvent and e.find("bpmn:timerEventDefinition", NS) is not None else _type()
                    t.parse(e)
                    if isinstance(t, CallActivity):
                        self.subprocesses[t.called_element] = t.deployment
                    if isinstance(t, SequenceFlow):
                        self.flow[t.source].append(t)
                    if isinstance(t, ExclusiveGateway):
                        if t.default:
                            self.elements[t.default].default = True
                    if isinstance(t, StartEvent):
                        self.pending.append(t)
                        self.process_pending[p._id].append(t)
                    self.elements[t._id] = t
                    self.process_elements[p._id][t._id] = t
        # Check if there is single deployement subprocess
        for k, v in self.subprocesses.items():
            if v:
                self.handle_deployment_subprocesses()
                break

    def to_json(self):
        return {
            "model_path": self.model_path,
            "version": self.version,
            "main_process": self.main_process.__dict__,
            "tasks": [
                x.to_json() for x in self.elements.values() if isinstance(x, UserTask)
            ],
            "instances": [i._id for i in self.instances.values()],
        }

    async def create_instance(self, _id, variables, process=None):
        queue = asyncio.Queue()
        if not process:
            if self.main_collaboration_process:
                # If Collaboration diagram
                process = self.main_collaboration_process
            else:
                # If Process diagram
                process = list(self.process_elements)[0]
        instance = BpmnInstance(
            _id, model=self, variables=variables, in_queue=queue, process=process
        )
        self.instances[_id] = instance
        return instance

    # Takes model_path needed for deployed subprocess
    def handle_deployment_subprocesses(self):
        models_directory = self.model_path.split("/")[:-1]
        models_directory = "/".join(models_directory) + "/"

        other_models_list = []

        for m in os.listdir(models_directory):
            if models_directory + m == self.model_path:
                continue
            other_model = BpmnModel(models_directory + m)
            other_models_list.append(other_model)
        for other_model in other_models_list:
            for subprocess_key in self.subprocesses.keys():
                for process_key in other_model.process_elements.keys():
                    if subprocess_key == process_key:
                        self.subprocesses[subprocess_key] = other_model.model_path


class BpmnInstance:
    def __init__(self, _id, model, variables, in_queue, process):
        instance_models[_id] = model
        self._id = _id
        self.model = model
        self.version = model.version
        self.variables = deepcopy(variables)
        self.in_queue = in_queue
        self.incident = None
        self.retry_attempt = None
        self.submission_lock = asyncio.Lock()
        self.timer_due = {}
        self.runner = None
        self.state = "initialized"
        self.pending = deepcopy(self.model.process_pending[process])
        self.process = process
        self.completed_activity_ids = set()
        self.triggered_events = []
        self.active_event = None

    def to_json(self):
        return {
            "id": self._id,
            "variables": self.variables,
            "completed_activity_ids": sorted(self.completed_activity_ids),
            "state": self.state,
            "model": {**self.model.to_json(), "version": self.version},
            "pending": [x._id for x in self.pending if not isinstance(x, (IntermediateCatchEvent, TimerCatchEvent))],
            "pending_timers": self.timer_due.copy(),
            "available_events": [x._id for x in self.pending if isinstance(x, IntermediateCatchEvent)],
            "triggered_events": self.triggered_events,
            "active_event": self.active_event,
            "env": env.SYSTEM_VARS,
        }

    @classmethod
    def check_condition(cls, state, condition, log):
        log(f"\t- checking condition {condition}... ")
        ok = False
        if condition:
            key = condition.partition(":")[0]
            value = condition.partition(":")[2]
            if key in state and state[key] == value:
                ok = True
        log("\t  DONE: Result is", ok)
        return ok

    async def run_from_log(self, log):
        self.completed_activity_ids.clear()
        self.triggered_events = []
        self.active_event = None
        for l in log:
            if l["activity_id"] == "@rollback":
                self.apply_rollback(l)
            elif l.get("activity_id") in self.model.elements:
                pending_elements_list = []
                for p in l.get("pending"):
                    pending_elements_list.append(self.model.elements[p])
                self.pending = pending_elements_list
                self.timer_due = {key: due for key, due in self.timer_due.items() if key in l["pending"] and key != l["activity_id"]}
                for timer in self.pending:
                    if isinstance(timer, TimerCatchEvent) and timer._id not in self.timer_due:
                        stamp = l["timestamp"]
                        if isinstance(stamp, str):
                            stamp = datetime.fromisoformat(stamp)
                        self.timer_due[timer._id] = stamp.timestamp() + timer.seconds
                self.variables.update(l.get("activity_variables"))
                activity = self.model.elements[l["activity_id"]]
                # Termination also clears pending on waiting gateways logged before the final event.
                if isinstance(activity, ParallelGateway) and not self.pending and l is not log[-1]:
                    continue
                # A waiting checkpoint can name a still-pending node; a direct self-loop reopens it.
                if activity._id not in l["pending"] or (
                    not isinstance(activity, ParallelGateway)
                    and any(flow.target == activity._id for flow in self.model.flow.get(activity._id, ()))
                ):
                    self.completed_activity_ids.add(activity._id)
                if isinstance(activity, IntermediateCatchEvent):
                    self.triggered_events.append(activity._id)
                    self.active_event = activity._id
                if not self.pending:
                    self.state = activity.outcome if isinstance(activity, EndEvent) else "finished"
        # Existing active checkpoints gain newly modelled start-branch subscriptions.
        if self.pending:
            pending_ids = {task._id for task in self.pending}
            branches = list(self.model.process_pending[self.process])
            visited = set()
            while branches:
                node = branches.pop()
                if node._id in visited:
                    continue
                visited.add(node._id)
                for flow in self.model.flow[node._id]:
                    event = self.model.elements[flow.target]
                    if isinstance(event, ParallelGateway) and event.incoming <= 1:
                        branches.append(event)
                    elif isinstance(event, IntermediateCatchEvent) and event._id == "odustajanje_od_prakse" and event._id not in pending_ids and event._id not in self.triggered_events:
                        self.pending.append(deepcopy(event))
                        pending_ids.add(event._id)
        return self

    def apply_rollback(self, event):
        self.pending = [deepcopy(self.model.elements[key]) for key in event["pending"]]
        self.timer_due = {key: due for key, due in self.timer_due.items() if key in event["pending"]}
        self.variables["_evaluation_generation"] = event["activity_variables"]["generation"]
        self.active_event = None
        self.incident = None

    async def run_subprocess(self, process_id):
        new_subproces_instance_id = str(uuid4())
        if not self.model.subprocesses[process_id]:
            new_subprocess_instance = await self.model.create_instance(
                new_subproces_instance_id, {}, process_id
            )
            finished_subprocess = await new_subprocess_instance.run()
        else:
            subprocess_model = BpmnModel(self.model.subprocesses[process_id])
            new_subproces_instance = await subprocess_model.create_instance(
                new_subproces_instance_id, {}, process_id
            )
            finished_subprocess = await new_subproces_instance.run()
        return True

    async def run(self):
        try:
            return await self._run()
        except Exception:
            self.state = "failed"
            self.incident = self.incident or db_connector.open_attempt(self._id)
            raise

    async def _run(self):
        print("Running instance", self._id)
        self.state = "running"
        outcome = "finished"
        _id = self._id
        prefix = f"\t[{_id}]"
        log = partial(print, prefix)  # if _id == "2" else lambda *x: x

        in_queue = self.in_queue
        # Take only elements of running process
        elements = deepcopy(self.model.process_elements[self.process])
        flow = deepcopy(self.model.flow)
        queue = deque()

        while len(self.pending) > 0:
            # process incoming messages
            if not in_queue.empty():
                queue.append(in_queue.get_nowait())
            # print("Check", _id, id(queue), id(in_queue))

            can_continue = False

            message = queue[0] if queue else None
            if message:
                log("--> msg in:", message and message.task_id)

            # Helper current dictionary
            current_and_variables_dict = {}

            attempt_id = None
            for idx, current in enumerate(self.pending):
                # Helper variables dict
                before_variables = deepcopy(self.variables)

                if isinstance(current, TimerCatchEvent):
                    if time.time() < self.timer_due[current._id]:
                        continue
                    del self.timer_due[current._id]
                    can_continue = True
                elif isinstance(current, IntermediateCatchEvent):
                    if not (message and message.task_id == current._id):
                        continue
                    queue.popleft()
                    self.active_event = current._id
                    can_continue = True
                elif isinstance(current, UserTask):
                    if not (message and isinstance(message, UserFormMessage)
                            and message.task_id == current._id):
                        continue
                    queue.popleft()
                    log("DOING:", current)
                    can_continue = current.run(self.variables, message.form_data)
                elif isinstance(current, ServiceTask):
                    log("DOING:", current)
                    attempt = self.retry_attempt or db_connector.start_attempt(
                        _id, current._id, legacy_xml=self.model.xml if self.version is None else None
                    )
                    self.retry_attempt = None
                    attempt_id, current.operation_key = attempt
                    try:
                        can_continue = await current.run(self.variables, _id)
                    except Exception as exc:
                        reason = "Provjerite dostupnost i konfiguraciju servisa."
                        if isinstance(exc, requests.HTTPError) and exc.response is not None:
                            reason = f"Servis je vratio HTTP {exc.response.status_code}."
                        self.variables = before_variables
                        self.incident = db_connector.fail_attempt(attempt_id, reason)
                        raise RuntimeError(reason) from None
                elif isinstance(current, CallActivity):
                    # TODO implement Variables tab CallActivity
                    log("DOING:", current)
                    can_continue = await self.run_subprocess(current.called_element)
                else:
                    if isinstance(current, Task):
                        log("DOING:", current)
                    can_continue = current.run()

                current_and_variables_dict[current._id] = {
                    k: value for k, value in self.variables.items()
                    if isinstance(current, StartEvent)
                    or k not in before_variables or value != before_variables[k]
                }

                if can_continue:
                    del self.pending[idx]
                    if isinstance(current, EndEvent) and current.terminate:
                        self.pending = []
                        self.timer_due.clear()
                        outcome = current.outcome
                    break

            default = current.default if isinstance(current, ExclusiveGateway) else None

            if can_continue:
                next_tasks = []
                if current._id in flow:
                    default_fallback = None
                    for sequence in flow[current._id]:
                        if sequence._id == default:
                            default_fallback = elements[sequence.target]
                            continue

                        if sequence.condition:
                            if self.check_condition(
                                self.variables, sequence.condition, log
                            ):
                                next_tasks.append(elements[sequence.target])
                        else:
                            next_tasks.append(elements[sequence.target])
                        if isinstance(current, ExclusiveGateway) and next_tasks:
                            break

                    if not next_tasks and default_fallback:
                        log("\t- going down default path...")
                        next_tasks.append(default_fallback)

                for next_task in next_tasks:
                    if next_task not in self.pending:
                        self.pending.append(next_task)
                        if isinstance(next_task, TimerCatchEvent):
                            self.timer_due[next_task._id] = time.time() + next_task.seconds
                        # log("-----> Adding", next_task)
                    # log("n", next_task)
                    if isinstance(next_task, ParallelGateway):
                        next_task.add_token()
            # Insert finished events into DB
            for c in current_and_variables_dict:
                # Add each current into DB
                db_connector.add_event(
                    model_name=self.model.model_path,
                    model_xml=self.model.xml,
                    instance_id=self._id,
                    activity_id=c,
                    attempt_id=attempt_id if c == current._id else None,
                    timestamp=datetime.now(),
                    pending=[pending._id for pending in self.pending],
                    activity_variables=current_and_variables_dict[c],
                    submission_id=(
                        message.submission_id
                        if message and message.task_id == c else None
                    ),
                )

            if can_continue:
                self.completed_activity_ids.add(current._id)
                if isinstance(current, IntermediateCatchEvent):
                    self.triggered_events.append(current._id)

            if not can_continue:
                self.active_event = None
                log("Waiting for user...", self.pending)
                if queue:
                    if queue[0].submission_id is not None:
                        raise ValueError("Queued submission does not match a pending task")
                    queue.popleft()
                timeout = max(0, min(self.timer_due.values()) - time.time()) if self.timer_due else None
                try:
                    queue.append(await asyncio.wait_for(in_queue.get(), timeout))
                except asyncio.TimeoutError:
                    pass

        log("DONE")
        self.state = outcome
        self.pending = []
        return self.variables
