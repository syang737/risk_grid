"""Create the schema and a first firm, user and API key.

    python -m control.bootstrap --firm acme --name "Acme Securities" \
        --email ops@acme.test --role firm_admin

Prints the token once. There is no way to recover it afterwards -- only its
hash is stored -- so a lost key is reissued, not looked up.
"""

from __future__ import annotations

import argparse

from .db import Database, set_database
from .models import Role
from .service import create_firm, create_user, get_firm, issue_key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the risk_grid control plane")
    parser.add_argument("--database", default=None, help="SQLAlchemy URL")
    parser.add_argument("--firm", required=True)
    parser.add_argument("--name", default=None, help="firm display name")
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--role",
        default=Role.firm_admin.value,
        choices=[r.value for r in Role],
    )
    parser.add_argument("--label", default="bootstrap", help="label for the API key")
    args = parser.parse_args(argv)

    database = Database(args.database)
    set_database(database)
    database.create_all()

    role = Role(args.role)
    with database.transaction() as session:
        try:
            firm = get_firm(session, args.firm)
        except KeyError:
            firm = create_firm(session, args.firm, args.name or args.firm, actor="bootstrap")

        user = create_user(
            session,
            email=args.email,
            role=role,
            # A platform admin belongs to no firm, even though one was named.
            firm_id=None if role.is_platform else firm.id,
            actor="bootstrap",
        )
        session.flush()
        _, token = issue_key(session, user, args.label, actor="bootstrap")

    print(f"firm  : {args.firm}")
    print(f"user  : {args.email} ({role.value})")
    print(f"token : {token}")
    print("\nThis token is shown once. Store it now.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
