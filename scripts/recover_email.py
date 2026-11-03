"""Console recovery for a local account. Requires deployment/database administrator access."""

import argparse
import asyncio
from sqlalchemy import select
from api.db.session import async_session_maker
from api.models import User
from api.services.lifecycle import allocation_lock
from api.services.email_security import recover_email
from api.services.mail_service import email_address


async def recover(username, email):
    async with async_session_maker() as db:
        await allocation_lock(db)
        user = await db.scalar(select(User).where(User.username == username))
        if not user or not user.is_active or user.auth_mode != "local":
            raise ValueError("Active local account required")
        await recover_email(db, user, email)
        await db.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    parser.add_argument("--email", required=True, type=email_address)
    args = parser.parse_args()
    if (
        input(
            "Verify the account owner's identity. This revokes their access and VPNs. Type RECUPERA: "
        )
        != "RECUPERA"
    ):
        raise SystemExit("Cancelled")
    asyncio.run(recover(args.username, args.email))
    print(
        "Recovery prepared. Password and the code sent to the new email are required at next login."
    )


if __name__ == "__main__":
    main()
