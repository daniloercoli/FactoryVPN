"""Publish restored revocations without processing any issuance request."""

import asyncio
import os
from pathlib import Path
from sqlalchemy import select
from api.db.session import async_session_maker
from api.models.provisioning import VPNCertificate
from factoryvpn_common.pki import publish_crl


async def main():
    async with async_session_maker() as db:
        revoked = (
            await db.scalars(select(VPNCertificate).where(VPNCertificate.state == "revoked"))
        ).all()
        publish_crl(Path(os.environ["FACTORYVPN_PKI_ROOT"]), revoked)


if __name__ == "__main__":
    asyncio.run(main())
