"""Create the initial administrator using an interactive password or stdin."""

import argparse
import asyncio
import getpass
import sys
from sqlalchemy import select
from api.db.session import async_session_maker, init_db
from api.models.user import User
from api.services.auth_service import hash_password


async def create(username, password, email=None):
    from api.services.mail_service import email_address

    email = email_address(email)
    if len(password) < 12 or len(password) > 128:
        raise ValueError("Admin password must contain between 12 and 128 characters")
    if not 3 <= len(username) <= 50:
        raise ValueError("Username must contain between 3 and 50 characters")
    await init_db()
    async with async_session_maker() as db:
        if (await db.execute(select(User.id).where(User.username == username))).first():
            raise ValueError("User already exists; refusing to overwrite the account")
        user = User(
            username=username,
            password_hash=hash_password(password),
            email=email,
            is_admin=True,
            is_active=True,
        )
        db.add(user)
        await db.commit()
        print(f"Administrator created: {user.id}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--email", required=True)
    args = parser.parse_args()
    password = (
        sys.stdin.readline().rstrip("\r\n")
        if args.password_stdin
        else getpass.getpass("Admin password: ")
    )
    asyncio.run(create(args.username, password, args.email))


if __name__ == "__main__":
    main()
