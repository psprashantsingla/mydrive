"""Shared HTTP helpers: authentication dependencies, safe file responses, streaming uploads."""
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from . import storage
from .security import Principal, key_principal, session_principal

COOKIE = "md_session"
CSRF_HEADER = "x-requested-with"

SAFE_INLINE = {
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp",
    "application/pdf", "text/plain",
}
TEXTY = {
    "application/json", "application/xml", "application/javascript", "application/x-sh",
    "application/x-yaml", "application/yaml", "application/toml", "application/sql",
}
TEXT_EXT = {".md", ".log", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env", ".py", ".ts",
            ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".rb", ".php", ".sql",
            ".csv", ".tsv", ".sh", ".txt", ".json", ".xml", ".html", ".css", ".js", ".svg", ".vue"}


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def principal_from_request(request: Request) -> Principal | None:
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return key_principal(auth[7:].strip())
    tok = request.cookies.get(COOKIE)
    if tok:
        pr = session_principal(tok)
        if pr and request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "mydrive":
            raise HTTPException(403, "Missing X-Requested-With header")
        return pr
    return None


def require_user(request: Request) -> Principal:
    pr = principal_from_request(request)
    if not pr:
        raise HTTPException(401, "Not signed in or invalid API key")
    return pr


def require_session(request: Request) -> Principal:
    pr = require_user(request)
    if pr.via != "session":
        raise HTTPException(403, "This action is only available in the web app")
    return pr


def require_admin(request: Request) -> Principal:
    pr = require_session(request)
    if not pr.is_admin:
        raise HTTPException(403, "Admins only")
    return pr


def is_texty(name: str, m: str) -> bool:
    return m.startswith("text/") or m in TEXTY or Path(name).suffix.lower() in TEXT_EXT


def file_response(p: Path, name: str, inline: bool = False) -> FileResponse:
    """Serve a stored file. User content is never rendered as active HTML/SVG on our origin."""
    m = storage.mime(name)
    media = m
    if inline:
        if m in SAFE_INLINE or m.startswith(("video/", "audio/")):
            pass
        elif is_texty(name, m):
            media = "text/plain; charset=utf-8"
        else:
            inline = False
    resp = FileResponse(
        p, media_type=media, filename=name,
        content_disposition_type="inline" if inline else "attachment",
    )
    if media != "application/pdf":
        resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'; media-src 'self'"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
    return resp


async def stream_to_file(pr: Principal, rel: str, request: Request, conflict: str = "overwrite",
                         mtime: float | None = None) -> tuple[dict, bool]:
    """Stream the request body into rel. Returns (info, created_new)."""
    size_hint = int(request.headers.get("content-length") or 0)
    final, tmp, final_rel, allowed = await run_in_threadpool(storage.prepare_write, pr, rel, conflict, size_hint)
    existed = final.exists()
    f = await run_in_threadpool(open, tmp, "wb")
    try:
        written, buf = 0, bytearray()
        async for chunk in request.stream():
            written += len(chunk)
            if written > allowed:
                raise storage.StorageError(507, "Not enough storage space (quota or disk full)")
            buf += chunk
            if len(buf) >= 1024 * 1024:
                data, buf = bytes(buf), bytearray()
                await run_in_threadpool(f.write, data)
        if buf:
            await run_in_threadpool(f.write, bytes(buf))
        await run_in_threadpool(f.close)
        res = await run_in_threadpool(storage.finish_write, pr, tmp, final, final_rel, mtime)
        return res, not existed
    finally:
        if not f.closed:
            f.close()
        if tmp.exists():
            tmp.unlink()
