"""MCP (Model Context Protocol) server over Streamable HTTP, stateless JSON responses.

Connect from Claude:
  * Claude Code:  claude mcp add --transport http mydrive https://HOST/mcp --header "Authorization: Bearer mdk_..."
  * claude.ai / Claude Desktop "custom connector": https://HOST/mcp/mdk_...   (key in the URL)

Each API key is confined to its scope folder and may be read-only."""
import base64
import json

from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from . import config, security, storage
from .security import Principal

router = APIRouter()
PROTOCOLS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

INSTRUCTIONS = (
    "MyDrive is the user's personal cloud storage. Paths are relative to this connection's root "
    "folder and use '/' separators; '' is the root. Deleted items go to the trash (restorable for "
    f"{config.TRASH_DAYS} days in the web app). Prefer list_files/search before reading or writing, "
    "and never overwrite or delete files the user did not ask you to change."
)


def _s(desc, **props):
    return {"type": "object", "properties": props, "required": [k for k, v in props.items() if v.pop("_req", False)],
            "additionalProperties": False}


def _p(t, desc, req=False, **extra):
    d = {"type": t, "description": desc, **extra}
    if req:
        d["_req"] = True
    return d


TOOLS = [
    {"name": "list_files", "title": "List folder",
     "description": "List the files and folders inside a folder.",
     "inputSchema": _s("", path=_p("string", "Folder path, '' for root")),
     "annotations": {"readOnlyHint": True}},
    {"name": "read_file", "title": "Read file",
     "description": "Read a file. Text is returned as text (paged with offset/max_bytes); small images are returned as images.",
     "inputSchema": _s("", path=_p("string", "File path", True),
                       offset=_p("integer", "Byte offset to start at (default 0)"),
                       max_bytes=_p("integer", f"Max bytes to return (default and max {config.MCP_MAX_READ})")),
     "annotations": {"readOnlyHint": True}},
    {"name": "write_file", "title": "Write file",
     "description": "Create or update a file. Parent folders are created automatically.",
     "inputSchema": _s("", path=_p("string", "File path", True),
                       content=_p("string", "File content (text, or base64 when encoding='base64')", True),
                       encoding=_p("string", "text (default) or base64", enum=["text", "base64"]),
                       mode=_p("string", "overwrite (default), append, or create (fail if it exists)",
                               enum=["overwrite", "append", "create"])),
     "annotations": {"readOnlyHint": False, "destructiveHint": True}},
    {"name": "create_folder", "title": "Create folder",
     "description": "Create a folder (and any missing parents).",
     "inputSchema": _s("", path=_p("string", "Folder path", True)),
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}},
    {"name": "move", "title": "Move / rename",
     "description": "Move or rename a file or folder. 'destination' is the full new path.",
     "inputSchema": _s("", source=_p("string", "Current path", True), destination=_p("string", "New path", True)),
     "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "copy", "title": "Copy",
     "description": "Copy a file or folder to a new path.",
     "inputSchema": _s("", source=_p("string", "Path to copy", True), destination=_p("string", "New path", True)),
     "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "delete", "title": "Move to trash",
     "description": "Move a file or folder to the trash (the user can restore it from the web app).",
     "inputSchema": _s("", path=_p("string", "Path to delete", True)),
     "annotations": {"readOnlyHint": False, "destructiveHint": True}},
    {"name": "search", "title": "Search by name",
     "description": "Find files and folders whose name contains the query (case-insensitive).",
     "inputSchema": _s("", query=_p("string", "Text to look for in names", True),
                       path=_p("string", "Folder to search in, '' for everything")),
     "annotations": {"readOnlyHint": True}},
    {"name": "get_info", "title": "File info",
     "description": "Get size, type and modification time of a file or folder.",
     "inputSchema": _s("", path=_p("string", "Path", True)),
     "annotations": {"readOnlyHint": True}},
    {"name": "create_share_link", "title": "Create share link",
     "description": "Create a public download link for a file or folder. Only do this when the user asks to share.",
     "inputSchema": _s("", path=_p("string", "Path to share", True),
                       expires_days=_p("number", "Days until the link stops working (default 7, 0 = never)")),
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}},
    {"name": "storage_usage", "title": "Storage usage",
     "description": "Show how much storage is used and available.",
     "inputSchema": _s(""), "annotations": {"readOnlyHint": True}},
]


def _fmt(i: dict) -> dict:
    return {k: i[k] for k in ("path", "is_dir", "size", "modified", "mime") if k in i}


def _text(obj) -> list[dict]:
    return [{"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj, indent=1)}]


def call_tool(pr: Principal, name: str, a: dict) -> list[dict]:
    if name == "list_files":
        items = storage.list_dir(pr, a.get("path", ""))
        return _text({"path": storage.norm(a.get("path", "")), "items": [_fmt(i) for i in items]})

    if name == "read_file":
        p, i = storage.stat_path(pr, a["path"])
        if i["is_dir"]:
            raise storage.StorageError(400, "That is a folder; use list_files")
        if i["mime"] in IMAGE_MIMES and i["size"] <= 4 * 1024 * 1024:
            return [{"type": "image", "mimeType": i["mime"], "data": base64.b64encode(p.read_bytes()).decode()},
                    *_text(_fmt(i))]
        offset = max(0, int(a.get("offset") or 0))
        n = min(int(a.get("max_bytes") or config.MCP_MAX_READ), config.MCP_MAX_READ)
        with open(p, "rb") as f:
            f.seek(offset)
            data = f.read(n)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as e:
            if e.start >= len(data) - 3 and offset + len(data) < i["size"]:
                data = data[:e.start]  # cut in the middle of a multi-byte char
                text = data.decode("utf-8")
            else:
                return _text(f"{a['path']} is a binary file ({i['mime']}, {i['size']} bytes) and cannot be shown "
                             "as text. Use create_share_link if the user wants to download it.")
        end = offset + len(data)
        if offset or end < i["size"]:
            text = f"[bytes {offset}-{end} of {i['size']}; call again with offset={end} for more]\n" + text
        return _text(text)

    if name == "write_file":
        enc, mode = a.get("encoding", "text"), a.get("mode", "overwrite")
        data = base64.b64decode(a["content"]) if enc == "base64" else a["content"].encode()
        if mode == "append":
            p = storage.resolve(pr.user, storage.full(pr, a["path"]))
            if p.is_file():
                if p.stat().st_size > 50 * 1024 * 1024:
                    raise storage.StorageError(400, "File too large to append to")
                data = p.read_bytes() + data
        i = storage.write_bytes(pr, a["path"], data, "error" if mode == "create" else "overwrite")
        return _text({"written": _fmt(i)})

    if name == "create_folder":
        return _text({"created": _fmt(storage.mkdir(pr, a["path"]))})
    if name == "move":
        return _text({"moved_to": _fmt(storage.move(pr, a["source"], a["destination"]))})
    if name == "copy":
        return _text({"copied_to": _fmt(storage.copy(pr, a["source"], a["destination"]))})
    if name == "delete":
        storage.delete(pr, a["path"])
        return _text(f"Moved {a['path']} to the trash.")
    if name == "search":
        return _text({"items": [_fmt(i) for i in storage.search(pr, a["query"], a.get("path", ""), 100)]})
    if name == "get_info":
        return _text(_fmt(storage.stat_path(pr, a["path"])[1]))
    if name == "create_share_link":
        from .api import create_share
        days = a.get("expires_days", 7)
        s = create_share(pr, a["path"], days or None)
        return _text({"url": s["url"], "expires_at": s["expires_at"]})
    if name == "storage_usage":
        q = pr.user["quota_bytes"]
        return _text({"used_bytes": storage.usage(pr.user), "quota_bytes": q,
                      "available_bytes": storage.space_left(pr.user, storage.full(pr, ""))})
    raise KeyError(name)


def _handle(pr: Principal, msg: dict) -> dict | None:
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None  # notification

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    if method == "initialize":
        v = params.get("protocolVersion")
        return ok({
            "protocolVersion": v if v in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "mydrive", "title": config.APP_NAME, "version": config.VERSION},
            "instructions": INSTRUCTIONS + (" This connection is READ-ONLY." if not pr.can_write else ""),
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        tools = TOOLS if pr.can_write else [t for t in TOOLS if t["annotations"].get("readOnlyHint")]
        return ok({"tools": tools})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        try:
            return ok({"content": call_tool(pr, name, args), "isError": False})
        except KeyError as e:
            if name not in {t["name"] for t in TOOLS}:
                return err(-32602, f"Unknown tool: {name}")
            return ok({"content": _text(f"Missing argument: {e}"), "isError": True})
        except storage.StorageError as e:
            return ok({"content": _text(f"Error: {e.msg}"), "isError": True})
        except (OSError, ValueError) as e:
            return ok({"content": _text(f"Error: {e}"), "isError": True})
    if method in ("resources/list", "prompts/list", "resources/templates/list"):
        return ok({method.split("/")[0]: [], } if method != "resources/templates/list" else {"resourceTemplates": []})
    return err(-32601, f"Method not found: {method}")


async def _endpoint(request: Request, key: str | None):
    if request.method != "POST":
        return Response(status_code=405, headers={"Allow": "POST"})
    if key is None:
        h = request.headers.get("authorization", "")
        key = h[7:].strip() if h[:7].lower() == "bearer " else ""
    pr = await run_in_threadpool(security.key_principal, key) if key else None
    if not pr:
        return Response('{"error":"invalid or missing API key"}', status_code=401, media_type="application/json")
    try:
        body = json.loads(await request.body())
    except ValueError:
        return Response(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}),
                        status_code=400, media_type="application/json")
    msgs = body if isinstance(body, list) else [body]
    out = []
    for m in msgs:
        if isinstance(m, dict):
            r = await run_in_threadpool(_handle, pr, m)
            if r is not None:
                out.append(r)
    if not out:
        return Response(status_code=202)
    return Response(json.dumps(out if isinstance(body, list) else out[0]), media_type="application/json")


@router.api_route("/mcp", methods=["POST", "GET", "DELETE"], include_in_schema=False)
async def mcp_header_auth(request: Request):
    return await _endpoint(request, None)


@router.api_route("/mcp/{key}", methods=["POST", "GET", "DELETE"], include_in_schema=False)
async def mcp_url_auth(request: Request, key: str):
    return await _endpoint(request, key)
