"""Flask entry point for the scheduling chat's NDJSON stream."""

import asyncio
import atexit
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Iterator
from concurrent.futures import CancelledError, Future
from ipaddress import ip_address
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
from backend.demo_calendar import DEFAULT_DEMO_TIME_ZONE, DemoCalendar
from backend.gemini import GeminiClient
from backend.limits import RateLimits
from backend.prefetch import warm_slot_bounds
from backend.prescan import EMAIL, warm_prescan
from backend.protocol import ChatRequest, ServerEvent, TimeZoneName
from backend.state import new_state
from backend.tools import CalendarTools, ToolRejected
from backend.turn import ERRORS, TurnService, observe_cal
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


def _client_address() -> str:
    forwarded = request.headers.get("x-vercel-forwarded-for") if os.environ.get("VERCEL") == "1" else None
    for candidate in (forwarded, request.remote_addr):
        if not candidate:
            continue
        try:
            address = ip_address(candidate)
            return str(getattr(address, "ipv4_mapped", None) or address)
        except ValueError:
            continue
    return "unknown"


def _read_limit(settings: dict[str, str], name: str, default: int) -> int:
    try:
        value = int(settings.get(name, str(default)))
    except ValueError:
        value = 0
    if value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _service_settings() -> dict[str, str]:
    return {**(load_local_environment() if ENV_FILE.is_file() else {}), **os.environ}


def load_service(*, persistent_model: bool = False, settings: dict[str, str] | None = None) -> TurnService:
    settings = _service_settings() if settings is None else settings
    mode = settings.get("DEMO_MODE", "").strip()
    if mode not in {"", "fake"}:
        raise ValueError("DEMO_MODE must be empty or fake.")
    demo_mode = mode == "fake"
    required = ("GEMINI_API_KEY", "STATE_SIGNING_SECRET") if demo_mode else (
        "GEMINI_API_KEY", "CAL_API_KEY", "STATE_SIGNING_SECRET",
    )
    missing = [name for name in required if not settings.get(name, "").strip()]
    if missing:
        raise ValueError("Set these environment variables: " + ", ".join(missing) + ".")
    secret = settings["STATE_SIGNING_SECRET"]
    if len(secret.encode("utf-8")) < 32:
        raise ValueError("STATE_SIGNING_SECRET must contain at least 32 bytes.")
    model_name = settings.get("GEMINI_MODEL") or "gemini-3.5-flash-lite"
    thinking = settings.get("GEMINI_THINKING_LEVEL") or ("low" if model_name == "gemini-3.8-flash" else "minimal")
    backup_name = settings.get("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite").strip()
    backup = None
    if backup_name and backup_name != model_name:
        backup = GeminiClient(settings["GEMINI_API_KEY"], model=backup_name,
                              thinking_level="low" if backup_name == "gemini-3.8-flash" else "minimal",
                              persistent=persistent_model, first_event_timeout=None)
    if demo_mode:
        demo_zone = settings.get("DEMO_TIME_ZONE", "").strip() or DEFAULT_DEMO_TIME_ZONE
        calendar = DemoCalendar(time_zone=demo_zone)
    else:
        calendar = CalClient(
            settings["CAL_API_KEY"], settings.get("CAL_API_BASE_URL") or "https://api.cal.com", observer=observe_cal,
        )
    warm_prescan()
    return TurnService(
        model=GeminiClient(settings["GEMINI_API_KEY"], model=model_name, thinking_level=thinking, persistent=persistent_model),
        backup_model=backup,
        backup_continuations={model_name, backup_name} == {"gemini-3.5-flash-lite", "gemini-3.1-flash-lite"},
        rate_limits=RateLimits(_read_limit(settings, "RATE_LIMIT_TURNS_PER_10MIN", 30),
                               _read_limit(settings, "DAILY_MODEL_CALL_CAP", 500)),
        calendar=calendar,
        secret=secret,
        allowed_slugs=() if demo_mode else tuple(slug.strip() for slug in settings.get("CAL_EVENT_TYPE_SLUGS", "").split(",") if slug.strip()),
        default_slug=None if demo_mode else (settings.get("CAL_DEFAULT_EVENT_TYPE_SLUG") or None),
    )


def _same_origin_request(*, hosted: bool) -> bool:
    if request.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
        return False
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    scheme = "https" if hosted else request.scheme
    if origin == f"{scheme}://{request.host}":
        return True
    if not hosted and request.remote_addr:
        try:
            loopback = ip_address(request.remote_addr).is_loopback
        except ValueError:
            loopback = False
        # The local Next.js dev proxy sets this header to its browser-facing host.
        if loopback and (host := request.headers.get("X-Forwarded-Host")):
            return origin == f"{scheme}://{host}"
    return False


def _api_json(status: int, **payload: Any) -> Response:
    result = jsonify(**payload)
    result.status_code = status
    result.headers["Cache-Control"] = "no-store"
    return result


def create_app(
    service: TurnService | None = None,
    *,
    turn_loop: TurnLoop | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 300 * 1024
    turn_service = load_service(persistent_model=True) if service is None else service
    hosted = os.environ.get("VERCEL") == "1"
    if service is None and turn_loop is None:
        turn_loop = TurnLoop(turn_service.close_models)
        atexit.register(turn_service.close_prefetch)
        atexit.register(turn_loop.close)

    def rate_limit_response(*, warm: bool = False) -> Response | None:
        limits = turn_service.rate_limits
        retry_after = limits.request_retry_after(_client_address(), warm=warm) if limits is not None else 0
        if not retry_after:
            return None
        result = jsonify(error=ERRORS["rate_limited"], code="rate_limited")
        result.status_code = 429
        result.headers["Retry-After"] = str(retry_after)
        result.headers["Cache-Control"] = "no-store"
        return result

    @app.get("/api/warm")
    def warm() -> Response | tuple[Response, int]:
        try:
            time_zone = _WARM_TIME_ZONE.validate_python(request.args.get("timeZone", "UTC"))
        except ValidationError:
            return jsonify(error="Invalid time zone.", code="invalid_request"), 400

        if (limited := rate_limit_response(warm=True)) is not None:
            return limited

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
        except (CalError, ToolRejected):
            return jsonify(error="The calendar could not be reached.", code="cal_down"), 503
        try:
            begin, end = warm_slot_bounds(now, time_zone)
            calendar.slots(default["id"], begin, end, time_zone)
        except CalError as error:
            app.logger.info("Calendar slot warm-up failed: %s", error.code)

        mode_metadata = {"demoMode": True} if isinstance(calendar, DemoCalendar) else {}
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
            **mode_metadata,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/chat")
    def chat() -> Response:
        if not _same_origin_request(hosted=hosted):
            return _api_json(403, code="invalid_origin", error="This request must come from the scheduling app.")
        try:
            chat_request = ChatRequest.model_validate(request.get_json(silent=True))
        except ValidationError:
            result = jsonify(error="Invalid chat request.", code="invalid_request")
            result.status_code = 400
            return result
        if (limited := rate_limit_response()) is not None:
            return limited
        return Response(
            stream_with_context(stream_ndjson(turn_service.events(chat_request), turn_loop)),
            content_type="application/x-ndjson; charset=utf-8",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    create_app().run(host="127.0.0.1", port=int(os.environ.get("FLASK_PORT", "5000")))
