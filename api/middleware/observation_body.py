"""Bound streamed observation bodies before JSON parsing, including chunked uploads."""

from starlette.responses import JSONResponse


class ObservationBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        bounded = (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and path.startswith("/api/v1/")
            and path.endswith(("/system-info", "/technical-logs"))
        )
        if not bounded:
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > 65536:
                return await JSONResponse(
                    {"detail": "Observation body exceeds 64 KiB"},
                    status_code=413,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
            if chunk:
                chunks.append(chunk)
            if not message.get("more_body", False):
                break
        consumed = False

        async def body():
            nonlocal consumed
            if consumed:
                return await receive()
            consumed = True
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        await self.app(scope, body, send)
