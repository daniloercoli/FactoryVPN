"""Publish restored revocations without processing any issuance request."""

import asyncio
import os
from pathlib import Path
from sqlalchemy import select
from api.db.session import async_session_maker
from api.models.provisioning import VPNCertificate
from factoryvpn_common.pki import publish_crl


async def main():
    from scripts.provisioning_worker import sync_issuers

    async with async_session_maker() as db:
        root = Path(os.environ["FACTORYVPN_PKI_ROOT"])
        await sync_issuers(db, root)
        revoked = (
            await db.scalars(select(VPNCertificate).where(VPNCertificate.state == "revoked"))
        ).all()
        publish_crl(root, revoked)
        await db.commit()


if __name__ == "__main__":
    asyncio.run(main())
