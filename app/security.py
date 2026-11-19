"""Password hashing, tokens, and authentication lookups."""
import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass

from . import db

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)


# ---------- passwords ----------
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt_b64, dk_b64 = stored.split("$")
        dk = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:
        return False


def validate_password(pw: str) -> None:
    if len(pw) < 8:
        raise ValueError("Password must be at least 8 characters")


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ---------- principal ----------
@dataclass
class Principal:
    user: dict
    scope: str = ""          # virtual folder this principal is confined to ("" = everything)
    can_write: bool = True
    via: str = "session"     # session | key | basic
    key_id: int | None = None

    @property
    def uid(self) -> int:
        return self.user["id"]

    @property
    def is_admin(self) -> bool:
        return bool(self.user["is_admin"]) and self.via == "session"


def _active_user(uid: int) -> dict | None:
    u = db.one("SELECT * FROM users WHERE id=?", (uid,))
    if not u or u["disabled"]:
        return None
    return u


# ---------- sessions ----------
def create_session(uid: int, days: int, ua: str = "") -> str:
    token = secrets.token_urlsafe(32)
    t = db.now()
    db.run(
        "INSERT INTO sessions(token_hash,user_id,created_at,expires_at,user_agent) VALUES(?,?,?,?,?)",
        (sha256(token), uid, t, t + days * 86400, ua[:200]),
    )
    return token


def session_principal(token: str) -> Principal | None:
    s = db.one("SELECT * FROM sessions WHERE token_hash=?", (sha256(token),))
    if not s or s["expires_at"] < db.now():
        return None
    u = _active_user(s["user_id"])
    return Principal(u, via="session") if u else None


def delete_session(token: str) -> None:
    db.run("DELETE FROM sessions WHERE token_hash=?", (sha256(token),))


# ---------- API keys ----------
KEY_PREFIX = "mdk_"


def create_api_key(uid: int, name: str, scope: str, can_write: bool) -> tuple[int, str]:
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    kid = db.run(
        "INSERT INTO api_keys(user_id,name,key_hash,prefix,scope,can_write,created_at) VALUES(?,?,?,?,?,?,?)",
        (uid, name, sha256(key), key[:12], scope, int(can_write), db.now()),
    )
    return kid, key


_last_used: dict[int, int] = {}


def key_principal(key: str, via: str = "key") -> Principal | None:
    if not key.startswith(KEY_PREFIX):
        return None
    k = db.one("SELECT * FROM api_keys WHERE key_hash=?", (sha256(key),))
    if not k:
        return None
    u = _active_user(k["user_id"])
    if not u:
        return None
    t = db.now()
    if t - _last_used.get(k["id"], 0) > 60:  # avoid a DB write on every request
        _last_used[k["id"]] = t
        db.run("UPDATE api_keys SET last_used_at=? WHERE id=?", (t, k["id"]))
    return Principal(u, scope=k["scope"], can_write=bool(k["can_write"]), via=via, key_id=k["id"])


# ---------- username/password (login + WebDAV basic auth) ----------
_basic_cache: dict[str, tuple[int, float]] = {}
_cache_lock = threading.Lock()


def password_principal(username: str, password: str, via: str = "basic") -> Principal | None:
    """Check a username/password. Successful checks are cached briefly because scrypt is
    deliberately slow and WebDAV clients authenticate on every request."""
    ck = sha256(username.lower() + "\0" + password)
    with _cache_lock:
        hit = _basic_cache.get(ck)
    if hit and hit[1] > time.time():
        u = _active_user(hit[0])
        return Principal(u, via=via) if u else None
    u = db.one("SELECT * FROM users WHERE username=?", (username.lower(),))
    if not u or u["disabled"] or not verify_password(password, u["pw_hash"]):
        return None
    with _cache_lock:
        if len(_basic_cache) > 1000:
            _basic_cache.clear()
        _basic_cache[ck] = (u["id"], time.time() + 600)
    return Principal(u, via=via)


def clear_auth_cache() -> None:
    with _cache_lock:
        _basic_cache.clear()


# ---------- brute-force protection ----------
_fails: dict[str, list[float]] = {}


def too_many_failures(ip: str) -> bool:
    cutoff = time.time() - 900
    lst = [t for t in _fails.get(ip, []) if t > cutoff]
    _fails[ip] = lst
    return len(lst) >= 10


def record_failure(ip: str) -> None:
    _fails.setdefault(ip, []).append(time.time())
    if len(_fails) > 5000:
        _fails.clear()
