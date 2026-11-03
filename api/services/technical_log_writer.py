"""Best-effort API event queue; never join the VPN transaction or log exceptions."""

import asyncio
from uuid import UUID
from fastapi import HTTPException
from api.models.gateway import Gateway
from api.services.gateway_observability import record_logs, prune_logs
from api.services.lifecycle import allocation_lock
from factoryvpn_common.technical_logs import LogQueue

queue = LogQueue("api")


def emit(session, code):
    if session.gateway_id and session.group_id:
        queue.emit(code, session.id, route=f"{session.gateway_id}/{session.group_id}")


async def flush_once(factory):
    route, events = queue.batch()
    if not events:
        return
    identifier, group = (UUID(value) for value in route.split("/"))
    async with factory() as db:
        await allocation_lock(db)
        gateway = await db.get(Gateway, identifier)
        if gateway:
            await record_logs(db, gateway, events, "api", expected_group=group)
        await db.commit()
    queue.acknowledge(events)


async def run(factory):
    iteration = 0
    while True:
        try:
            async with asyncio.timeout(2):
                await flush_once(factory)
            if iteration % 4 == 0:
                async with asyncio.timeout(2), factory() as db:
                    await prune_logs(db)
                    await db.commit()
        except (Exception, HTTPException):
            pass
        iteration += 1
        await asyncio.sleep(15)
