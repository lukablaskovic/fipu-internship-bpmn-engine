import aiohttp
import os
from pathlib import Path
import sys
import env
from utils import access, diary

from aiohttp import web
from uuid import UUID, uuid4, uuid5
import asyncio
from bpmn_model import ServiceTask, BpmnModel, UserFormMessage, UserTask, IntermediateCatchEvent, get_model_for_instance, reload_model, forget_instance
import aiohttp_cors
import db_connector
from functools import reduce
from datetime import datetime


# Setup database
db_connector.setup_db()
routes = web.RouteTableDef()

# uuid4 = lambda: 2  # hardcoded for easy testing

models = {}
for file in os.listdir("models"):
    if file.endswith(".bpmn"):
        m = BpmnModel(file)
        models[file] = m


async def run_as_server(app):
    app["bpmn_models"] = models
    log = db_connector.get_running_instances_log(running_only=False, include_definition=True)
    for l in log:
        for key, data in l.items():
            if data["model_path"] in app["bpmn_models"]:
                current = app["bpmn_models"][data["model_path"]]
                model = current
                if data["xml"] and data["xml"] != current.xml:
                    model = BpmnModel(current.model_path, data["xml"])
                    model.instances = current.instances
                instance = await model.create_instance(key, {})
                if not data["xml"]:
                    instance.version = None
                instance = await instance.run_from_log(data["events"])
                instance.incident = db_connector.open_attempt(key)
                if instance.incident:
                    instance.state = "failed"
                elif instance.pending:
                    start_runner(instance)


def start_runner(instance):
    instance.in_queue = asyncio.Queue()
    for submission in db_connector.get_pending_submissions(instance._id):
        if isinstance(instance.model.elements.get(submission["task_id"]), IntermediateCatchEvent):
            instance.active_event = submission["task_id"]
        instance.in_queue.put_nowait(UserFormMessage(
            submission["task_id"], submission["form_data"], submission["id"]
        ))
    instance.state = "running"
    instance.runner = asyncio.create_task(instance.run())


# Get all models
# Model.search
from types import SimpleNamespace


@routes.get("/status")
async def health(request):
    return web.json_response({"status": "ok"})


@web.middleware
async def authorize(request, handler):
    name = handler.__name__
    if request.method == "OPTIONS" or request.match_info.http_exception is not None or name == "health":
        return await handler(request)
    instance_id = request.match_info.get("instance_id")
    if name == "employer_evaluation":
        token = request.headers.get("X-Evaluation-Token")
        if not token:
            raise web.HTTPUnauthorized()
        model = get_model_for_instance(instance_id)
        instance = model and model.instances.get(instance_id)
        if not instance or not access.valid_evaluation(token, instance):
            raise web.HTTPForbidden()
        if instance.state != "running" or instance.active_event or access.EVALUATION not in [t._id for t in instance.pending]:
            raise web.HTTPConflict(text="Evaluation has closed")
    else:
        principal = request["principal"] = await access.connector_read(request, "/api/session")
        if not principal["coordinator"]:
            if name not in {"get_instance_model", "get_model", "handle_new_instance", "handle_instance_info", "handle_task_info", "handle_form", "handle_event"}:
                raise web.HTTPForbidden()
            if instance_id:
                model = get_model_for_instance(instance_id)
                instance = model and model.instances.get(instance_id)
                if not instance or not access.is_owner(principal, instance):
                    raise web.HTTPNotFound()
                if name == "handle_form" and request.match_info["task_id"] not in access.STUDENT_TASKS:
                    raise web.HTTPForbidden()
                if name == "handle_event" and request.match_info["event_id"] != "odustajanje_od_prakse":
                    raise web.HTTPForbidden()
                if name in {"handle_form", "handle_event"} and (principal["student"] or {}).get("process_instance_id") != instance_id:
                    raise web.HTTPForbidden()
    response = await handler(request)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@routes.get("/model")
async def get_models(request):
    models = {}
    for file in os.listdir("models"):
        if file.endswith(".bpmn"):
            m = BpmnModel(file)
            models[file] = m

    running_instance_logs = db_connector.get_running_instances_log(running_only=False)

    instance_to_model_mapping = {}
    for log_entry in running_instance_logs:
        for instance_id, details in log_entry.items():
            instance_to_model_mapping[instance_id] = details

    for model in models.values():
        for instance_id, details in instance_to_model_mapping.items():
            if details["model_path"] == model.model_path:
                instance_object = SimpleNamespace(
                    _id=instance_id, events=details["events"]
                )
                model.instances[instance_id] = instance_object

    data = [m.to_json() for m in models.values()]
    return web.json_response({"status": "ok", "results": data})


@routes.get("/engine")
async def engine_dashboard(request):
    deployments = []
    for name, loaded in sorted(request.app["bpmn_models"].items()):
        current = BpmnModel(name)
        versions = []
        for snapshot in Path("models/history").glob(name + ".*.xml"):
            number = snapshot.name[len(name) + 1:-4]
            if number.isascii() and number.isdigit() and 0 < int(number) < current.version:
                versions.append(int(number))
        deployments.append({
            "previous_versions": sorted(versions, reverse=True),
            "model_path": name, "version": current.version,
            "process": current.main_process.name,
            "active": sum(i.state not in {"finished", "cancelled"} for i in loaded.instances.values()),
        })
    incidents = []
    for model in request.app["bpmn_models"].values():
        for instance in model.instances.values():
            if instance.incident:
                task = instance.model.elements.get(instance.incident["task_id"])
                incidents.append({**instance.incident, "instance_id": instance._id,
                                  "model": model.model_path, "version": instance.version,
                                  "task": task.name if task else None,
                                  "connector": task.datasource_id if hasattr(task, "datasource_id") else None})
    return web.json_response({"deployments": deployments, "incidents": incidents})


@routes.get("/instance/{instance_id}/model")
async def get_instance_model(request):
    model = get_model_for_instance(request.match_info["instance_id"])
    if not model:
        raise web.HTTPNotFound()
    return web.Response(text=model.xml, content_type="application/xml")


@routes.get("/model/{model_name}")
async def get_model(request):
    model_name = request.match_info.get("model_name")
    if model_name not in request.app["bpmn_models"]:
        raise web.HTTPNotFound()
    return web.FileResponse(
        path=os.path.join("models", model_name), headers={"Cache-Control": "no-store"}
    )


# Creates new process instance
@routes.post("/model/{model_name}/instance")
async def handle_new_instance(request):
    _id = str(uuid4())
    model = request.match_info.get("model_name")
    if model not in request.app["bpmn_models"]:
        raise web.HTTPNotFound()
    data = await request.json() if request.can_read_body else {}
    if not isinstance(data, dict):
        raise web.HTTPBadRequest()
    principal = request["principal"]
    if principal["coordinator"] or not principal["student"]:
        raise web.HTTPForbidden()
    variables = {"_owner_email": principal["email"], "_owner_student_id": principal["student"]["id"]}
    def created():
        return web.json_response({"id": _id, "binding": access.signature("bind", principal["email"], _id,
                                 model.removeprefix("strucna_praksa_").removesuffix(".bpmn"), previous_id)})
    previous_id = data.get("previous_instance_id")
    if previous_id is not None:
        try:
            previous_uuid = UUID(str(previous_id))
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid previous instance ID")
        previous = get_model_for_instance(str(previous_uuid))
        if not previous or not access.is_owner(principal, previous.instances[str(previous_uuid)]):
            raise web.HTTPNotFound()
        if previous.instances[str(previous_uuid)].state != "cancelled":
            raise web.HTTPConflict(text="Previous internship must be withdrawn")
        # A retry after a failed student-record update reuses the same successor.
        _id = str(uuid5(previous_uuid, "next-internship"))
        existing = get_model_for_instance(_id)
        if existing:
            if not access.is_owner(principal, existing.instances[_id]):
                raise web.HTTPForbidden()
            if existing.model_path != model:
                raise web.HTTPConflict(text="Another model was already selected")
            return created()
        variables["previous_instance_id"] = str(previous_uuid)
    if (principal["student"].get("process_instance_id") or None) != previous_id:
        raise web.HTTPConflict(text="Student already has an internship")
    current = reload_model(request.app["bpmn_models"], model)
    instance = await current.create_instance(_id, variables)
    instance.runner = asyncio.create_task(instance.run())
    return created()


@routes.post("/instance/{instance_id}/task/{task_id}/form")
async def handle_form(request):
    instance_id = request.match_info["instance_id"]
    task_id = request.match_info["task_id"]
    if task_id == diary.TASK:
        model = get_model_for_instance(instance_id)
        if not model:
            raise web.HTTPNotFound()
        instance = model.instances[instance_id]
        principal = request["principal"]
        if (principal["student"] or {}).get("process_instance_id") != instance_id or not access.is_owner(principal, instance):
            raise web.HTTPForbidden()
        async with instance.submission_lock:
            previous = db_connector.diary_receipt(instance_id, task_id)
            if not previous and (instance.active_event or task_id not in [t._id for t in instance.pending] or instance.state != "running"):
                raise web.HTTPConflict(text="Diary is not pending")
            fields, files, receipt = await diary.read_diary(request)
            if previous:
                if previous != receipt:
                    raise web.HTTPConflict(text="A different diary was already submitted")
                return web.json_response({"status": "OK"})
            row_id = instance.variables["id_dnevnik_prakse"]
            headers = {"X-Workflow-Token": access.service_token()}
            for key, column in diary.FILES.items():
                await access.connector_request("PUT", f"/api/diary/{row_id}/{column}", headers,
                                               binary=files[key])
            fields["_diary_receipt"] = receipt
            return await submit_form(request, instance_id, task_id, fields)
    model = get_model_for_instance(instance_id)
    if not model:
        raise web.HTTPNotFound()
    async with model.instances[instance_id].submission_lock:
        post = await request.json()
        return await submit_form(request, instance_id, task_id, post)


async def submit_form(request, instance_id, task_id, post):
    m = get_model_for_instance(instance_id)
    if m is None or instance_id not in m.instances:
        raise web.HTTPNotFound()
    instance = m.instances[instance_id]
    if not isinstance(post, dict):
        raise web.HTTPBadRequest(text="Form data must be a JSON object")
    def require_pending():
        if instance.state != "running" or instance.active_event:
            raise web.HTTPConflict(text="Instance is not running")
        if not any(isinstance(t, UserTask) and t._id == task_id for t in instance.pending):
            raise web.HTTPConflict(text="Task is not waiting for a form")

    require_pending()
    principal = request.get("principal")
    if task_id in {"odabiranje_zadatka_student", "model_b_direktna_prijava_student"}:
        student = principal["student"]
        if not student or student.get("process_instance_id") != instance_id or not access.is_owner(principal, instance):
            raise web.HTTPForbidden()
        post.update(JMBAG=student["JMBAG"], Student=[student["id"]], student_email=principal["email"],
                    student_ime=student["ime"], student_prezime=student["prezime"],
                    student_godina_studija=(student.get("godina_studija") or {}).get("value"),
                    frontend_url=env.SYSTEM_VARS["_frontend_url"], process_instance_id=instance_id)
        if task_id == "model_b_direktna_prijava_student":
            allocation = await access.connector_request("POST", "/api/alokacija/direct",
                {"X-Workflow-Token": access.service_token()}, {"id_instance": instance_id, "Student": post["Student"],
                "JMBAG": post["JMBAG"], "Napomena": post.get("napomena")})
            require_pending()
            post.update(id_alokacija=allocation["id_alokacija"], Alocirani_zadatak=[instance.variables["id_zadatak"]])
    if task_id == "alociranje_profesor":
        post["_evaluation_origin"] = access.evaluation_origin(request.headers.get("Origin"))
        for key in ("Student", "JMBAG"):
            if key in instance.variables:
                post[key] = instance.variables[key]
    try:
        if any(s["task_id"] == task_id for s in db_connector.get_pending_submissions(instance_id)):
            raise web.HTTPConflict(text="Task already has an accepted submission")
        submission_id = db_connector.save_submission(instance_id, task_id, post)
    except web.HTTPException:
        raise
    except Exception:
        raise web.HTTPServiceUnavailable(text="Could not save submission")
    instance.in_queue.put_nowait(UserFormMessage(task_id, post, submission_id))

    return web.json_response({"status": "OK"})


async def resume_instance(instance, actor="", attempt_id=None):
    record = db_connector.get_running_instances_log(instance_id=instance._id)
    if not record:
        raise web.HTTPConflict(text="No running checkpoint to resume")
    instance.variables = {}
    await instance.run_from_log(record[0][instance._id]["events"])
    if instance.incident:
        incident = instance.incident
        task_id = incident["task_id"]
        task = next((t for t in instance.pending if t._id == task_id), None)
        if not task or incident["attempt_id"] != attempt_id:
            raise web.HTTPConflict(text="Failed step has changed")
        attempt = db_connector.start_attempt(instance._id, task_id, attempt_id, actor)
        if attempt is None:
            raise web.HTTPConflict(text="Attempt already retried")
        instance.pending.remove(task)
        instance.pending.insert(0, task)
        instance.retry_attempt = attempt
        instance.incident = None
    start_runner(instance)


@routes.get("/instance/{instance_id}/rollback")
@routes.post("/instance/{instance_id}/rollback")
async def rollback_instance(request):
    iid = request.match_info["instance_id"]
    body = None
    if request.method == "POST":
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest()
        if not isinstance(body, dict) or type(body.get("checkpoint")) is not int or not isinstance(body.get("target"), str):
            raise web.HTTPBadRequest(text="Expected checkpoint and target task")

    model = get_model_for_instance(iid)
    if not model:
        raise web.HTTPNotFound()
    instance = model.instances[iid]
    history = db_connector.rollback_history(iid)
    elements = model.process_elements[instance.process]
    past = []
    source = None
    checkpoint = 0
    for checkpoint, completed, pending in history:
        if completed == source and isinstance(elements.get(completed), UserTask):
            past.append(completed)
        work = [key for key in pending if not isinstance(elements.get(key), IntermediateCatchEvent)]
        source = work[0] if len(work) == 1 else None
        # A modeled return or coordinator move makes later visits future steps again.
        for key in pending:
            if key in past:
                del past[past.index(key):]
    eligible = (
        (instance.state == "running" and instance.runner and isinstance(elements.get(source), UserTask))
        or (instance.state == "failed" and instance.incident and source == instance.incident["task_id"]
            and isinstance(elements.get(source), ServiceTask))
    )
    if not eligible or instance.active_event or instance.submission_lock.locked() or db_connector.get_pending_submissions(iid):
        past = []
    if body is None:
        return web.json_response({"checkpoint": checkpoint, "source": source,
                                  "targets": [{"id": key, "name": elements[key].name or key} for key in reversed(past)]})
    if checkpoint != body["checkpoint"] or body["target"] not in past:
        raise web.HTTPConflict(text="Task is not behind the current checkpoint")

    target = body["target"]
    was_failed = instance.state == "failed"
    # Failed event commits can leave uncommitted changes in the runner's variables.
    if was_failed:
        saved = db_connector.get_running_instances_log(instance_id=iid)[0][iid]["events"]
    subscriptions = [t._id for t in instance.pending if isinstance(t, IntermediateCatchEvent)]
    event = db_connector.record_rollback(instance, source, target,
                                         [target, *subscriptions],
                                         request["principal"]["email"])
    if was_failed:
        instance.variables = {}
        await instance.run_from_log(saved)
    # Rollback opens a new path; apply_rollback resets its gateway counters.
    if instance.runner and not instance.runner.done():
        instance.runner.cancel()
    instance.apply_rollback(event)
    start_runner(instance)
    return web.json_response({"status": "OK"})


@routes.post("/instance/{instance_id}/retry")
async def retry_connector(request):
    iid = request.match_info["instance_id"]
    model = get_model_for_instance(iid)
    if not model:
        raise web.HTTPNotFound()
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest()
    if not isinstance(body, dict) or type(body.get("attempt_id")) is not int:
        raise web.HTTPBadRequest(text="Expected attempt ID")
    instance = model.instances[iid]
    incident = instance.incident
    if instance.state != "failed" or not incident or incident["attempt_id"] != body["attempt_id"]:
        raise web.HTTPConflict(text="Attempt is no longer pending")
    task = model.elements.get(incident["task_id"])
    if getattr(task, "datasource_id", None) == "emailjs" and body.get("confirm_resend") is not True:
        raise web.HTTPConflict(text="Email may already have been delivered")
    await resume_instance(instance, request["principal"]["email"], incident["attempt_id"])
    return web.json_response({"status": "accepted"}, status=202)


@routes.post("/instance/{instance_id}/event/{event_id}")
async def handle_event(request):
    instance_id = request.match_info["instance_id"]
    event_id = request.match_info["event_id"]
    model = get_model_for_instance(instance_id)
    if model is None or instance_id not in model.instances:
        raise web.HTTPNotFound()
    instance = model.instances[instance_id]
    event = instance.model.elements.get(event_id)
    if not isinstance(event, IntermediateCatchEvent):
        raise web.HTTPNotFound()
    try:
        post = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="Message events require JSON")
    if post != {}:
        raise web.HTTPBadRequest(text="Message events accept an empty JSON object")
    if instance.submission_lock.locked():
        raise web.HTTPConflict(text="Submission is being processed")
    submissions = db_connector.get_pending_submissions(instance_id)
    if (event_id in instance.triggered_events and event_id not in [t._id for t in instance.pending]) or any(s["task_id"] == event_id for s in submissions):
        if instance.state == "failed":
            incident = instance.incident
            if incident and not (
                event_id == "odustajanje_od_prakse" and incident["task_id"] == "evidentiranje_odustajanja"
                and getattr(model.elements.get(incident["task_id"]), "datasource_id", None) == "baserow"
            ):
                raise web.HTTPConflict(text="Coordinator recovery required")
            await resume_instance(instance, request["principal"]["email"], incident and incident["attempt_id"])
        return web.json_response({"status": "OK"})
    if instance.state != "running" or event_id not in [t._id for t in instance.pending] or submissions:
        raise web.HTTPConflict(text="Event is not available or a form is being processed")
    try:
        submission_id = db_connector.save_submission(instance_id, event_id, {})
    except Exception:
        raise web.HTTPServiceUnavailable(text="Could not save event")
    instance.active_event = event_id
    instance.in_queue.put_nowait(UserFormMessage(event_id, {}, submission_id))
    return web.json_response({"status": "OK"})


@routes.get("/instance")
async def search_instance(request):
    params = request.rel_url.query
    queries = []
    try:
        strip_lower = lambda x: x.strip().lower()
        check_colon = lambda x: x if ":" in x else f":{x}"

        queries = list(
            tuple(
                map(
                    strip_lower,
                    check_colon(q).split(":"),
                )
            )
            for q in params["q"].split(",")
        )
    except:
        return web.json_response({"error": "invalid_query"}, status=400)

    result_ids = []
    for att, value in queries:
        ids = []
        for m in models.values():
            for _id, instance in m.instances.items():
                search_atts = []
                if not att:
                    search_atts = list(instance.variables.keys())
                else:
                    for key in instance.variables.keys():
                        if not att or att in key.lower():
                            search_atts.append(key)
                search_atts = filter(
                    lambda x: isinstance(instance.variables[x], str), search_atts
                )

                for search_att in search_atts:
                    if search_att and value in instance.variables[search_att].lower():
                        # data.append(instance.to_json())
                        ids.append(_id)
        result_ids.append(set(ids))

    ids = reduce(lambda a, x: a.intersection(x), result_ids[:-1], result_ids[0])

    data = []
    for _id in ids:
        data.append(get_model_for_instance(_id).instances[_id].to_json())

    return web.json_response({"status": "ok", "results": data})


@routes.get("/instance/{instance_id}/task/{task_id}")
async def handle_task_info(request):
    instance_id = request.match_info.get("instance_id")
    task_id = request.match_info.get("task_id")
    m = get_model_for_instance(instance_id)
    if not m:
        raise aiohttp.web.HTTPNotFound
    instance = m.instances[instance_id]
    task = instance.model.elements[task_id]

    return web.json_response(task.get_info())


@routes.get("/instance/{instance_id}")
async def handle_instance_info(request):
    instance_id = request.match_info.get("instance_id")
    m = get_model_for_instance(instance_id)
    if not m:
        raise aiohttp.web.HTTPNotFound
    instance = m.instances[instance_id].to_json()

    if not request["principal"]["coordinator"]:
        instance["model"]["instances"] = [instance_id]
    return web.json_response(instance)


@routes.get("/instance/{instance_id}/evaluation-link")
async def coordinator_evaluation_link(request):
    iid = request.match_info["instance_id"]
    model = get_model_for_instance(iid)
    if not model or access.EVALUATION not in [t._id for t in model.instances[iid].pending]:
        raise web.HTTPConflict(text="No pending evaluation")
    return web.json_response({"url": access.evaluation_url(iid, model.instances[iid].variables, request.headers.get("Origin"))})


@routes.get("/evaluation/{instance_id}")
@routes.post("/evaluation/{instance_id}")
async def employer_evaluation(request):
    iid = request.match_info["instance_id"]
    instance = get_model_for_instance(iid).instances[iid]
    if request.method == "POST":
        data = await request.json()
        if not access.valid_evaluation(request.headers["X-Evaluation-Token"], instance):
            raise web.HTTPForbidden()
        if data not in ({"kandidat_odobren": "true"}, {"kandidat_odobren": "false"}):
            raise web.HTTPBadRequest(text="Expected an evaluation decision")
        return await submit_form(request, iid, access.EVALUATION, data)
    fields = ("student_ime", "student_prezime", "student_email", "student_godina_studija", "Alocirani_zadatak",
              "opis_zadatka", "poslodavac_naziv", "poslodavac_email")
    return web.json_response({"variables": {k: instance.variables.get(k) for k in fields},
                              "task": instance.model.elements[access.EVALUATION].get_info()})


@routes.delete("/instance/{instance_id}")
async def delete_instance(request):
    instance_id = request.match_info.get("instance_id")
    response = db_connector.delete_instance(instance_id)
    if response["status"] == "success":
        forget_instance(instance_id)
        return web.json_response(
            {"status": "ok", "message": "Instance deleted successfully."}
        )
    else:
        return web.json_response(
            {"status": "error", "message": response["message"]}, status=400
        )


@routes.get("/events")
async def get_all_events(request):
    try:
        events = db_connector.get_all_events()
        data = []
        for event in events:
            model = request.app["bpmn_models"].get(event.model_name)
            instance = model and model.instances.get(event.instance_id)
            data.append({**event.to_dict(), "model_version": instance.version if instance else None})
        return web.json_response({"status": "ok", "results": data})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)})


app = None

def run():
    global app
    access.service_token()
    app = web.Application(middlewares=[authorize])
    app.on_startup.append(run_as_server)
    app.add_routes(routes)

    cors = aiohttp_cors.setup(
        app,
        defaults={
            "*": aiohttp_cors.ResourceOptions(
                allow_credentials=True,
                expose_headers="*",
                allow_headers="*",
                allow_methods="*",
            )
        },
    )

    for route in list(app.router.routes()):
        cors.add(route)

    return app


async def serve():
    return run()


if __name__ == "__main__":
    app = run()
    web.run_app(app, port=os.getenv("PORT", 8080))

# conda activate fipu-internship-bpmn-engine && npx nodemon server.py
