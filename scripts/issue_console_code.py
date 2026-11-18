"""Issue a one-use local account code. Requires deployment/database administrator access."""

import argparse
import asyncio

from sqlalchemy import select

from api.db.session import async_session_maker
from api.models import User
from api.services import console_access


async def issue_code(username, *, operator, reason, purpose="login"):
    async with async_session_maker() as db:
        user = await db.scalar(select(User).where(User.username == username))
        row, code = await console_access.issue(
            db, user, operator=operator, reason=reason, purpose=purpose
        )
        await db.commit()
        return row, code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    parser.add_argument("--operator", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--purpose", choices=sorted(console_access.PURPOSES), default="login")
    args = parser.parse_args()
    try:
        args.operator = console_access.audit_text(args.operator, "operator", 100)
        args.reason = console_access.audit_text(args.reason, "reason", 500)
    except ValueError as exc:
        parser.error(str(exc))
    if input("Verify the account owner's identity. Type EMETTI to issue one code: ") != "EMETTI":
        raise SystemExit("Cancelled")
    row, code = asyncio.run(
        issue_code(args.username, operator=args.operator, reason=args.reason, purpose=args.purpose)
    )
    print(f"Console code ({row.purpose}): {code}")
    print(f"Expires at {row.expires_at.isoformat()}. One use, together with the account password.")
    print("Keep this code private. It does not verify or change the account email.")


if __name__ == "__main__":
    main()
