# A python module for parsing and executing BPMN models

## FIPU execution profile

The shipped internship models use standard BPMN 2.0 model/diagram XML and the
`urn:fipu:bpmn:1` extension namespace. Praksa BPMN Studio edits this profile without
Camunda libraries. This is a limited execution profile, not full BPMN conformance.

Forms use `fipu:formData` / `fipu:formField`; connectors use
`fipu:connector` / `fipu:connectorId`, and task/connector input-output mappings use
`fipu:inputOutput`. Parameter values can be text, `fipu:list` of `fipu:value`, or
`fipu:map` of `fipu:entry key="..."`. `${variable}` substitutions retain the
existing engine behavior. Output parameter names select top-level JSON response
keys; output expressions are not evaluated. User-task input/output extensions
are preserved by the editor but only declared form fields are accepted by the
engine. Gateway conditions use exact `key:value` string equality.

Execution extensions must use the FIPU namespace. XML prefixes are arbitrary. Service/send tasks must
define one registered HTTP connector with a supported method and a relative URL
without path traversal. Script parameters and malformed connector configuration
fail when the model is loaded, before a workflow starts.

`models/` contains exactly the authoritative A/B files. Studio and MCP edit these files; the dashboard reads them through `GET /model/<filename>` with caching disabled. The obsolete edited/example model was removed (no local instances used it). Before deploying this removal elsewhere, check for instances referencing that legacy filename.

Every new instance reparses its current model file. Existing live instances retain their already-loaded graph and remain discoverable through the shared instance registry. Restart recovery still reads the current files: preserve task/form IDs and review graph compatibility for existing instances. There is no public model-write endpoint on the engine; Studio's local authoring service owns revision-checked file writes. No restart or snapshot sync is needed for new instances after a model edit.

Run `python -m unittest discover -s tests -v` in an environment with the engine
requirements installed. The extension tests stub configuration and HTTP calls;
the persistence tests use temporary SQLite databases and synthetic subprocesses.

## Supported BPMN elements so far:

### Start/end events

### User Task
- Form fields
    - Id, Type, Label
    - Validation, Properties
- Element Documentation 

### Service Task & Send Task
- Supported implementation at the moment is **Connector**
    - Connector ID is an `env.DS` key: `baserow`, `emailjs` (the email connector), or `pdf`. The datasource type is `http-connector`.
    - Input parameters needs to have following parameters:
        - **url** -> connector-relative path, for example `/api/prijavnica`; the base URL comes from `env.DS`, not the model
        - **method** -> `GET`, `POST`, or `PATCH`
        - **url_parameter** -> in case you want to send URL parameter with your request, it should be **Map** type with _key_ and _value_. It is worth noting that _value_ can be simple expression eg. `${my_process_variable}`. Eg. URL request result for _key_ `task_id` and _value_ `${current_task}`, if `current_task` is inside process variables, will be http://myservice.com/api/call?task_id=1
- Input variables and output variables for Service and Send task **must** be specified in a task-level `fipu:inputOutput` element
    - Input parameters type at the moment can be **String or Expression** and if you plan to use process variables you should use expression, eg. `${my_process_variable}`, only **String or Expression** type is currently supporting expressions
        - All input variables will be send as JSON to the service  
    - Output parameters will be saved to process variables by their _name_, **String or Expression** is currently advised although only Script is not supported 
        - It is expected that service response with JSON
        - It will try to match Output parameter _name_ with keys inside JSON -> if found -> process_variables\[_name_] = response\[_name_]

### Call Activity
- CallActivity Type **must** be BPMN
- Called Element **must** be *process_id* of process you wish to start
- Binding **must** be **deployment** if you wish to call process from other BPMN diagram, other bindings assumes that called process is inside the same diagraas Call Activity

### Gateways (Exclusive, Parallel)

An Exclusive Gateway takes the first matching outgoing Sequence Flow in XML order,
then the default only when no other flow matches. A normal End Event consumes its
own token; other parallel branches remain pending and are retained in the event
checkpoint. The instance finishes when no tokens remain. Terminate End Events are
outside the supported profile.

Parallel gateways currently use a single-pass incoming-token counter. Cycles that
re-enter a parallel gateway are not supported by this execution profile.

### Sequence flow with conditions
- To use conditions on sequence flows you **must** chose Condition Type **Expression** and in Expression you need to write `key:value` where `key` is the process variable name and `value` is string
- eg. `standard:BPMN` -> engine will check -> `process_variables["standard"]` == `"BPMN"` -> if True returns True -> process continues on that Sequence Flow
- In the future real expression will be possible 

### Collaboration Diagrams
- In case there is more then 1 Pool in Collaboration diagram you **MUST** specify in **Extensions/Properties** a property with _name_ `is_main` and _value_ `True` for your **main** Pool so the engine knows where to start the process

---

## Pending features:
-   full fledged REST API
-   in/out variables for Call Activity
-   all standard BPMN elements
-   ...

---

## Example BPMN model used for demo:
![image](https://user-images.githubusercontent.com/714889/114159824-81c65d80-9926-11eb-8b74-6d5dd9bb82ea.png)

> The package can be used as a standalone server exposing a REST API (see `server.py`)

Example execution trace:

```python
Running process 1
-----------------
        [1] --> msg in: t_wrong
        [1] Waiting for user... [UserTask(Which option?)]
        [1] --> msg in: t0
        [1] DOING: UserTask(Which option?)
        [1] Waiting for user... [UserTask(Down), UserTask(Up)]
        [1] --> msg in: tup
        [1] DOING: UserTask(Up)
        [1] Waiting for user... [UserTask(Down), ParallelGateway(ParallelGateway_0vffee4)]
        [1] --> msg in: t_wrong
        [1] Waiting for user... [UserTask(Down), ParallelGateway(ParallelGateway_0vffee4)]
        [1] --> msg in: tdown
        [1] DOING: UserTask(Down)
        [1] DOING: ManualTask(Manual Task 2)
        [1] DOING: ServiceTask(Task 3)
        [1]     - checking variables={} with ['option==1']...
        [1]       DONE: Result is False
        [1]     - going down default path...
        [1] Waiting for user... [UserTask(Task down)]
        [1] --> msg in: t_wrong
        [1] Waiting for user... [UserTask(Task down)]
        [1] --> msg in: tup2
        [1] Waiting for user... [UserTask(Task down)]
        [1] --> msg in: t_wrong
        [1] Waiting for user... [UserTask(Task down)]
        [1] --> msg in: tdown2
        [1] DOING: UserTask(Task down)
        [1] DONE
Running process 2
-----------------
        [2] --> msg in: t_wrong
        [2] Waiting for user... [UserTask(Which option?)]
        [2] --> msg in: t0
        [2] DOING: UserTask(Which option?)
        [2] Waiting for user... [UserTask(Down), UserTask(Up)]
        [2] --> msg in: tup
        [2] DOING: UserTask(Up)
        [2] Waiting for user... [UserTask(Down), ParallelGateway(ParallelGateway_0vffee4)]
        [2] --> msg in: t_wrong
        [2] Waiting for user... [UserTask(Down), ParallelGateway(ParallelGateway_0vffee4)]
        [2] --> msg in: tdown
        [2] DOING: UserTask(Down)
        [2] DOING: ManualTask(Manual Task 2)
        [2] DOING: ServiceTask(Task 3)
        [2]     - checking variables={} with ['option==1']...
        [2]       DONE: Result is False
        [2]     - going down default path...
        [2] Waiting for user... [UserTask(Task down)]
        [2] --> msg in: t_wrong
        [2] Waiting for user... [UserTask(Task down)]
        [2] --> msg in: tup2
        [2] Waiting for user... [UserTask(Task down)]
        [2] --> msg in: t_wrong
        [2] Waiting for user... [UserTask(Task down)]
        [2] --> msg in: tdown2
        [2] DOING: UserTask(Task down)
        [2] DONE
```


## Persistence and restart checks

A successful `POST /instance/{instance_id}/task/{task_id}/form` means the form is
committed to the `PendingSubmission` inbox. It does not mean downstream connectors
have completed. Invalid/non-waiting tasks and duplicate queued submissions return
409; non-object form data returns 400; an inbox storage failure returns 503.

Each processed form is removed from the inbox in the same transaction that writes
its event, changed variable values, next pending task IDs, and running flag. Startup
replays events in insertion order, applies later values over earlier ones, and
reloads outstanding submissions for running instances only. Completed instances
remain available in model listings. Persistence errors stop execution rather than
advancing without a checkpoint; after correcting the database problem, restart the
engine to recover its saved checkpoint and outstanding forms.

Startup creates the additive `PendingSubmission` table using the existing Pony
schema setup. Back up the database before upgrading and restart the single engine
process; do not run old and new workers together. Existing events remain readable,
but corrected values never recorded by older versions cannot be reconstructed.
SQLite stores its database under the working directory's `database/` folder.

With the engine dependencies installed, run:

```sh
python -m unittest discover -s tests -v
```

Tests launch real HTTP servers in child processes with temporary SQLite databases,
then kill and restart them. They cover corrected scalar/nested values and exact
waiting tasks, acknowledged forms before queue processing, failed inbox/event
writes, atomic inbox consumption, completed instances, duplicates, and deletion.
The actual A/B models are also checked from the application step to diary waiting;
those tests stub connector execution and use only synthetic data.

This is single-process recovery, not distributed worker coordination. External
connector effects (Baserow writes, email, PDF) are not atomic with engine events and
can repeat if a crash occurs after the external effect but before its event commit.

### Internship withdrawal

Both internship models contain the `odustajanje_od_prakse` Intermediate Message Catch Event. Submit `{}` to `POST /instance/{instance_id}/event/odustajanje_od_prakse`; acknowledgement means the message is durable, not that the connector has finished. Read the instance until `state` is `cancelled` (or `failed`). Repeating the event request after a failed connector/checkpoint resumes from the last committed transition. Completed withdrawal requests are idempotent. A normal form submitted after withdrawal is accepted is rejected with 409.

The BPMN branch updates linked Baserow allocations to `student_odustao` and reaches a terminate end event with `fipu:outcome="cancelled"`. It preserves business records and event history. The API exposes waiting message IDs in `available_events` and the accepted message in `active_event`; ordinary `pending` task IDs retain their previous meaning. Completed instances remain readable after restart. Restart the engine to add the new start-branch subscription to existing active instances; Studio saves alone only affect newly created instances.
