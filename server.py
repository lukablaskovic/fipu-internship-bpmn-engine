import aiohttp
import os
import sys
import env

from aiohttp import web
from uuid import UUID, uuid4, uuid5
import asyncio
from bpmn_model import BpmnModel, UserFormMessage, UserTask, IntermediateCatchEvent, get_model_for_instance, reload_model
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
    log = db_connector.get_running_instances_log(running_only=False)
    for l in log:
        for key, data in l.items():
            if data["model_path"] in app["bpmn_models"]:
                instance = await app["bpmn_models"][data["model_path"]].create_instance(
                    key, {}
                )
                instance = await instance.run_from_log(data["events"])
                for submission in db_connector.get_pending_submissions(key):
                    if isinstance(instance.model.elements.get(submission["task_id"]), IntermediateCatchEvent):
                        instance.active_event = submission["task_id"]
                    instance.in_queue.put_nowait(UserFormMessage(
                        submission["task_id"], submission["form_data"], submission["id"]
                    ))
                if instance.pending:
                    asyncio.create_task(instance.run())


# Get all models
# Model.search
from types import SimpleNamespace


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
    variables = {}
    previous_id = data.get("previous_instance_id")
    if previous_id is not None:
        try:
            previous_uuid = UUID(str(previous_id))
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid previous instance ID")
        previous = get_model_for_instance(str(previous_uuid))
        if previous is None or previous.instances[str(previous_uuid)].state != "cancelled":
            raise web.HTTPConflict(text="Previous internship must be withdrawn")
        # A retry after a failed student-record update reuses the same successor.
        _id = str(uuid5(previous_uuid, "next-internship"))
        existing = get_model_for_instance(_id)
        if existing:
            if existing.model_path != model:
                raise web.HTTPConflict(text="Another model was already selected")
            return web.json_response({"id": _id})
        variables["previous_instance_id"] = str(previous_uuid)
    current = reload_model(request.app["bpmn_models"], model)
    instance = await current.create_instance(_id, variables)
    asyncio.create_task(instance.run())
    return web.json_response({"id": _id})


@routes.post("/instance/{instance_id}/task/{task_id}/form")
async def handle_form(request):
    post = await request.json()
    instance_id = request.match_info.get("instance_id")
    task_id = request.match_info.get("task_id")
    m = get_model_for_instance(instance_id)
    if m is None or instance_id not in m.instances:
        raise web.HTTPNotFound()
    instance = m.instances[instance_id]
    if not isinstance(post, dict):
        raise web.HTTPBadRequest(text="Form data must be a JSON object")
    if instance.state != "running" or instance.active_event:
        raise web.HTTPConflict(text="Instance is not running")
    if not any(isinstance(t, UserTask) and t._id == task_id for t in instance.pending):
        raise web.HTTPConflict(text="Task is not waiting for a form")
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


async def resume_instance(instance):
    for record in db_connector.get_running_instances_log():
        if instance._id in record:
            instance.variables = {}
            await instance.run_from_log(record[instance._id]["events"])
            instance.in_queue = asyncio.Queue()
            for submission in db_connector.get_pending_submissions(instance._id):
                instance.active_event = submission["task_id"]
                instance.in_queue.put_nowait(UserFormMessage(
                    submission["task_id"], submission["form_data"], submission["id"]
                ))
            instance.state = "running"
            asyncio.create_task(instance.run())
            return
    raise web.HTTPConflict(text="No running checkpoint to resume")


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
    submissions = db_connector.get_pending_submissions(instance_id)
    if event_id in instance.triggered_events:
        if instance.state == "failed":
            # Retry from the committed checkpoint, including a failed connector write.
            await resume_instance(instance)
        return web.json_response({"status": "OK"})
    if any(s["task_id"] == event_id for s in submissions):
        if instance.state == "failed":
            await resume_instance(instance)
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

    return web.json_response(instance)


@routes.delete("/instance/{instance_id}")
async def delete_instance(request):
    instance_id = request.match_info.get("instance_id")
    response = db_connector.delete_instance(instance_id)
    if response["status"] == "success":
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
        data = [event.to_dict() for event in events]
        return web.json_response({"status": "ok", "results": data})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)})


app = None

def run():
    global app
    app = web.Application()
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

# conda activate python-bpmn-engine && npx nodemon server.py
