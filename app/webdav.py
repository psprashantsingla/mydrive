"""Minimal WebDAV server at /dav/ — mount the drive in Windows Explorer, macOS Finder,
iOS/Android file apps, or use it with rclone (vendor "other").

Auth: HTTP Basic with username + password, or username + API key (key scope applies)."""
import base64
import email.utils
import os
import secrets
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from xml.sax.saxutils import escape

from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from . import config, security, storage
from .security import Principal
from .web import client_ip, file_response, stream_to_file

router = APIRouter()
PREFIX = "/dav"
METHODS = ["OPTIONS", "PROPFIND", "PROPPATCH", "GET", "HEAD", "PUT", "DELETE", "MKCOL",
           "MOVE", "COPY", "LOCK", "UNLOCK"]


def _auth(request: Request) -> Principal | None:
    h = request.headers.get("authorization", "")
    if h[:6].lower() == "basic ":
        try:
            user, _, pw = base64.b64decode(h[6:]).decode().partition(":")
        except Exception:
            return None
        if pw.startswith(security.KEY_PREFIX):
            pr = security.key_principal(pw, via="basic")
            return pr if pr and pr.user["username"] == user.lower() else None
        ip = client_ip(request)
        if security.too_many_failures(ip):
            return None
        pr = security.password_principal(user, pw)
        if not pr:
            security.record_failure(ip)
        return pr
    if h[:7].lower() == "bearer ":
        return security.key_principal(h[7:].strip())
    return None


def _href(rel: str, is_dir: bool) -> str:
    h = PREFIX + "/" + quote(rel)
    if is_dir and not h.endswith("/"):
        h += "/"
    return h


def _http_date(ts: float) -> str:
    return email.utils.formatdate(ts, usegmt=True)


def _iso(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _prop_xml(i: dict, quota: tuple[int, int] | None) -> str:
    rel, d = i["path"], i["is_dir"]
    name = i["name"] or "MyDrive"
    parts = [
        f"<d:displayname>{escape(name)}</d:displayname>",
        f"<d:getlastmodified>{_http_date(i['modified'])}</d:getlastmodified>",
        f"<d:creationdate>{_iso(i['modified'])}</d:creationdate>",
        f'<d:getetag>"{i["modified"]:x}-{i["size"]:x}"</d:getetag>',
        "<d:supportedlock><d:lockentry><d:lockscope><d:exclusive/></d:lockscope>"
        "<d:locktype><d:write/></d:locktype></d:lockentry></d:supportedlock>",
    ]
    if d:
        parts.append("<d:resourcetype><d:collection/></d:resourcetype>")
        if quota:
            parts.append(f"<d:quota-used-bytes>{quota[0]}</d:quota-used-bytes>"
                         f"<d:quota-available-bytes>{quota[1]}</d:quota-available-bytes>")
    else:
        parts.append("<d:resourcetype/>")
        parts.append(f"<d:getcontentlength>{i['size']}</d:getcontentlength>")
        parts.append(f"<d:getcontenttype>{escape(i['mime'] or '')}</d:getcontenttype>")
    return (f"<d:response><d:href>{escape(_href(rel, d))}</d:href><d:propstat><d:prop>"
            + "".join(parts) + "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")


def _multistatus(body: str) -> Response:
    xml = '<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:">' + body + "</d:multistatus>"
    return Response(xml, status_code=207, media_type='application/xml; charset="utf-8"')


def _dest_rel(request: Request) -> str | None:
    dest = request.headers.get("destination")
    if not dest:
        return None
    path = unquote(urlparse(dest).path)
    if not (path == PREFIX or path.startswith(PREFIX + "/")):
        return None
    return storage.norm(path[len(PREFIX):])


def _propfind(pr: Principal, rel: str, depth: str) -> Response:
    p, i = storage.stat_path(pr, rel)
    quota = None
    if i["is_dir"]:
        used = storage.usage(pr.user)
        quota = (used, storage.space_left(pr.user, storage.full(pr, rel)))
    out = [_prop_xml(i, quota)]
    if i["is_dir"] and depth != "0":
        for c in storage.list_dir(pr, rel):
            out.append(_prop_xml(c, None))
    return _multistatus("".join(out))


@router.api_route(PREFIX, methods=METHODS, include_in_schema=False)
@router.api_route(PREFIX + "/{path:path}", methods=METHODS, include_in_schema=False)
async def dav(request: Request, path: str = ""):
    m = request.method
    if m == "OPTIONS":
        return Response(headers={"DAV": "1, 2", "MS-Author-Via": "DAV", "Allow": ", ".join(METHODS)})
    pr = await run_in_threadpool(_auth, request)
    if not pr:
        return Response("Authentication required", status_code=401,
                        headers={"WWW-Authenticate": f'Basic realm="{config.APP_NAME}", charset="UTF-8"'})
    try:
        rel = storage.norm(unquote(path))
    except storage.StorageError as e:
        return Response(e.msg, status_code=e.status)
    try:
        return await _handle(request, pr, m, rel)
    except storage.StorageError as e:
        code = {412: 412, 507: 507, 404: 404, 403: 403, 409: 409}.get(e.status, 400)
        return Response(e.msg, status_code=code)
    except FileNotFoundError:
        return Response("Not found", status_code=404)


async def _handle(request: Request, pr: Principal, m: str, rel: str) -> Response:
    if m == "PROPFIND":
        await request.body()
        return await run_in_threadpool(_propfind, pr, rel, request.headers.get("depth", "1"))

    if m == "PROPPATCH":
        await request.body()
        storage.stat_path(pr, rel)
        return _multistatus(f"<d:response><d:href>{escape(_href(rel, False))}</d:href><d:propstat><d:prop/>"
                            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")

    if m in ("GET", "HEAD"):
        p, i = storage.stat_path(pr, rel)
        if i["is_dir"]:
            items = storage.list_dir(pr, rel)
            links = "".join(f'<li><a href="{escape(_href(c["path"], c["is_dir"]))}">{escape(c["name"])}'
                            f'{"/" if c["is_dir"] else ""}</a></li>' for c in items)
            return Response(f"<!doctype html><meta charset=utf-8><title>{escape(rel or '/')}</title>"
                            f"<h3>/{escape(rel)}</h3><ul>{links}</ul>", media_type="text/html")
        return file_response(p, p.name, inline=False)

    if m == "PUT":
        mtime = request.headers.get("x-oc-mtime")
        info, created = await stream_to_file(pr, rel, request, "overwrite", float(mtime) if mtime else None)
        h = {"ETag": f'"{info["modified"]:x}-{info["size"]:x}"'}
        if mtime:
            h["X-OC-Mtime"] = "accepted"
        return Response(status_code=201 if created else 204, headers=h)

    if m == "DELETE":
        await run_in_threadpool(storage.delete, pr, rel)
        return Response(status_code=204)

    if m == "MKCOL":
        if await request.body():
            return Response(status_code=415)
        p = storage.resolve(pr.user, storage.full(pr, rel))
        if p.exists():
            return Response(status_code=405)
        if not p.parent.is_dir():
            return Response(status_code=409)
        storage.mkdir(pr, rel)
        return Response(status_code=201)

    if m in ("MOVE", "COPY"):
        dst = _dest_rel(request)
        if dst is None:
            return Response("Bad Destination", status_code=400)
        overwrite = request.headers.get("overwrite", "T").upper() != "F"
        dp = storage.resolve(pr.user, storage.full(pr, dst))
        existed = os.path.lexists(dp)
        fn = storage.move if m == "MOVE" else storage.copy
        await run_in_threadpool(fn, pr, rel, dst, overwrite)
        return Response(status_code=204 if existed else 201)

    if m == "LOCK":
        await request.body()
        # Fake exclusive lock: enough for Finder / Explorer / Office to save files.
        p = storage.resolve(pr.user, storage.full(pr, rel))
        created = False
        if not p.exists():
            await run_in_threadpool(storage.write_bytes, pr, rel, b"", "error")
            created = True
        token = f"opaquelocktoken:{secrets.token_hex(16)}"
        timeout = request.headers.get("timeout", "Second-3600")
        xml = ('<?xml version="1.0" encoding="utf-8"?><d:prop xmlns:d="DAV:"><d:lockdiscovery><d:activelock>'
               "<d:locktype><d:write/></d:locktype><d:lockscope><d:exclusive/></d:lockscope>"
               f"<d:depth>{escape(request.headers.get('depth', '0'))}</d:depth>"
               f"<d:owner>{escape(pr.user['username'])}</d:owner><d:timeout>{escape(timeout)}</d:timeout>"
               f"<d:locktoken><d:href>{token}</d:href></d:locktoken>"
               f"<d:lockroot><d:href>{escape(_href(rel, False))}</d:href></d:lockroot>"
               "</d:activelock></d:lockdiscovery></d:prop>")
        return Response(xml, status_code=201 if created else 200, media_type='application/xml; charset="utf-8"',
                        headers={"Lock-Token": f"<{token}>"})

    if m == "UNLOCK":
        return Response(status_code=204)

    return Response(status_code=405)
