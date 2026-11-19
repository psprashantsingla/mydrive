"""JSON REST API used by the web app and by your own projects (Bearer API key)."""
import secrets
import shutil
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import config, db, security, storage
from .security import Principal
from .web import (COOKIE, client_ip, file_response, require_admin, require_session, require_user,
                  stream_to_file)

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------- auth
class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response):
    ip = client_ip(request)
    if security.too_many_failures(ip):
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
    u = db.one("SELECT * FROM users WHERE username=?", (body.username.strip().lower(),))
    if not u or u["disabled"] or not security.verify_password(body.password, u["pw_hash"]):
        security.record_failure(ip)
        raise HTTPException(401, "Wrong username or password")
    tok = security.create_session(u["id"], config.SESSION_DAYS, request.headers.get("user-agent", ""))
    response.set_cookie(COOKIE, tok, max_age=config.SESSION_DAYS * 86400, httponly=True,
                        secure=config.COOKIE_SECURE, samesite="lax", path="/")
    return {"ok": True}


@router.post("/logout")
def logout(request: Request, response: Response):
    tok = request.cookies.get(COOKIE)
    if tok:
        security.delete_session(tok)
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(pr: Principal = Depends(require_user)):
    disk = shutil.disk_usage(config.DATA_DIR)
    return {
        "username": pr.user["username"],
        "is_admin": bool(pr.user["is_admin"]),
        "quota_bytes": pr.user["quota_bytes"],
        "usage_bytes": storage.usage(pr.user),
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
        "scope": pr.scope,
        "can_write": pr.can_write,
        "via": pr.via,
        "base_url": config.BASE_URL,
        "trash_days": config.TRASH_DAYS,
        "version": config.VERSION,
    }


class PasswordIn(BaseModel):
    current: str
    new: str


@router.post("/me/password")
def change_password(body: PasswordIn, pr: Principal = Depends(require_session)):
    if not security.verify_password(body.current, pr.user["pw_hash"]):
        raise HTTPException(400, "Current password is wrong")
    try:
        security.validate_password(body.new)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.run("UPDATE users SET pw_hash=? WHERE id=?", (security.hash_password(body.new), pr.uid))
    security.clear_auth_cache()
    return {"ok": True}


# ---------------------------------------------------------------- files
@router.get("/files")
def list_files(path: str = "", pr: Principal = Depends(require_user)):
    return {"path": storage.norm(path), "items": storage.list_dir(pr, path)}


@router.get("/stat")
def stat(path: str = "", pr: Principal = Depends(require_user)):
    return storage.stat_path(pr, path)[1]


@router.get("/download")
def download(path: str, inline: bool = False, pr: Principal = Depends(require_user)):
    p, i = storage.stat_path(pr, path)
    if i["is_dir"]:
        return zip_download([path], pr)
    return file_response(p, p.name, inline)


@router.get("/zip")
def zip_download(paths: list[str] = Query(...), pr: Principal = Depends(require_user)):
    for p in paths:
        storage.stat_path(pr, p)  # 404 early
    name = (storage.name_of(storage.norm(paths[0])) or "files") if len(paths) == 1 else "files"
    return StreamingResponse(
        storage.iter_zip(pr, paths), media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.zip"},
    )


@router.put("/upload")
async def upload(request: Request, path: str, conflict: str = "rename", mtime: float | None = None,
                 pr: Principal = Depends(require_user)):
    if conflict not in ("rename", "overwrite", "error"):
        raise HTTPException(400, "conflict must be rename, overwrite or error")
    info, _ = await stream_to_file(pr, path, request, conflict, mtime)
    return info


class TextIn(BaseModel):
    path: str
    content: str
    conflict: str = "overwrite"


@router.post("/write")
def write_text(body: TextIn, pr: Principal = Depends(require_user)):
    return storage.write_bytes(pr, body.path, body.content.encode(), body.conflict)


class PathIn(BaseModel):
    path: str


@router.post("/mkdir")
def mkdir(body: PathIn, pr: Principal = Depends(require_user)):
    return storage.mkdir(pr, body.path)


class TransferIn(BaseModel):
    src: str
    dst: str
    overwrite: bool = False


@router.post("/move")
def move(body: TransferIn, pr: Principal = Depends(require_user)):
    return storage.move(pr, body.src, body.dst, body.overwrite)


@router.post("/copy")
def copy(body: TransferIn, pr: Principal = Depends(require_user)):
    return storage.copy(pr, body.src, body.dst, body.overwrite)


class PathsIn(BaseModel):
    paths: list[str]


@router.post("/delete")
def delete(body: PathsIn, pr: Principal = Depends(require_user)):
    return {"trashed": [storage.delete(pr, p) for p in body.paths]}


@router.get("/search")
def search(q: str, path: str = "", pr: Principal = Depends(require_user)):
    return {"items": storage.search(pr, q, path)}


# ---------------------------------------------------------------- trash
@router.get("/trash")
def trash(pr: Principal = Depends(require_session)):
    return {"items": storage.trash_list(pr.uid), "days": config.TRASH_DAYS}


@router.post("/trash/{tid}/restore")
def trash_restore(tid: int, pr: Principal = Depends(require_session)):
    return {"path": storage.trash_restore(pr.user, tid)}


@router.delete("/trash/{tid}")
def trash_delete(tid: int, pr: Principal = Depends(require_session)):
    return {"deleted": storage.trash_delete(pr.uid, tid)}


@router.delete("/trash")
def trash_empty(pr: Principal = Depends(require_session)):
    return {"deleted": storage.trash_delete(pr.uid)}


# ---------------------------------------------------------------- shares
class ShareIn(BaseModel):
    path: str
    expires_days: float | None = 7


def create_share(pr: Principal, path: str, expires_days: float | None) -> dict:
    if not pr.can_write:
        raise storage.StorageError(403, "This key is read-only")
    _, i = storage.stat_path(pr, path)
    fullp = storage.full(pr, path)
    if fullp == "":
        raise storage.StorageError(400, "Cannot share your whole drive")
    tok = secrets.token_urlsafe(12)
    exp = db.now() + int(expires_days * 86400) if expires_days else None
    sid = db.run(
        "INSERT INTO shares(token,user_id,vpath,is_dir,created_at,expires_at) VALUES(?,?,?,?,?,?)",
        (tok, pr.uid, fullp, int(i["is_dir"]), db.now(), exp),
    )
    return {"id": sid, "token": tok, "url": f"{config.BASE_URL}/s/{tok}", "path": fullp, "expires_at": exp}


@router.post("/shares")
def shares_create(body: ShareIn, pr: Principal = Depends(require_user)):
    return create_share(pr, body.path, body.expires_days)


@router.get("/shares")
def shares_list(pr: Principal = Depends(require_session)):
    rows = db.all("SELECT * FROM shares WHERE user_id=? ORDER BY created_at DESC", (pr.uid,))
    for r in rows:
        r["url"] = f"{config.BASE_URL}/s/{r['token']}"
        r["expired"] = bool(r["expires_at"] and r["expires_at"] < db.now())
    return {"items": rows}


@router.delete("/shares/{sid}")
def shares_delete(sid: int, pr: Principal = Depends(require_session)):
    db.run("DELETE FROM shares WHERE id=? AND user_id=?", (sid, pr.uid))
    return {"ok": True}


# ---------------------------------------------------------------- API keys
class KeyIn(BaseModel):
    name: str
    scope: str = ""
    can_write: bool = True


@router.get("/keys")
def keys_list(pr: Principal = Depends(require_session)):
    return {"items": db.all(
        "SELECT id,name,prefix,scope,can_write,created_at,last_used_at FROM api_keys WHERE user_id=? ORDER BY created_at DESC",
        (pr.uid,))}


@router.post("/keys")
def keys_create(body: KeyIn, pr: Principal = Depends(require_session)):
    name = body.name.strip()[:60]
    if not name:
        raise HTTPException(400, "Name is required")
    scope = storage.norm(body.scope)
    if scope:
        p = storage.resolve(pr.user, scope)
        if p.exists() and not p.is_dir():
            raise HTTPException(400, "Scope must be a folder")
        p.mkdir(parents=True, exist_ok=True)
    kid, key = security.create_api_key(pr.uid, name, scope, body.can_write)
    return {"id": kid, "key": key, "name": name, "scope": scope, "can_write": body.can_write,
            "base_url": config.BASE_URL}


@router.delete("/keys/{kid}")
def keys_delete(kid: int, pr: Principal = Depends(require_session)):
    db.run("DELETE FROM api_keys WHERE id=? AND user_id=?", (kid, pr.uid))
    return {"ok": True}


# ---------------------------------------------------------------- admin
class UserIn(BaseModel):
    username: str
    password: str
    is_admin: bool = False
    quota_gb: float | None = None


class UserPatch(BaseModel):
    password: str | None = None
    is_admin: bool | None = None
    quota_gb: float | None = None
    clear_quota: bool = False
    disabled: bool | None = None


def _gb(q):
    return int(q * 1024**3) if q else None


@router.get("/admin/users")
def users_list(pr: Principal = Depends(require_admin)):
    rows = db.all("SELECT id,username,is_admin,quota_bytes,disabled,created_at FROM users ORDER BY username")
    for r in rows:
        r["usage_bytes"] = storage.usage(r)
    return {"items": rows}


@router.post("/admin/users")
def users_create(body: UserIn, pr: Principal = Depends(require_admin)):
    return create_user(body.username, body.password, body.is_admin, _gb(body.quota_gb))


def create_user(username: str, password: str, is_admin: bool = False, quota_bytes: int | None = None) -> dict:
    username = username.strip().lower()
    if not security.USERNAME_RE.match(username) or username == config.SHARED_NAME.lower():
        raise HTTPException(400, "Username: 2-32 chars, lowercase letters, digits, . _ -")
    try:
        security.validate_password(password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if db.one("SELECT id FROM users WHERE username=?", (username,)):
        raise HTTPException(409, "Username already taken")
    uid = db.run(
        "INSERT INTO users(username,pw_hash,is_admin,quota_bytes,created_at) VALUES(?,?,?,?,?)",
        (username, security.hash_password(password), int(is_admin), quota_bytes, db.now()),
    )
    (config.USERS_DIR / username).mkdir(parents=True, exist_ok=True)
    return {"id": uid, "username": username}


@router.patch("/admin/users/{uid}")
def users_update(uid: int, body: UserPatch, pr: Principal = Depends(require_admin)):
    if not db.one("SELECT id FROM users WHERE id=?", (uid,)):
        raise HTTPException(404, "No such user")
    if uid == pr.uid and (body.disabled or body.is_admin is False):
        raise HTTPException(400, "You cannot disable or demote yourself")
    if body.password:
        try:
            security.validate_password(body.password)
        except ValueError as e:
            raise HTTPException(400, str(e))
        db.run("UPDATE users SET pw_hash=? WHERE id=?", (security.hash_password(body.password), uid))
        db.run("DELETE FROM sessions WHERE user_id=?", (uid,))
    if body.is_admin is not None:
        db.run("UPDATE users SET is_admin=? WHERE id=?", (int(body.is_admin), uid))
    if body.clear_quota:
        db.run("UPDATE users SET quota_bytes=NULL WHERE id=?", (uid,))
    elif body.quota_gb is not None:
        db.run("UPDATE users SET quota_bytes=? WHERE id=?", (_gb(body.quota_gb), uid))
    if body.disabled is not None:
        db.run("UPDATE users SET disabled=? WHERE id=?", (int(body.disabled), uid))
        if body.disabled:
            db.run("DELETE FROM sessions WHERE user_id=?", (uid,))
    security.clear_auth_cache()
    return {"ok": True}


@router.delete("/admin/users/{uid}")
def users_delete(uid: int, pr: Principal = Depends(require_admin)):
    if uid == pr.uid:
        raise HTTPException(400, "You cannot delete yourself")
    u = db.one("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        raise HTTPException(404, "No such user")
    storage.trash_delete(uid)
    db.run("DELETE FROM users WHERE id=?", (uid,))
    security.clear_auth_cache()
    return {"ok": True, "files_kept_at": str(config.USERS_DIR / u["username"])}


@router.get("/health")
def health():
    return {"ok": True, "version": config.VERSION}
