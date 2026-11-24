"""Flask entrypoint for the Vercel calendar service."""

import logging
from secrets import token_hex
from time import perf_counter


def _load_app():
    started = perf_counter()
    from flask import request
    from backend.server import create_app

    logging.basicConfig(level=logging.INFO)
    app = create_app()
    boot = token_hex(4)
    app.logger.info("flask_start boot=%s setup_ms=%.1f", boot, (perf_counter() - started) * 1000)

    @app.before_request
    def log_routed_request() -> None:
        if request.path in {"/api/warm", "/api/chat"}:
            app.logger.info("flask_request boot=%s route=%s", boot, request.path)

    return app


app = _load_app()
