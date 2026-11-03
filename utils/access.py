"""HTTP identity and purpose-bound workflow credentials (never workflow variables)."""
import asyncio
import hmac
import json
import os
import re
from pathlib import Path
import time
import aiohttp
from aiohttp import web
import env

STUDENT_TASKS = {"odabiranje_zadatka_student", "model_b_direktna_prijava_student",
                 "ispunjavanje_prijavnice_student", "predavanje_dnevnika_student"}
EVALUATION = "evaluacija_poslodavac"


def service_token():
    key = Path(os.environ["WORKFLOW_TOKEN_FILE"]).read_text().strip() if os.getenv("WORKFLOW_TOKEN_FILE") else os.environ["WORKFLOW_SERVICE_TOKEN"]
    if len(key) < 32:
        raise ValueError("WORKFLOW_SERVICE_TOKEN requires at least 32 characters")
    return key


def signature(*fields):
    return hmac.digest(service_token().encode(), json.dumps(fields).encode(), "sha256").hex()


def evaluation_token(instance_id, variables, expires):
    fields = ["evaluation", instance_id, variables["id_alokacija"], variables["poslodavac_email"], expires]
    if "_evaluation_generation" in variables:
        fields.append(variables["_evaluation_generation"])
    return signature(*fields)


def evaluation_origin(origin):
    if re.fullmatch(r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", origin or ""):
        return origin
    return env.SYSTEM_VARS["_frontend_url"]


def evaluation_url(instance_id, variables, origin=None):
    expires = int(time.time()) + 14 * 86400
    token = evaluation_token(instance_id, variables, expires)
    return f"{evaluation_origin(origin)}/evaluacija/{instance_id}#{expires}.{token}"


def valid_evaluation(token, instance):
    expiry, _, mac = token.partition(".")
    if not expiry.isascii() or not expiry.isdigit() or len(expiry) > 12 or int(expiry) <= time.time():
        return False
    if not all(instance.variables.get(k) for k in ("id_alokacija", "poslodavac_email")):
        return False
    return hmac.compare_digest(mac.encode(), evaluation_token(instance._id, instance.variables, int(expiry)).encode())


async def connector_read(request, path):
    headers = {k: request.headers[k] for k in ("Authorization", "X-Local-User", "Origin") if k in request.headers}
    if not ("Authorization" in headers or "X-Local-User" in headers):
        raise web.HTTPUnauthorized()
    return await connector_request("GET", path, headers)


async def connector_request(method, path, headers, data=None, *, binary=None):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60 if binary is not None else 8)) as session:
            async with session.request(method, env.DS["baserow"]["url"].rstrip("/") + path, headers=headers, json=data, data=binary, allow_redirects=False) as response:
                if response.status == 401:
                    raise web.HTTPUnauthorized()
                if response.status == 403:
                    raise web.HTTPForbidden()
                if response.status == 404:
                    raise web.HTTPNotFound()
                if response.status == 409:
                    raise web.HTTPConflict(text="Conflicting connector record")
                if response.status != 200:
                    raise web.HTTPServiceUnavailable(text="Identity service unavailable")
                return await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        raise web.HTTPServiceUnavailable(text="Identity service unavailable")


def is_owner(principal, instance):
    owner = instance.variables.get("_owner_student_id")
    return bool(owner and owner == (principal["student"] or {}).get("id")
                and instance.variables.get("_owner_email") == principal["email"])
