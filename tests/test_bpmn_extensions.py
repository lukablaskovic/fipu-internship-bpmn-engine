"""FIPU extension compatibility checks; no database or connector requests."""
import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
env = ModuleType("env")
env.SYSTEM_VARS = {"_frontend_url": "http://localhost:5173"}
env.DS = {
    name: {"type": "http-connector", "url": f"http://{name}.local.test"}
    for name in ("baserow", "sendgrid", "pdf")
}
spec = importlib.util.spec_from_file_location("profile_types", ROOT / "bpmn_types.py")
types = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"env": env}):
    spec.loader.exec_module(types)


def parse_objects(xml):
    root = ET.fromstring(xml)
    result = {}
    for tag, object_type in types.BPMN_MAPPINGS.items():
        for element in root.findall(f".//{tag}", types.NS):
            obj = object_type()
            obj.parse(element)
            result[obj._id] = obj.__dict__
    return result


def service_xml(body="", connector_id="baserow", method="POST", url="/api/prijavnica"):
    return f'''<bpmn:serviceTask xmlns:bpmn="{types.NS['bpmn']}"
        xmlns:fipu="{types.NS['fipu']}" id="save">
      <bpmn:extensionElements>
        <fipu:connector>
          <fipu:connectorId>{connector_id}</fipu:connectorId>
          <fipu:inputOutput>
            <fipu:inputParameter name="method">{method}</fipu:inputParameter>
            <fipu:inputParameter name="url">{url}</fipu:inputParameter>
          </fipu:inputOutput>
        </fipu:connector>{body}
      </bpmn:extensionElements>
    </bpmn:serviceTask>'''


def parse_service(xml):
    task = types.ServiceTask()
    task.parse(ET.fromstring(xml))
    return task


class ExtensionTests(unittest.TestCase):
    def test_shipped_models_have_identical_legacy_and_fipu_contracts(self):
        for name in ("A", "B", "edited"):
            with self.subTest(model=name):
                xml = (ROOT / "models" / f"strucna_praksa_{name}.bpmn").read_text()
                self.assertNotIn(types.NS["camunda"], xml)
                current = parse_objects(xml)
                self.assertEqual(current, parse_objects(xml.replace(
                    types.NS["fipu"], types.NS["camunda"])))
                self.assertEqual(current, parse_objects(xml.replace("<fipu:", "<execution:")
                                                        .replace("</fipu:", "</execution:")
                                                        .replace(" fipu:", " execution:")
                                                        .replace("xmlns:fipu", "xmlns:execution")))
                self.assertIn("ispunjavanje_prijavnice_student", current)
                self.assertIn("detaljan_opis_zadatka", current[
                    "ispunjavanje_prijavnice_student"]["form_fields"])
                self.assertIn("predavanje_dnevnika_student", current)

    def test_process_name_is_optional_in_bpmn(self):
        process = types.Process()
        process.parse(ET.fromstring(
            f'<bpmn:process xmlns:bpmn="{types.NS["bpmn"]}" id="unnamed_process"/>'))
        self.assertEqual(process.name, "unnamed_process")

    def test_mixed_extension_profiles_are_rejected(self):
        xml = service_xml().replace("<fipu:connectorId>",
            f'<legacy:connectorId xmlns:legacy="{types.NS["camunda"]}">')
        xml = xml.replace("</fipu:connectorId>", "</legacy:connectorId>")
        with self.assertRaisesRegex(ValueError, "Mixed FIPU and Camunda"):
            parse_service(xml)

    def test_invalid_connector_configuration_is_rejected_before_execution(self):
        variants = [
            service_xml(connector_id="unregistered"),
            service_xml(connector_id=""),
            service_xml(method="DELETE"),
            service_xml(method=""),
            service_xml(url="https://remote.test"),
            service_xml(url="//remote.test"),
            service_xml(url="/api/../secret"),
            service_xml(url="/api/%2e%2e/secret"),
            service_xml(url=""),
            service_xml().replace('<fipu:connector>', '').replace('</fipu:connector>', ''),
            service_xml('<fipu:connector/>'),
            service_xml().replace('</fipu:inputOutput>',
                '<fipu:inputParameter name="url_parameter">wrong</fipu:inputParameter></fipu:inputOutput>'),
            service_xml('<fipu:inputOutput><fipu:inputParameter name="script">'
                        '<fipu:script>ignored()</fipu:script></fipu:inputParameter></fipu:inputOutput>'),
        ]
        for xml in variants:
            with self.subTest(xml=xml), self.assertRaises(ValueError):
                parse_service(xml)

    def test_form_properties_and_validation_accept_any_prefix(self):
        xml = f'''<b:userTask xmlns:b="{types.NS['bpmn']}" xmlns:e="{types.NS['fipu']}" id="application">
          <b:documentation>Application</b:documentation><b:extensionElements><e:formData>
            <e:formField id="description" type="string" label="Description">
              <e:properties><e:property id="url" value="${{_frontend_url}}"/></e:properties>
              <e:validation><e:constraint name="required" config="true"/></e:validation>
            </e:formField>
          </e:formData></b:extensionElements></b:userTask>'''
        task = types.UserTask()
        task.parse(ET.fromstring(xml))
        self.assertEqual(task.form_fields["description"], {
            "type": "string", "label": "Description",
            "properties": {"url": "http://localhost:5173"},
            "validation": {"required": "true"},
        })
        state = {}
        task.run(state, {"description": "Synthetic", "unexpected": "ignored"})
        self.assertEqual(state, {"description": "Synthetic"})

    def test_connector_payload_and_outputs_keep_the_existing_wire_contract(self):
        body = '''<fipu:inputOutput>
          <fipu:inputParameter name="name">${student_name}</fipu:inputParameter>
          <fipu:inputParameter name="id_instance"/>
          <fipu:inputParameter name="choices"><fipu:list><fipu:value>${choice}</fipu:value><fipu:value/></fipu:list></fipu:inputParameter>
          <fipu:inputParameter name="details"><fipu:map><fipu:entry key="name">${student_name}</fipu:entry><fipu:entry key="empty"/></fipu:map></fipu:inputParameter>
          <fipu:outputParameter name="prijavnica_id"/>
        </fipu:inputOutput>'''
        task = parse_service(service_xml(body))
        original_inputs = deepcopy(task.input_variables)
        response = Mock(status_code=201)
        response.json.return_value = {"prijavnica_id": 12, "ignored": "value"}
        state = {"student_name": "Synthetic", "choice": "Test"}
        with patch.object(types.requests, "post", return_value=response) as request:
            asyncio.run(task.run(state, "synthetic-instance"))
        request.assert_called_once_with(
            "http://baserow.local.test/api/prijavnica", params={},
            json={"name": "Synthetic", "id_instance": "synthetic-instance",
                  "choices": ["Test", ""], "details": {"name": "Synthetic", "empty": ""},
                  "_frontend_url": "http://localhost:5173"},
        )
        self.assertEqual(state["prijavnica_id"], 12)
        self.assertNotIn("ignored", state)
        self.assertEqual(task.input_variables, original_inputs)


if __name__ == "__main__":
    unittest.main()
