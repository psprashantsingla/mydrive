"""MyDrive application entrypoint."""
import asyncio
import contextlib
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import api, config, db, mcp, storage, webdav
from .security import Principal
from .web import file_response

STATIC = Path(__file__).parent / "static"
APP_CSP = ("default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; frame-src 'self'; "
           "style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none'; base-uri 'none'; "
           "frame-ancestors 'none'")


async def _housekeeping():
    while True:
        with contextlib.suppress(Exception):
            await run_in_threadpool(storage.purge_old_trash)
            await run_in_threadpool(storage.cleanup_tmp)
            db.run("DELETE FROM sessions WHERE expires_at < ?", (db.now(),))
            await run_in_threadpool(db.backup)
        await asyncio.sleep(6 * 3600)


@contextlib.asynccontextmanager
async def lifespan(_app):
    config.ensure_dirs()
    db.conn()
    task = asyncio.create_task(_housekeeping())
    yield
    task.cancel()


app = FastAPI(title=config.APP_NAME, version=config.VERSION, lifespan=lifespan,
              docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
app.include_router(api.router)
app.include_router(webdav.router)
app.include_router(mcp.router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(storage.StorageError)
async def storage_error(_req, e: storage.StorageError):
    return JSONResponse({"detail": e.msg}, status_code=e.status)


@app.exception_handler(FileNotFoundError)
async def fnf_error(_req, _e):
    return JSONResponse({"detail": "Not found"}, status_code=404)


@app.middleware("http")
async def headers(request: Request, call_next):
    resp: Response = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return resp


@app.get("/", include_in_schema=False)
def index():
    r = FileResponse(STATIC / "index.html", media_type="text/html")
    r.headers["Content-Security-Policy"] = APP_CSP
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC / "icon.svg", media_type="image/svg+xml")


# ---------------------------------------------------------------- public share links
def _share(token: str) -> Principal:
    s = db.one("SELECT * FROM shares WHERE token=?", (token,))
    if not s:
        raise HTTPException(404, "This link does not exist or was revoked")
    if s["expires_at"] and s["expires_at"] < db.now():
        raise HTTPException(410, "This link has expired")
    u = db.one("SELECT * FROM users WHERE id=?", (s["user_id"],))
    if not u or u["disabled"]:
        raise HTTPException(404, "This link is no longer available")
    return Principal(u, scope=s["vpath"], can_write=False, via="share")


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


SHARE_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · MyDrive</title>
<link rel="icon" href="/static/icon.svg"><link rel="stylesheet" href="/static/style.css"></head>
<body class="share"><main class="share-card"><div class="brand"><img src="/static/icon.svg" alt="" width="28" height="28"> MyDrive</div>
{body}</main></body></html>"""


@app.get("/s/{token}", include_in_schema=False)
def share_page(token: str, path: str = ""):
    try:
        pr = _share(token)
    except HTTPException as e:
        return HTMLResponse(SHARE_PAGE.format(title="Link unavailable", body=f"<h1>{escape(e.detail)}</h1>"),
                            status_code=e.status_code)
    p, i = storage.stat_path(pr, path)
    base = f"/s/{quote(token)}"
    if not i["is_dir"]:
        q = f"?path={quote(i['path'])}" if i["path"] else ""
        sep = "&" if q else "?"
        preview = ""
        m = i["mime"] or ""
        src = f"{base}/dl{q}{sep}inline=1"
        if m.startswith("image/") and m != "image/svg+xml":
            preview = f'<img class="share-preview" src="{src}" alt="">'
        elif m.startswith("video/"):
            preview = f'<video class="share-preview" src="{src}" controls preload="metadata"></video>'
        elif m.startswith("audio/"):
            preview = f'<audio src="{src}" controls preload="metadata"></audio>'
        body = (f"<h1>{escape(p.name)}</h1><p class=muted>{_size(i['size'])}</p>{preview}"
                f'<p><a class="btn primary" href="{base}/dl{q}">Download</a></p>')
        r = HTMLResponse(SHARE_PAGE.format(title=escape(p.name), body=body))
    else:
        items = storage.list_dir(pr, path)
        rows = []
        if i["path"]:
            up = storage.parent_of(i["path"])
            rows.append(f'<li><a href="{base}?path={quote(up)}">⬆ ..</a></li>')
        for c in items:
            if c["is_dir"]:
                rows.append(f'<li><a href="{base}?path={quote(c["path"])}">📁 {escape(c["name"])}</a></li>')
            else:
                rows.append(f'<li><a href="{base}/dl?path={quote(c["path"])}">📄 {escape(c["name"])}</a>'
                            f'<span class=muted>{_size(c["size"])}</span></li>')
        title = storage.name_of(pr.scope) + ("/" + i["path"] if i["path"] else "")
        body = (f"<h1>📁 {escape(title)}</h1><ul class=share-list>{''.join(rows) or '<li class=muted>Empty folder</li>'}</ul>"
                f'<p><a class="btn primary" href="{base}/dl?path={quote(i["path"])}">Download all (.zip)</a></p>')
        r = HTMLResponse(SHARE_PAGE.format(title=escape(title), body=body))
    r.headers["Content-Security-Policy"] = APP_CSP
    return r


@app.get("/s/{token}/dl", include_in_schema=False)
def share_download(token: str, path: str = "", inline: bool = False):
    pr = _share(token)
    p, i = storage.stat_path(pr, path)
    if not inline:
        db.run("UPDATE shares SET downloads=downloads+1 WHERE token=?", (token,))
    if i["is_dir"]:
        from fastapi.responses import StreamingResponse
        name = storage.name_of(storage.full(pr, path)) or "files"
        return StreamingResponse(storage.iter_zip(pr, [path]), media_type="application/zip",
                                 headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.zip"})
    return file_response(p, p.name, inline)


@app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@app.get("/.well-known/oauth-protected-resource/{rest:path}", include_in_schema=False)
@app.get("/.well-known/oauth-authorization-server", include_in_schema=False)
def no_oauth(rest: str = ""):
    # MyDrive uses API keys, not OAuth. A clean 404 makes MCP clients fall back to the key.
    return JSONResponse({"detail": "OAuth is not used; authenticate with an API key"}, status_code=404)

