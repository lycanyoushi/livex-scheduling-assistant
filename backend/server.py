"""Flask entry point for the scheduling chat's NDJSON stream."""

import asyncio
from collections.abc import AsyncGenerator, Iterator
import json
import logging
import os
from queue import Empty, Full, Queue
from threading import Event, Thread

from flask import Flask, Response, jsonify, request, stream_with_context
from pydantic import ValidationError

from backend.cal_client import CalClient
from backend.gemini import GeminiClient
from backend.prescan import warm_prescan
from backend.protocol import ChatRequest, ServerEvent
from backend.turn import TurnService, observe_cal
from scripts.local_environment import ENV_FILE, load_local_environment


def stream_ndjson(events: AsyncGenerator[ServerEvent, None]) -> Iterator[bytes]:
    pending: Queue[ServerEvent] = Queue(maxsize=16)
    closed, done = Event(), Event()
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task | None = None
    failed = False

    async def produce() -> None:
        nonlocal loop, task
        loop, task = asyncio.get_running_loop(), asyncio.current_task()
        try:
            async for event in events:
                while not closed.is_set():
                    try:
                        pending.put_nowait(event)
                        break
                    except Full:
                        await asyncio.sleep(0.005)
                if closed.is_set():
                    break
        finally:
            await events.aclose()

    def run() -> None:
        nonlocal failed
        try:
            if not closed.is_set():
                asyncio.run(produce())
        except asyncio.CancelledError:
            pass
        except Exception as error:
            failed = True
            logging.getLogger(__name__).error("Chat stream failed: %s", type(error).__name__)
        finally:
            done.set()

    worker = Thread(target=run, name="livex-chat-stream")
    worker.start()
    try:
        while not done.is_set() or not pending.empty():
            try:
                event = pending.get(timeout=0.05)
            except Empty:
                continue
            yield _encode(event)
        if failed:
            yield _encode({"type": "error", "code": "stream_error",
                           "message": "The chat stopped unexpectedly. Please retry.", "retryable": True})
    finally:
        closed.set()
        if loop is not None and task is not None and not done.is_set():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        worker.join()


def _encode(event: ServerEvent) -> bytes:
    return (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def load_service() -> TurnService:
    settings = {**os.environ, **(load_local_environment() if ENV_FILE.is_file() else {})}
    required = ("GEMINI_API_KEY", "CAL_API_KEY", "STATE_SIGNING_SECRET")
    missing = [name for name in required if not settings.get(name)]
    if missing:
        raise ValueError("Set these environment variables: " + ", ".join(missing) + ".")
    secret = settings["STATE_SIGNING_SECRET"]
    if len(secret.encode("utf-8")) < 32:
        raise ValueError("STATE_SIGNING_SECRET must contain at least 32 bytes.")
    model_name = settings.get("GEMINI_MODEL") or "gemini-3.5-flash-lite"
    thinking = settings.get("GEMINI_THINKING_LEVEL") or ("low" if model_name == "gemini-3.8-flash" else "minimal")
    warm_prescan()
    return TurnService(
        model=GeminiClient(settings["GEMINI_API_KEY"], model=model_name, thinking_level=thinking),
        calendar=CalClient(settings["CAL_API_KEY"], settings.get("CAL_API_BASE_URL") or "https://api.cal.com",
                           observer=observe_cal),
        secret=secret,
        allowed_slugs=tuple(slug.strip() for slug in settings.get("CAL_EVENT_TYPE_SLUGS", "").split(",") if slug.strip()),
        default_slug=settings.get("CAL_DEFAULT_EVENT_TYPE_SLUG") or None,
    )


def create_app(service: TurnService | None = None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 300 * 1024
    turn_service = service if service is not None else load_service()

    @app.post("/api/chat")
    def chat() -> Response:
        try:
            chat_request = ChatRequest.model_validate(request.get_json(silent=True))
        except ValidationError:
            result = jsonify(error="Invalid chat request.", code="invalid_request")
            result.status_code = 400
            return result
        return Response(
            stream_with_context(stream_ndjson(turn_service.events(chat_request))),
            content_type="application/x-ndjson; charset=utf-8",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    create_app().run(host="127.0.0.1", port=int(os.environ.get("FLASK_PORT", "5000")))
