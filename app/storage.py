"""File storage: maps virtual paths to disk, enforces scope/permissions/quota, trash.

Virtual layout seen by a user (unscoped):
    /                 -> /data/files/users/<username>/
    /Shared/...       -> /data/files/shared/...   (family folder, everyone can read/write)

An API key with scope "projects/blog" sees that folder as its root "/".
"""
import mimetypes
import os
import secrets
import shutil
import stat
import threading
import time
import zipfile
from pathlib import Path

from . import config, db
from .security import Principal

SHARED = config.SHARED_NAME
TMP_PREFIX = ".mdpart-"


class StorageError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status
        self.msg = msg


# ---------------------------------------------------------------- paths
def norm(p: str | None) -> str:
    if not p:
        return ""
    parts = []
    for seg in p.replace("\\", "/").split("/"):
        if seg in ("", "."):
            continue
        if seg == ".." or "\0" in seg or len(seg.encode()) > 255:
            raise StorageError(400, "Invalid path")
        parts.append(seg)
    return "/".join(parts)


def join(a: str, b: str) -> str:
    return f"{a}/{b}" if a and b else (a or b)


def parent_of(p: str) -> str:
    return p.rsplit("/", 1)[0] if "/" in p else ""


def name_of(p: str) -> str:
    return p.rsplit("/", 1)[-1]


def full(pr: Principal, rel: str) -> str:
    return join(pr.scope, norm(rel))


def rel_of(pr: Principal, fullp: str) -> str:
    if not pr.scope:
        return fullp
    if fullp == pr.scope:
        return ""
    return fullp[len(pr.scope) + 1:]


def home_dir(user: dict) -> Path:
    return config.USERS_DIR / user["username"]


def is_shared(fullp: str) -> bool:
    return fullp == SHARED or fullp.startswith(SHARED + "/")


def resolve(user: dict, fullp: str) -> Path:
    parts = fullp.split("/") if fullp else []
    if parts and parts[0] == SHARED:
        base, rest = config.SHARED_DIR, parts[1:]
    else:
        base, rest = home_dir(user), parts
        base.mkdir(parents=True, exist_ok=True)
    p = base.joinpath(*rest)
    real = os.path.realpath(p)
    if real != str(base) and not real.startswith(str(base) + os.sep):
        raise StorageError(403, "Path is outside your storage")
    return p


def mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def info(p: Path, rel: str, st: os.stat_result | None = None) -> dict:
    st = st or p.stat()
    d = stat.S_ISDIR(st.st_mode)
    return {
        "name": p.name if rel else "",
        "path": rel,
        "is_dir": d,
        "size": 0 if d else st.st_size,
        "modified": int(st.st_mtime),
        "mime": None if d else mime(p.name),
    }


def _guard_write(pr: Principal, rel: str) -> str:
    if not pr.can_write:
        raise StorageError(403, "This key is read-only")
    fullp = full(pr, rel)
    if norm(rel) == "" or fullp in ("", SHARED):
        raise StorageError(400, "This folder cannot be modified")
    return fullp


def unique_path(p: Path) -> Path:
    if not os.path.lexists(p):
        return p
    stem, suffix = (p.name, "") if p.is_dir() else (p.stem, p.suffix)
    i = 1
    while True:
        c = p.with_name(f"{stem} ({i}){suffix}")
        if not os.path.lexists(c):
            return c
        i += 1


# ---------------------------------------------------------------- quota
_usage: dict[int, list] = {}
_usage_lock = threading.Lock()


def dir_size(path: Path) -> int:
    total, stack = 0, [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def path_size(p: Path) -> int:
    try:
        return dir_size(p) if p.is_dir() else p.stat().st_size
    except OSError:
        return 0


def usage(user: dict, fresh: bool = False) -> int:
    """Bytes in the user's home + trash (Shared is not counted against anyone)."""
    uid = user["id"]
    with _usage_lock:
        hit = _usage.get(uid)
    if hit and not fresh and time.time() - hit[1] < 600:
        return hit[0]
    total = dir_size(home_dir(user)) + dir_size(config.TRASH_DIR / str(uid))
    with _usage_lock:
        _usage[uid] = [total, time.time()]
    return total


def add_usage(uid: int, delta: int) -> None:
    with _usage_lock:
        if uid in _usage:
            _usage[uid][0] = max(0, _usage[uid][0] + delta)


def space_left(user: dict, fullp: str) -> int:
    """How many more bytes may be written to fullp."""
    left = shutil.disk_usage(config.DATA_DIR).free - config.MIN_FREE_BYTES
    if user.get("quota_bytes") and not is_shared(fullp):
        left = min(left, user["quota_bytes"] - usage(user))
    return max(0, left)


def check_space(user: dict, fullp: str, add: int) -> None:
    if add > space_left(user, fullp):
        raise StorageError(507, "Not enough storage space (quota or disk full)")


# ---------------------------------------------------------------- read
def stat_path(pr: Principal, rel: str) -> tuple[Path, dict]:
    rel = norm(rel)
    fullp = full(pr, rel)
    p = resolve(pr.user, fullp)
    try:
        return p, info(p, rel)
    except FileNotFoundError:
        raise StorageError(404, "Not found")


def list_dir(pr: Principal, rel: str) -> list[dict]:
    rel = norm(rel)
    fullp = full(pr, rel)
    p = resolve(pr.user, fullp)
    if not p.is_dir():
        raise StorageError(404 if not p.exists() else 400, "Folder not found")
    out = []
    with os.scandir(p) as it:
        for e in it:
            if e.name.startswith(TMP_PREFIX) or e.is_symlink():
                continue
            try:
                out.append(info(Path(e.path), join(rel, e.name), e.stat()))
            except OSError:
                continue
    if fullp == "":
        sd = config.SHARED_DIR
        out = [x for x in out if x["name"] != SHARED]
        out.append(info(sd, SHARED) | {"name": SHARED, "shared": True})
    out.sort(key=lambda x: (not x["is_dir"], x["name"].casefold()))
    return out


def search(pr: Principal, q: str, rel: str = "", limit: int = 200) -> list[dict]:
    q = q.casefold().strip()
    if not q:
        return []
    rel = norm(rel)
    roots = [rel]
    if full(pr, rel) == "":
        roots.append(SHARED)
    out, deadline = [], time.time() + 8
    for root in roots:
        stack = [(resolve(pr.user, full(pr, root)), root)]
        while stack and len(out) < limit and time.time() < deadline:
            d, drel = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if e.name.startswith(TMP_PREFIX) or e.is_symlink():
                            continue
                        erel = join(drel, e.name)
                        if full(pr, drel) == "" and e.name == SHARED:
                            continue
                        is_dir = e.is_dir()
                        if q in e.name.casefold():
                            out.append(info(Path(e.path), erel, e.stat()))
                        if is_dir:
                            stack.append((Path(e.path), erel))
            except OSError:
                continue
    return out[:limit]


def iter_zip(pr: Principal, rels: list[str]):
    """Stream a zip (stored, no compression -> cheap on CPU) of the given paths."""

    class Sink:
        def __init__(self):
            self.buf = bytearray()
            self.pos = 0

        def write(self, b):
            self.buf += b
            self.pos += len(b)
            return len(b)

        def tell(self):
            return self.pos

        def flush(self):
            pass

        def take(self):
            b = bytes(self.buf)
            self.buf.clear()
            return b

    sink = Sink()
    zf = zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True)
    for rel in rels:
        rel = norm(rel)
        p = resolve(pr.user, full(pr, rel))
        base = p.parent
        items = [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file() and not x.is_symlink())
        for f in items:
            if f.name.startswith(TMP_PREFIX):
                continue
            arc = str(f.relative_to(base))
            zi = zipfile.ZipInfo.from_file(f, arc)
            zi.compress_type = zipfile.ZIP_STORED
            with open(f, "rb") as src, zf.open(zi, "w", force_zip64=True) as dst:
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)
                    if len(sink.buf) >= 1024 * 1024:
                        yield sink.take()
            yield sink.take()
    zf.close()
    yield sink.take()


# ---------------------------------------------------------------- write
def mkdir(pr: Principal, rel: str) -> dict:
    fullp = _guard_write(pr, rel)
    p = resolve(pr.user, fullp)
    if p.exists() and not p.is_dir():
        raise StorageError(409, "A file with that name already exists")
    p.mkdir(parents=True, exist_ok=True)
    return info(p, norm(rel))


def prepare_write(pr: Principal, rel: str, conflict: str = "overwrite", size_hint: int = 0):
    """Returns (final_path, tmp_path, final_rel, bytes_allowed)."""
    fullp = _guard_write(pr, rel)
    p = resolve(pr.user, fullp)
    if p.is_dir():
        raise StorageError(409, "A folder with that name already exists")
    if p.exists():
        if conflict == "error":
            raise StorageError(409, "File already exists")
        if conflict == "rename":
            p = unique_path(p)
    if not p.parent.is_dir():
        if p.parent.exists():
            raise StorageError(409, "Parent is not a folder")
        p.parent.mkdir(parents=True, exist_ok=True)
    allowed = space_left(pr.user, fullp)
    if size_hint > allowed:
        raise StorageError(507, "Not enough storage space (quota or disk full)")
    tmp = p.parent / f"{TMP_PREFIX}{secrets.token_hex(6)}"
    final_rel = join(parent_of(norm(rel)), p.name)
    return p, tmp, final_rel, allowed


def finish_write(pr: Principal, tmp: Path, final: Path, final_rel: str, mtime: float | None = None) -> dict:
    old = final.stat().st_size if final.is_file() else 0
    os.replace(tmp, final)
    if mtime:
        os.utime(final, (mtime, mtime))
    size = final.stat().st_size
    if not is_shared(full(pr, final_rel)):
        add_usage(pr.uid, size - old)
    return info(final, final_rel)


def write_bytes(pr: Principal, rel: str, data: bytes, conflict: str = "overwrite") -> dict:
    final, tmp, final_rel, allowed = prepare_write(pr, rel, conflict, len(data))
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        return finish_write(pr, tmp, final, final_rel)
    finally:
        if tmp.exists():
            tmp.unlink()


def delete(pr: Principal, rel: str) -> int:
    """Move to the deleting user's trash. Returns trash id."""
    fullp = _guard_write(pr, rel)
    p = resolve(pr.user, fullp)
    if not os.path.lexists(p):
        raise StorageError(404, "Not found")
    is_dir = p.is_dir()
    size = path_size(p)
    tdir = config.TRASH_DIR / str(pr.uid)
    tdir.mkdir(parents=True, exist_ok=True)
    tid = db.run(
        "INSERT INTO trash(user_id,vpath,stored,is_dir,size,deleted_at) VALUES(?,?,?,?,?,?)",
        (pr.uid, fullp, "", int(is_dir), size, db.now()),
    )
    stored = f"{tid}-{secrets.token_hex(4)}"
    shutil.move(str(p), str(tdir / stored))
    db.run("UPDATE trash SET stored=? WHERE id=?", (stored, tid))
    if is_shared(fullp):
        add_usage(pr.uid, size)
    return tid


def _transfer(pr: Principal, src: str, dst: str, overwrite: bool, copy: bool) -> dict:
    sfull = full(pr, src) if copy else _guard_write(pr, src)
    if not pr.can_write:
        raise StorageError(403, "This key is read-only")
    dfull = _guard_write(pr, dst)
    if dfull == sfull:
        raise StorageError(400, "Source and destination are the same")
    if dfull.startswith(sfull + "/"):
        raise StorageError(400, "Cannot put a folder inside itself")
    sp, dp = resolve(pr.user, sfull), resolve(pr.user, dfull)
    if not os.path.lexists(sp):
        raise StorageError(404, "Source not found")
    if not dp.parent.is_dir():
        raise StorageError(409, "Destination folder does not exist")
    if os.path.lexists(dp):
        if not overwrite:
            raise StorageError(412, "Destination already exists")
        delete(pr, dst)
    crosses = is_shared(sfull) != is_shared(dfull)
    size = path_size(sp) if (copy or crosses) else 0
    if copy or (crosses and not is_shared(dfull)):
        check_space(pr.user, dfull, size)
    if copy:
        if sp.is_dir():
            shutil.copytree(sp, dp, symlinks=False, ignore=shutil.ignore_patterns(TMP_PREFIX + "*"))
        else:
            shutil.copy2(sp, dp)
        if not is_shared(dfull):
            add_usage(pr.uid, size)
    else:
        shutil.move(str(sp), str(dp))
        if crosses:
            add_usage(pr.uid, size if is_shared(sfull) else -size)
    return info(dp, norm(dst))


def move(pr, src, dst, overwrite=False):
    return _transfer(pr, src, dst, overwrite, copy=False)


def copy(pr, src, dst, overwrite=False):
    return _transfer(pr, src, dst, overwrite, copy=True)


# ---------------------------------------------------------------- trash
def trash_list(uid: int) -> list[dict]:
    return db.all("SELECT id,vpath,is_dir,size,deleted_at FROM trash WHERE user_id=? ORDER BY deleted_at DESC", (uid,))


def trash_restore(user: dict, tid: int) -> str:
    row = db.one("SELECT * FROM trash WHERE id=? AND user_id=?", (tid, user["id"]))
    if not row:
        raise StorageError(404, "Not in trash")
    src = config.TRASH_DIR / str(user["id"]) / row["stored"]
    dest = resolve(user, row["vpath"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest = unique_path(dest)
    shutil.move(str(src), str(dest))
    db.run("DELETE FROM trash WHERE id=?", (tid,))
    if is_shared(row["vpath"]):
        add_usage(user["id"], -row["size"])
    return join(parent_of(row["vpath"]), dest.name)


def _trash_purge_row(row: dict) -> None:
    p = config.TRASH_DIR / str(row["user_id"]) / row["stored"]
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, ignore_errors=True)
    elif os.path.lexists(p):
        p.unlink()
    db.run("DELETE FROM trash WHERE id=?", (row["id"],))
    add_usage(row["user_id"], -row["size"])


def trash_delete(uid: int, tid: int | None = None) -> int:
    if tid is None:
        rows = db.all("SELECT * FROM trash WHERE user_id=?", (uid,))
    else:
        rows = db.all("SELECT * FROM trash WHERE user_id=? AND id=?", (uid, tid))
    for r in rows:
        _trash_purge_row(r)
    return len(rows)


def purge_old_trash() -> int:
    rows = db.all("SELECT * FROM trash WHERE deleted_at < ?", (db.now() - config.TRASH_DAYS * 86400,))
    for r in rows:
        _trash_purge_row(r)
    return len(rows)


def cleanup_tmp(max_age: int = 86400) -> None:
    """Remove abandoned partial uploads."""
    cutoff = time.time() - max_age
    for root, _dirs, files in os.walk(config.FILES_DIR):
        for f in files:
            if f.startswith(TMP_PREFIX):
                p = os.path.join(root, f)
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.unlink(p)
                except OSError:
                    pass
