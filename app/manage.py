"""Command-line account recovery, e.g. inside the container:

    docker compose exec deadair-tv python -m app.manage list-users
    docker compose exec deadair-tv python -m app.manage set-password <username> [--admin]
"""

from __future__ import annotations

import argparse
import getpass
import sys

from . import auth
from .config import Settings
from .db import Database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.manage")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-users", help="show all accounts")
    sp = sub.add_parser("set-password", help="set a password, creating the user if needed")
    sp.add_argument("username")
    sp.add_argument("--admin", action="store_true", help="also make the user an admin")
    args = parser.parse_args(argv)

    db = Database(Settings.from_env().db_path)
    if args.cmd == "list-users":
        for u in db.list_users():
            print(f"{u['username']}\t{'admin' if u['is_admin'] else 'viewer'}")
        return 0

    password = getpass.getpass(f"New password for {args.username}: ")
    if password != getpass.getpass("Again: "):
        print("Passwords don't match.", file=sys.stderr)
        return 1
    problem = auth.validate_new_credentials(args.username, password)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    user = db.get_user_by_name(args.username)
    if user is None:
        db.create_user(args.username, auth.hash_password(password), args.admin)
        print(f"Created {'admin' if args.admin else 'viewer'} {args.username}.")
    else:
        db.update_user(user["id"], password_hash=auth.hash_password(password), is_admin=True if args.admin else None)
        db.delete_user_sessions(user["id"])
        print(f"Password updated for {user['username']}; their sessions were logged out.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
