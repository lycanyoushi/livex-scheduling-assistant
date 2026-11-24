"""Verify configuration and perform read-only provider checks."""

import asyncio

from backend.cal_client import CalError
from backend.demo_calendar import DemoCalendar
from backend.model import ModelError
from backend.server import load_service


def _report_failure(provider: str, status: int | None) -> None:
    if status in {400, 401, 403, 404}:
        print(f"{provider} rejected the check (HTTP {status}). Verify the configured credentials and access.")
    elif type(status) is int and 100 <= status <= 599:
        print(f"{provider} could not complete the check (HTTP {status}).")
    else:
        print(f"{provider} could not be reached with the current configuration.")


def main() -> int:
    try:
        service = load_service()
    except (OSError, UnicodeError):
        print("Could not read .env.local. Verify its location and encoding.")
        return 1
    except ValueError as error:
        print(str(error))
        return 1

    passed = True
    try:
        if isinstance(service.calendar, DemoCalendar):
            print("Demo mode uses the in-memory calendar. The Cal.com check was skipped.")
        else:
            try:
                service.calendar.me(retry=False)
            except CalError as error:
                _report_failure("Cal.com", error.status)
                passed = False
            except (OSError, ValueError):
                _report_failure("Cal.com", None)
                passed = False
            else:
                print("Cal.com authenticated the profile lookup.")

        try:
            asyncio.run(service.model.check_metadata())
        except ModelError as error:
            _report_failure("Gemini", error.http_status)
            passed = False
        else:
            print("Gemini returned metadata for the configured model.")
    finally:
        service.close_prefetch()
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
