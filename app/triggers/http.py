import asyncio
import json
import os
from contextlib import suppress

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app.triggers.base import BaseTrigger, TriggerEvent

HTTP_API_KEY = os.getenv("HTTP_API_KEY", "")
HTTP_PUBLIC_URL = os.getenv("HTTP_PUBLIC_URL", "")


async def _verify_auth(authorization: str = Header(default="")) -> None:
    if HTTP_API_KEY and authorization != f"Bearer {HTTP_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")


class RunRequest(BaseModel):
    message: str
    session_id: str | None = None
    metadata: dict = {}


class HTTPTrigger(BaseTrigger):
    def __init__(self, host: str = "0.0.0.0", port: int = 8000) -> None:
        self.host = host
        self.port = port
        self._server = None
        self._uvicorn: uvicorn.Server | None = None
        self._sessions: dict[str, list[dict]] = {}
        self.app = self._build_app()

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Pino Agent")

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        @app.get("/files/{path:path}", dependencies=[Depends(_verify_auth)])
        async def serve_file(path: str):
            from app.tools.files import WORKSPACE_DIR, _safe_path
            target = _safe_path(path)
            if target is None or not target.is_file():
                raise HTTPException(status_code=404, detail="File not found")
            return FileResponse(target)

        @app.post("/api/v1/run", dependencies=[Depends(_verify_auth)])
        async def run(req: RunRequest):
            history = (
                self._sessions.setdefault(req.session_id, [])
                if req.session_id
                else []
            )
            queue: asyncio.Queue = asyncio.Queue()

            async def respond_fn(text: str) -> None:
                await queue.put({"type": "output", "text": text})

            async def status_fn(text: str) -> None:
                await queue.put({"type": "status", "text": text})

            async def react_fn(emoji: str) -> None:
                await queue.put({"type": "reaction", "text": emoji})

            async def deliver_fn(path: str) -> str:
                base = HTTP_PUBLIC_URL.rstrip("/") or f"http://localhost:{self.port}"
                url = f"{base}/files/{path}"
                await queue.put({"type": "file", "path": path, "url": url})
                return f"Download link: {url}"

            event = TriggerEvent(
                input=req.message,
                source="http",
                metadata=req.metadata,
                history=history,
                respond_fn=respond_fn,
                status_fn=status_fn,
                react_fn=react_fn,
                deliver_fn=deliver_fn,
            )

            async def run_agent() -> None:
                try:
                    await self._server.handle_event(event)
                except Exception as e:
                    await queue.put({"type": "error", "text": str(e)})
                finally:
                    await queue.put(None)

            async def generate():
                task = asyncio.create_task(run_agent())
                try:
                    while True:
                        item = await queue.get()
                        if item is None:
                            break
                        yield f"data: {json.dumps(item)}\n\n"
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

            return StreamingResponse(generate(), media_type="text/event-stream")

        return app

    async def start(self, server) -> None:
        self._server = server
        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning")
        self._uvicorn = uvicorn.Server(config)
        print(f"HTTP trigger listening on {self.host}:{self.port}")
        await self._uvicorn.serve()

    async def stop(self) -> None:
        if self._uvicorn:
            self._uvicorn.should_exit = True
