"""Managing API keys.

    python -m control.keys issue  --email ops@acme.test [--label laptop]
    python -m control.keys list   [--firm acme] [--email ops@acme.test]
    python -m control.keys revoke --prefix 7e45a61a

Only a SHA-256 of each token is stored, so a lost key cannot be looked up --
it is replaced. This is the command that replaces it, which until now the
bootstrap docstring promised and nothing provided.

`list` shows the prefix, which identifies a key without being any part of the
secret. Nothing here ever prints something that could reconstruct a token
except `issue`, once, at the moment of minting.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from config import load_env

from .db import Database, get_database, set_database
from .models import ApiKey
from .service import find_key_by_prefix, get_user_by_email, issue_key, list_keys, revoke_key


def _database(url: str | None) -> Database:
    if url:
        database = Database(url)
        set_database(database)
        return database
    return get_database()


def _when(value: datetime | None) -> str:
    return f"{value:%Y-%m-%d %H:%M}" if value else "never"


def _issue(args: argparse.Namespace) -> int:
    with _database(args.database).transaction() as session:
        try:
            user = get_user_by_email(session, args.email)
        except KeyError:
            print(f"no such user: {args.email}")
            print("Create one with: python -m control.bootstrap --firm <firm> "
                  f"--email {args.email}")
            return 1
        _, token = issue_key(session, user, args.label, actor="keys-cli")

    print(f"user  : {args.email}")
    print(f"firm  : {user.firm_id or '(platform admin)'}")
    print(f"token : {token}")
    print("\nThis token is shown once. Store it now.")
    print("For the local frontend, put it in web/.env.local as:")
    print(f"  VITE_API_TOKEN={token}")
    return 0


def _list(args: argparse.Namespace) -> int:
    with _database(args.database).transaction() as session:
        keys = list_keys(session, firm_id=args.firm, email=args.email)
        if not keys:
            print("no keys found")
            return 0

        rows = [
            (
                key.prefix,
                key.user.email,
                key.user.firm_id or "-",
                key.label or "-",
                _when(key.created_at),
                _when(key.last_used_at),
                "revoked" if key.revoked_at else "active",
            )
            for key in keys
        ]

    headers = ("PREFIX", "USER", "FIRM", "LABEL", "CREATED", "LAST USED", "STATUS")
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)))
    return 0


def _revoke(args: argparse.Namespace) -> int:
    with _database(args.database).transaction() as session:
        try:
            key: ApiKey = find_key_by_prefix(session, args.prefix)
        except KeyError:
            print(f"no key with prefix {args.prefix!r}")
            print("List them with: python -m control.keys list")
            return 1
        if key.revoked_at:
            print(f"key {args.prefix} was already revoked at {_when(key.revoked_at)}")
            return 0
        email = key.user.email
        revoke_key(session, key, actor="keys-cli")

    print(f"revoked {args.prefix} ({email})")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(description="Manage risk_grid API keys")
    parser.add_argument("--database", default=None, help="SQLAlchemy URL")
    sub = parser.add_subparsers(dest="command", required=True)

    issue = sub.add_parser("issue", help="mint a new key for an existing user")
    issue.add_argument("--email", required=True)
    issue.add_argument("--label", default="", help="what this key is for, e.g. laptop")
    issue.set_defaults(handler=_issue)

    listing = sub.add_parser("list", help="show keys, never their secrets")
    listing.add_argument("--firm", default=None)
    listing.add_argument("--email", default=None)
    listing.set_defaults(handler=_list)

    revoke = sub.add_parser("revoke", help="stop a key working")
    revoke.add_argument("--prefix", required=True, help="from `keys list`")
    revoke.set_defaults(handler=_revoke)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
