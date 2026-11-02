import requests
import os
import env
from urllib.parse import unquote, urlsplit
from utils.common import parse_expression
from utils import access

NS = {
    "bpmn": "http://www.omg.org/spec/BPMN/20100524/MODEL",
    "fipu": "urn:fipu:bpmn:1",
    "camunda": "http://camunda.org/schema/1.0/bpmn",
}


def extension_namespaces(element):
    for node in element.iter():
        if any(name.startswith("{" + NS["camunda"] + "}") for name in (node.tag, *node.attrib)):
            raise ValueError("Unsupported Camunda execution extension")
    return {**NS, "ext": NS["fipu"]}


BPMN_MAPPINGS = {}


def bpmn_tag(tag):
    def wrap(object):
        object.tag = tag
        BPMN_MAPPINGS[tag] = object
        return object

    return wrap


class BpmnObject(object):
    def __repr__(self):
        return f"{type(self).__name__}({self.name or self._id})"

    def to_json(self):
        return {
            "_id": self._id,
            "name": self.name,
        }

    def parse(self, element):
        self._id = element.attrib["id"]
        self.name = element.attrib["name"] if "name" in element.attrib else None

    def run(self):
        return True


@bpmn_tag("bpmn:process")
class Process(BpmnObject):
    def __init__(self):
        self.is_main_in_collaboration = None
        self.name = None

    def parse(self, element):
        super(Process, self).parse(element)
        # Extensions should exists only if it's Collaboration diagram
        self.name = element.get("name") or self._id
        ns = extension_namespaces(element)
        ext = element.find("bpmn:extensionElements", NS)
        if ext is not None:
            for p in ext.findall(".//ext:property", ns):
                # Find property is_main
                if p.attrib["name"] == "is_main" and p.attrib["value"] == "True":
                    self.is_main_in_collaboration = True


@bpmn_tag("bpmn:sequenceFlow")
class SequenceFlow(BpmnObject):
    def __init__(self):
        self.source = None
        self.target = None
        self.condition = None

    def parse(self, element):
        super(SequenceFlow, self).parse(element)
        self.source = element.attrib["sourceRef"]
        self.target = element.attrib["targetRef"]
        for c in element.findall("bpmn:conditionExpression", NS):
            self.condition = c.text

    def __repr__(self):
        condition = f" w. {len(self.condition)} con. " if self.condition else ""
        return f"{type(self).__name__}({self._id}): {self.source} -> {self.target}{condition}"

    pass


@bpmn_tag("bpmn:task")
class Task(BpmnObject):
    def parse(self, element):
        super(Task, self).parse(element)

    def get_info(self):
        return {"type": self.tag}


@bpmn_tag("bpmn:manualTask")
class ManualTask(Task):
    pass


@bpmn_tag("bpmn:userTask")
class UserTask(Task):
    def __init__(self):
        self.form_fields = {}
        self.documentation = ""

    def parse(self, element):
        super(UserTask, self).parse(element)
        ns = extension_namespaces(element)
        for f in element.findall(".//ext:formField", ns):
            form_field_properties_dict = {}
            form_field_validations_dict = {}

            self.form_fields[f.attrib["id"]] = {}
            self.form_fields[f.attrib["id"]]["type"] = f.attrib["type"]
            if "label" in f.attrib:
                self.form_fields[f.attrib["id"]]["label"] = f.attrib["label"]
            else:
                self.form_fields[f.attrib["id"]]["label"] = ""

            for p in f.findall(".//ext:property", ns):
                form_field_properties_dict[p.attrib["id"]] = parse_expression(
                    p.attrib["value"], env.SYSTEM_VARS | env.DS
                )

            for v in f.findall(".//ext:constraint", ns):
                form_field_validations_dict[v.attrib["name"]] = v.attrib["config"]

            self.form_fields[f.attrib["id"]]["validation"] = form_field_validations_dict
            self.form_fields[f.attrib["id"]]["properties"] = form_field_properties_dict

        for d in element.findall(".//bpmn:documentation", NS):
            self.documentation = d.text

    def run(self, state, user_input):
        for k, v in user_input.items():
            if k in self.form_fields:
                state[k] = v
        return True

    def get_info(self):
        info = super(UserTask, self).get_info()
        return {
            **info,
            "form_fields": self.form_fields,
            "documentation": self.documentation,
        }


@bpmn_tag("bpmn:serviceTask")
class ServiceTask(Task):
    def __init__(self):
        self.properties_fields = {}
        self.input_variables = {}
        self.output_variables = {}
        self.connector_fields = {
            "connector_id": "",
            "input_variables": {},
            "output_variables": {},
        }

    def parse(self, element):
        super(ServiceTask, self).parse(element)

        ns = extension_namespaces(element)
        connectors = []
        for ee in element.findall("bpmn:extensionElements", NS):
            self._parse_input_output_variables(
                ee, self.input_variables, self.output_variables, ns
            )
            connectors.extend(ee.findall("ext:connector", ns))
        if len(connectors) != 1:
            raise ValueError(f"Task {self._id} must define exactly one connector")
        connector = connectors[0]
        self._parse_input_output_variables(
            connector,
            self.connector_fields["input_variables"],
            self.connector_fields["output_variables"],
            ns,
        )
        identifiers = connector.findall("ext:connectorId", ns)
        if len(identifiers) != 1 or identifiers[0].text not in env.DS:
            raise ValueError(f"Task {self._id} has an unknown or missing connector ID")
        self.datasource_id = identifiers[0].text
        datasource = env.DS[self.datasource_id]
        if datasource["type"] != "http-connector":
            raise ValueError(f"Task {self._id} uses an unsupported connector type")
        inputs = self.connector_fields["input_variables"]
        if inputs.get("method") not in ("GET", "POST", "PATCH"):
            raise ValueError(f"Task {self._id} requires GET, POST, or PATCH")
        endpoint = inputs.get("url")
        if (
            not isinstance(endpoint, str)
            or not endpoint.startswith("/")
            or endpoint.startswith("//")
            or ".." in unquote(urlsplit(endpoint).path).split("/")
        ):
            raise ValueError(f"Task {self._id} requires a connector-relative URL path without traversal")
        if "url_parameter" in inputs and not isinstance(inputs["url_parameter"], dict):
            raise ValueError(f"Task {self._id} requires a map for URL parameters")
        self.connector_fields["connector_id"] = datasource["type"]
        inputs["base_url"] = datasource["url"]

    def _parse_input_output_variables(self, element, input_dict, output_dict, ns):
        for io in element.findall("ext:inputOutput", ns):
            for inparam in io.findall("ext:inputParameter", ns):
                self._parse_input_output_parameters(inparam, input_dict, ns)
            for outparam in io.findall("ext:outputParameter", ns):
                self._parse_input_output_parameters(outparam, output_dict, ns)

    def _parse_input_output_parameters(self, element, dictionary, ns):
        values = element.find("ext:list", ns)
        entries = element.find("ext:map", ns)
        if values is not None:
            dictionary[element.attrib["name"]] = [value.text or "" for value in values]
        elif entries is not None:
            dictionary[element.attrib["name"]] = {
                entry.attrib["key"]: entry.text or "" for entry in entries
            }
        elif element.find("ext:script", ns) is not None:
            raise ValueError(f"Task {self._id} uses unsupported script parameters")
        else:
            dictionary[element.attrib["name"]] = element.text or ""

    async def run_connector(self, variables, instance_id):
        # Check for URL parameters
        parameters = {}
        if self.connector_fields["input_variables"].get("url_parameter"):
            for key, value in self.connector_fields["input_variables"][
                "url_parameter"
            ].items():
                # Parse expression and add to parameters
                parameters[key] = parse_expression(value, variables)

        # JSON data for API
        data = {}
        for key, value in self.input_variables.items():
            # Parse expression if it exists
            if isinstance(value, str):
                value = parse_expression(value, variables)
            elif isinstance(value, list):
                value = [parse_expression(v, variables) for v in value]
            elif isinstance(value, dict):
                value = {k: parse_expression(v, variables) for k, v in value.items()}
            # Special case for instance id
            if key == "id_instance":
                value = instance_id
            # Add parsed value to data
            data[key] = value
        # system vars
        data = {**data, **env.SYSTEM_VARS}

        base_url = self.connector_fields["input_variables"].get("base_url", "")
        endpoint = self.connector_fields["input_variables"]["url"].lstrip("/")
        url = f"{base_url}/{endpoint}"

        # Check method and make request
        if method := self.connector_fields["input_variables"].get("method"):
            if method == "POST":
                call_function = requests.post
            elif method == "PATCH":
                call_function = requests.patch
            else:
                call_function = requests.get

            options = {}
            if self.datasource_id == "baserow":
                options = {"headers": {"X-Workflow-Token": access.service_token()}, "allow_redirects": False}
            if self.datasource_id == "emailjs" and "evaluacija_url" in data:
                data["evaluacija_url"] = access.evaluation_url(instance_id, variables)
            response = call_function(
                url,
                params=parameters,
                json=data,
                **options,
            )

        if response.status_code not in (200, 201):
            raise Exception(response.text)

        # Check for output variables
        if self.output_variables:
            r = response.json()
            for key in self.output_variables:
                if key in r:
                    variables[key] = r[key]

    async def run(self, variables, instance_id):
        if self.connector_fields["connector_id"] == "http-connector":
            await self.run_connector(variables, instance_id)
        return True


@bpmn_tag("bpmn:sendTask")
class SendTask(ServiceTask):
    def parse(self, element):
        super(SendTask, self).parse(element)


@bpmn_tag("bpmn:callActivity")
class CallActivity(Task):
    def __init__(self):
        self.deployment = False
        self.called_element = ""

    def parse(self, element):
        super(CallActivity, self).parse(element)
        if element.attrib.get("calledElement"):
            self.called_element = element.attrib["calledElement"]
        ns = extension_namespaces(element)
        self.deployment = element.get(f"{{{ns['ext']}}}calledElementBinding") == "deployment"


@bpmn_tag("bpmn:businessRule")
class BusinessRule(ServiceTask):
    def __init__(self):
        self.decision_ref = None

    def parse(self, element):
        super(BusinessRule, self).parse(element)


@bpmn_tag("bpmn:event")
class Event(BpmnObject):
    pass


@bpmn_tag("bpmn:startEvent")
class StartEvent(Event):
    pass


@bpmn_tag("bpmn:intermediateCatchEvent")
class IntermediateCatchEvent(Event):
    def parse(self, element):
        super().parse(element)
        definitions = [node for node in element if node.tag.endswith("EventDefinition")]
        if len(definitions) != 1 or definitions[0].tag != f"{{{NS['bpmn']}}}messageEventDefinition":
            raise ValueError("Only message Intermediate Catch Events are supported")


@bpmn_tag("bpmn:endEvent")
class EndEvent(Event):
    def parse(self, element):
        super().parse(element)
        self.terminate = element.find("bpmn:terminateEventDefinition", NS) is not None
        ns = extension_namespaces(element)
        self.outcome = element.get(f"{{{ns['ext']}}}outcome", "finished")
        if self.outcome not in ("finished", "cancelled") or (self.outcome != "finished" and not self.terminate):
            raise ValueError("Cancelled outcomes require a Terminate End Event")


@bpmn_tag("bpmn:gateway")
class Gateway(BpmnObject):
    def parse(self, element):
        self.incoming = len(element.findall("bpmn:incoming", NS))
        self.outgoing = len(element.findall("bpmn:outgoing", NS))
        super(Gateway, self).parse(element)


@bpmn_tag("bpmn:parallelGateway")
class ParallelGateway(Gateway):
    def add_token(self):
        self.incoming -= 1

    def run(self):
        return self.incoming == 0


@bpmn_tag("bpmn:exclusiveGateway")
class ExclusiveGateway(Gateway):
    def __init__(self):
        self.default = False
        super(ExclusiveGateway, self).__init__()

    def parse(self, element):
        self.default = (
            element.attrib["default"] if "default" in element.attrib else None
        )
        super(ExclusiveGateway, self).parse(element)
