"""In-memory bounded delivery. Emit never performs network or filesystem I/O."""

import asyncio
import time
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from uuid import uuid4, UUID
import httpx
from factoryvpn_common.observability import TechnicalEvent, LOG_CODES, EventBatch


class LogQueue:
    def __init__(self, source, max_events=1000, max_bytes=1048576):
        self.source, self.max_events, self.max_bytes = source, max_events, max_bytes
        self.events = OrderedDict()
        self.frozen = set()
        self.dropped = 0
        self._drops = OrderedDict()

    def emit(self, code, session_id=None, *, route="", **parameters):
        now = datetime.now(timezone.utc)
        session_id = UUID(str(session_id)) if session_id else None
        if self.source not in LOG_CODES[code][3]:
            raise ValueError("Invalid log producer")
        key = (route, code, session_id, tuple(sorted(parameters.items())))
        for identifier, (old_key, old, size) in reversed(self.events.items()):
            if (
                old_key == key
                and identifier not in self.frozen
                and (now - old.observed_at).total_seconds() < 60
            ):
                self.events[identifier] = (
                    key,
                    old.model_copy(
                        update={
                            "observed_at": now,
                            "occurrences": min(1000000, old.occurrences + 1),
                        }
                    ),
                    size,
                )
                return
        event = TechnicalEvent(
            event_id=uuid4(),
            observed_at=now,
            first_observed_at=now,
            session_id=session_id,
            code=code,
            parameters=parameters,
        )
        self.events[event.event_id] = (
            key,
            event,
            len(event.model_dump_json().encode()) + len(route) + 64,
        )
        self._trim(now)

    def _trim(self, now):
        for identifier, (_, event, _) in list(self.events.items()):
            if event.first_observed_at < now - timedelta(hours=24):
                self._drop(identifier)
        while (
            len(self.events) > self.max_events
            or sum(x[2] for x in self.events.values()) > self.max_bytes
        ):
            self._drop(next(iter(self.events)))

    def _drop(self, identifier):
        key, event, _ = self.events.pop(identifier)
        self.frozen.discard(identifier)
        self.dropped += event.occurrences
        scope = (key[0], event.session_id)
        # Drop metadata is also bounded; never copy another session's counts to generic logs.
        self._drops[scope] = min(1000000, self._drops.get(scope, 0) + event.occurrences)
        if len(self._drops) > 100:
            self._drops.popitem(last=False)

    def batch(self):
        self._trim(datetime.now(timezone.utc))
        if not self.events and self._drops:
            # After a long SMS sleep, expired events may be the entire queue.
            # Deliver their bounded summary even if no new fault has occurred.
            self.acknowledge([])
        if not self.events:
            return "", []
        route = next(iter(self.events.values()))[0][0]
        events, size = [], 0
        for key, event, event_size in self.events.values():
            if key[0] != route:
                continue
            if len(events) == 50 or size + event_size > 60000:
                break
            events.append(event)
            size += event_size
        self.frozen.update(e.event_id for e in events)
        return route, events

    def acknowledge(self, events):
        for event in events:
            self.events.pop(event.event_id, None)
            self.frozen.discard(event.event_id)
        drops, self._drops = self._drops, OrderedDict()
        for (route, session), count in drops.items():
            self.emit("logs_dropped", session, route=route, dropped=count)

    async def deliver_once(self, client, path):
        route, events = self.batch()
        if not events:
            return 15
        response = await client.post(
            path.format(route=route), json=EventBatch(events=events).model_dump(mode="json")
        )
        if response.status_code in (401, 403):
            # Old identity must never be replayed under a replacement credential.
            self.events.clear()
            self._drops.clear()
            self.frozen.clear()
            return 3600
        if response.status_code == 404:
            if self.source == "worker":
                # This route names one gateway. A deleted gateway must not hold
                # every other gateway's events behind it, including later batches.
                for identifier, (key, _, _) in list(self.events.items()):
                    if key[0] == route:
                        self.events.pop(identifier)
                        self.frozen.discard(identifier)
                self._drops = OrderedDict(
                    (scope, count) for scope, count in self._drops.items() if scope[0] != route
                )
                return 15
            # Server may predate this API. Do not flood it, or interrupt VPN control.
            return 3600
        if response.status_code in (400, 413, 422):
            self.acknowledge(events)
            return 15
        if response.status_code == 429:
            try:
                return max(15, min(3600, int(response.headers.get("Retry-After", "60"))))
            except ValueError:
                return 60
        response.raise_for_status()
        self.acknowledge(events)
        return 15

    async def run(self, base_url, headers, path, enabled=lambda: True):
        # Headers are fixed for this process/queue. Credential replacement requires a new queue.
        retry, next_attempt = 1, 0
        async with httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=2
        ) as client:
            while True:
                if enabled() and time.monotonic() >= next_attempt:
                    try:
                        async with asyncio.timeout(2):
                            delay = await self.deliver_once(client, path)
                        retry = 1
                    except Exception:
                        delay, retry = retry, min(60, retry * 2)
                    next_attempt = time.monotonic() + delay
                await asyncio.sleep(1)
