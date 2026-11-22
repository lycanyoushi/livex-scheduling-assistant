"""Flask entry point for the scheduling chat's NDJSON stream."""

import asyncio
import atexit
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Iterator
from concurrent.futures import CancelledError, Future
import json
import logging
import os
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, stream_with_context
from pydantic import TypeAdapter, ValidationError

from backend.cal_client import CalClient, CalError
from backend.gemini import GeminiClient
from backend.prefetch import warm_slot_bounds
from backend.prescan import EMAIL, warm_prescan
from backend.protocol import ChatRequest, ServerEvent, TimeZoneName
from backend.state import new_state
from backend.tools import CalendarTools, ToolRejected
from backend.turn import TurnService, observe_cal
from scripts.local_environment import ENV_FILE, load_local_environment


_WARM_TIME_ZONE = TypeAdapter(TimeZoneName)


def _public_url(value: str | None) -> str | None:
    if not value or len(value) > 2_048 or any(ord(character) <= 32 for character in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None:
            return value
    except ValueError:
        pass
    return None


def _public_text(value: str) -> str:
    return EMAIL.sub("[email]", " ".join(value.split()))[:120]


class TurnLoop:
    """Own the Flask process's turn loop and close its shared model transport."""

    def __init__(self, close_model: Callable[[], Awaitable[None]] | None = None) -> None:
        self._close_model = close_model
        self._lock = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker: Thread | None = None
        self._closed = False

    def submit(self, task: Coroutine[Any, Any, None]) -> Future[None]:
        with self._lock:
            if self._closed:
                task.close()
                raise RuntimeError("The turn loop is closed.")
            if self._loop is None:
                runner = asyncio.Runner()
                self._loop = runner.get_loop()
                self._worker = Thread(target=self._run, args=(runner,), name="livex-turn-loop", daemon=True)
                self._worker.start()
            return asyncio.run_coroutine_threadsafe(task, self._loop)

    @staticmethod
    def _run(runner: asyncio.Runner) -> None:
        with runner:
            runner.get_loop().run_forever()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop, worker = self._loop, self._worker
        if loop is None or worker is None:
            return

        async def shutdown() -> None:
            active = asyncio.all_tasks() - {asyncio.current_task()}
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            if self._close_model is not None:
                await self._close_model()

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), loop).result()
        finally:
            loop.call_soon_threadsafe(loop.stop)
            worker.join()


def stream_ndjson(events: AsyncGenerator[ServerEvent, None], turn_loop: TurnLoop | None = None) -> Iterator[bytes]:
    pending: Queue[ServerEvent] = Queue(maxsize=16)
    closed, done = Event(), Event()
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task | None = None
    failed = False

    async def produce() -> None:
        nonlocal loop, task, failed
        loop, task = asyncio.get_running_loop(), asyncio.current_task()
        try:
            try:
                if closed.is_set():
                    return
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
        except Exception as error:
            failed = True
            logging.getLogger(__name__).error("Chat stream failed: %s", type(error).__name__)
        finally:
            done.set()

    def run() -> None:
        try:
            asyncio.run(produce())
        except asyncio.CancelledError:
            pass

    worker = None
    producer = None
    if turn_loop is None:
        worker = Thread(target=run, name="livex-chat-stream")
        worker.start()
    else:
        producer = turn_loop.submit(produce())
        producer.add_done_callback(lambda _: done.set())
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
        if worker is not None:
            worker.join()
        elif producer is not None:
            try:
                producer.result()
            except CancelledError:
                pass


def _encode(event: ServerEvent) -> bytes:
    return (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def load_service(*, persistent_model: bool = False) -> TurnService:
    settings = {**(load_local_environment() if ENV_FILE.is_file() else {}), **os.environ}
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
        model=GeminiClient(settings["GEMINI_API_KEY"], model=model_name, thinking_level=thinking, persistent=persistent_model),
        calendar=CalClient(settings["CAL_API_KEY"], settings.get("CAL_API_BASE_URL") or "https://api.cal.com",
                           observer=observe_cal),
        secret=secret,
        allowed_slugs=tuple(slug.strip() for slug in settings.get("CAL_EVENT_TYPE_SLUGS", "").split(",") if slug.strip()),
        default_slug=settings.get("CAL_DEFAULT_EVENT_TYPE_SLUG") or None,
    )


def create_app(service: TurnService | None = None, *, turn_loop: TurnLoop | None = None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 300 * 1024
    turn_service = service if service is not None else load_service(persistent_model=True)
    if service is None and turn_loop is None:
        turn_loop = TurnLoop(getattr(turn_service.model, "aclose", None))
        atexit.register(turn_service.close_prefetch)
        atexit.register(turn_loop.close)

    @app.get("/api/warm")
    def warm() -> Response | tuple[Response, int]:
        try:
            time_zone = _WARM_TIME_ZONE.validate_python(request.args.get("timeZone", "UTC"))
        except ValidationError:
            return jsonify(error="Invalid time zone.", code="invalid_request"), 400

        now = turn_service.clock()
        calendar = turn_service.calendar
        tools = CalendarTools(
            calendar, new_state(time_zone, now), now,
            allowed_slugs=turn_service.allowed_slugs, default_slug=turn_service.default_slug,
        )
        try:
            profile = calendar.me()
            event_types = tools.event_types()
            default = tools.default_event()
            begin, end = warm_slot_bounds(now, time_zone)
            calendar.slots(default["id"], begin, end, time_zone)
        except (CalError, ToolRejected):
            return jsonify(error="The calendar could not be reached.", code="cal_down"), 503

        response = jsonify(
            hostName=_public_text(profile.get("name") or "") or _public_text(profile["username"]),
            avatarUrl=_public_url(profile.get("avatarUrl")),
            eventTypes=[{
                "id": event["id"],
                "slug": event["slug"],
                "title": _public_text(event["title"]),
                "lengthInMinutes": event["lengthInMinutes"],
            } for event in event_types],
            bookingUrl=_public_url(default.get("bookingUrl")),
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/chat")
    def chat() -> Response:
        try:
            chat_request = ChatRequest.model_validate(request.get_json(silent=True))
        except ValidationError:
            result = jsonify(error="Invalid chat request.", code="invalid_request")
            result.status_code = 400
            return result
        return Response(
            stream_with_context(stream_ndjson(turn_service.events(chat_request), turn_loop)),
            content_type="application/x-ndjson; charset=utf-8",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    create_app().run(host="127.0.0.1", port=int(os.environ.get("FLASK_PORT", "5000")))
