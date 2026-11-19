"""Admin command line.  Run as the mydrive user, e.g.:

    sudo mydrive-admin create-user prashant --admin
    sudo mydrive-admin reset-password prashant
    sudo mydrive-admin list-users
"""
import argparse
import getpass
import sys

from fastapi import HTTPException

from . import config, db, security


def _ask_password() -> str:
    while True:
        a = getpass.getpass("New password (min 8 chars): ")
        if len(a) < 8:
            print("Too short.")
            continue
        if getpass.getpass("Repeat password: ") != a:
            print("Passwords do not match.")
            continue
        return a


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mydrive-admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user")
    c.add_argument("username")
    c.add_argument("--admin", action="store_true")
    c.add_argument("--quota-gb", type=float)
    c.add_argument("--password", help="(avoid: ends up in shell history)")
    r = sub.add_parser("reset-password")
    r.add_argument("username")
    sub.add_parser("list-users")
    a = ap.parse_args(argv)
    config.ensure_dirs()

    if a.cmd == "create-user":
        from .api import create_user
        pw = a.password or _ask_password()
        try:
            u = create_user(a.username, pw, a.admin, int(a.quota_gb * 1024**3) if a.quota_gb else None)
        except HTTPException as e:
            sys.exit(f"Error: {e.detail}")
        print(f"Created user '{u['username']}'{' (admin)' if a.admin else ''}.")
    elif a.cmd == "reset-password":
        u = db.one("SELECT id FROM users WHERE username=?", (a.username.lower(),))
        if not u:
            sys.exit("No such user")
        db.run("UPDATE users SET pw_hash=?, disabled=0 WHERE id=?", (security.hash_password(_ask_password()), u["id"]))
        db.run("DELETE FROM sessions WHERE user_id=?", (u["id"],))
        print("Password updated.")
    elif a.cmd == "list-users":
        for u in db.all("SELECT username,is_admin,disabled,quota_bytes FROM users ORDER BY username"):
            q = f"{u['quota_bytes'] / 1024**3:.2f} GB" if u["quota_bytes"] else "no quota"
            print(f"{u['username']:<20} {'admin' if u['is_admin'] else 'user':<6} {q:<10} "
                  f"{'DISABLED' if u['disabled'] else ''}")


if __name__ == "__main__":
    main()
