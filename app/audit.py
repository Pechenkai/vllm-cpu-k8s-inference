"""ASGI-аудит без чтения, буферизации и записи тел запросов и ответов."""

import asyncio
import logging
import time
import uuid

logger = logging.getLogger("private_audit")


class AuditMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        # Фиксированные имена исключают попадание клиентских секретов в пути логов.
        operation = {
            "/v1/chat/completions": "chat_completions",
            "/v1/completions": "completions",
            "/v1/embeddings": "embeddings",
        }.get(scope.get("path"), "other")
        if scope.get("path") in ("/health", "/metrics"):
            return await self.app(scope, receive, send)
        started = time.monotonic()
        status = None
        completed = False
        outcome = "incomplete"
        request_id = uuid.uuid4().hex

        async def audited_send(message):
            nonlocal status, completed
            if message["type"] == "http.response.start":
                status = message["status"]
                message = {
                    **message,
                    "headers": [
                        (key, value)
                        for key, value in message.get("headers", [])
                        if key.lower() != b"x-audit-id"
                    ]
                    + [(b"x-audit-id", request_id.encode("ascii"))],
                }
            await send(message)
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                completed = True

        try:
            await self.app(scope, receive, audited_send)
            outcome = "complete" if completed else "incomplete"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            if status is None:
                status = 500
            raise
        finally:
            logger.info(
                "request_finished",
                extra={
                    "audit": {
                        "request_id": request_id,
                        "operation": operation,
                        "status": status,
                        "outcome": outcome,
                        "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    }
                },
            )
