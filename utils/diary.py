"""Read the two bounded PDFs and scalar fields for the diary user task."""
from datetime import date
from hashlib import sha256

from aiohttp import web

TASK = "predavanje_dnevnika_student"
FILES = {"dnevnik_attachment": "dnevnik_prakse_upload", "potvrda_attachment": "ispunjena_potvrda_upload"}
MAX_PDF = 10 * 1024 * 1024


async def read_diary(request):
    if request.content_type != "multipart/form-data":
        raise web.HTTPBadRequest(text="Two PDF documents are required")
    fields = {}
    files = {}
    try:
        reader = await request.multipart()
        async for part in reader:
            key = part.name
            if key in fields or key in files or key not in {*FILES, "prijavljen_rok", "nastavak_radnog_odnosa"}:
                raise web.HTTPBadRequest(text="Unexpected diary field")
            limit = MAX_PDF if key in FILES else 32
            data = bytearray()
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > limit:
                    raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(data))
            if key in FILES:
                if not data.startswith(b"%PDF-"):
                    raise web.HTTPBadRequest(text="Expected a PDF document")
                files[key] = data
            else:
                fields[key] = data.decode("ascii")
        if files.keys() != FILES.keys() or fields.get("nastavak_radnog_odnosa") not in {"true", "false"}:
            raise ValueError()
        day = fields["prijavljen_rok"]
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError()
    except (KeyError, ValueError, AssertionError, AttributeError):
        raise web.HTTPBadRequest(text="Invalid diary form")
    receipt = sha256("|".join([day, fields["nastavak_radnog_odnosa"],
                               *(sha256(files[key]).hexdigest() for key in FILES)]).encode()).hexdigest()
    fields["nastavak_radnog_odnosa"] = fields["nastavak_radnog_odnosa"] == "true"
    return fields, files, receipt
