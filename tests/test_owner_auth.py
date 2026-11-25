import os
import unittest
from unittest.mock import patch

from backend.model import ModelCompletion, ModelText
from backend.owner_auth import OwnerAccess
from backend.server import TurnLoop, create_app
from backend.turn import TurnService
from evals.fake_cal import DEFAULT_CLOCK, FakeCal


_TEST_CODE = "synthetic owner access code"
_TEST_SIGNING_KEY = "synthetic-signing-secret-for-owner-auth"
_CHAT_REQUEST = {"stateToken": None, "timeZone": "UTC", "input": {"kind": "text", "text": "Hello"}}
_COOKIE_NAME = "livex_owner"


class _Calendar(FakeCal):
    def __init__(self):
        super().__init__()
        self.profile_calls = 0

    def me(self):
        self.profile_calls += 1
        return super().me()


class _Model:
    def __init__(self):
        self.calls = 0

    async def stream(self, **kwargs):
        self.calls += 1
        yield ModelText("I can help with your calendar.")
        yield ModelCompletion("fixture-interaction", "completed")


class OwnerAuthTests(unittest.TestCase):
    def setUp(self):
        self.calendar = _Calendar()
        self.model = _Model()
        self.service = TurnService(
            model=self.model, calendar=self.calendar, secret=_TEST_SIGNING_KEY, clock=lambda: DEFAULT_CLOCK,
        )
        self.addCleanup(self.service.close_prefetch)
        self.app = create_app(self.service, owner_access_code=_TEST_CODE)
        self.client = self.app.test_client()

    def sign_in(self, client=None, **kwargs):
        return (client or self.client).post("/api/auth", json={"code": _TEST_CODE}, **kwargs)

    def test_gate_returns_generic_no_store_401_before_profile_or_model_work(self):
        self.assertEqual(self.client.get("/api/auth").json, {"authenticated": False, "required": True})
        for response in (
            self.client.get("/api/warm?timeZone=invalid"),
            self.client.post("/api/chat", json=_CHAT_REQUEST),
            self.client.post("/api/chat", json={"malformed": "body"}),
        ):
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json["code"], "owner_auth_required")
            self.assertNotIn("hostName", response.json)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual((self.calendar.profile_calls, self.model.calls), (0, 0))

    def test_signed_cookie_unlocks_calendar_and_sign_out_clears_it(self):
        signed_in = self.sign_in()
        self.assertEqual(signed_in.status_code, 200)
        self.assertEqual(signed_in.json, {"authenticated": True, "required": True})
        cookie_header = signed_in.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie_header)
        self.assertIn("SameSite=Strict", cookie_header)
        self.assertIn("Path=/api", cookie_header)
        self.assertNotIn("Domain=", cookie_header)
        self.assertNotIn(_TEST_CODE, cookie_header)

        warm = self.client.get("/api/warm")
        self.assertEqual(warm.status_code, 200)
        self.assertIn("hostName", warm.json)
        self.assertGreater(self.calendar.profile_calls, 0)
        chat = self.client.post("/api/chat", json=_CHAT_REQUEST)
        self.assertEqual(chat.mimetype, "application/x-ndjson")
        self.assertIn(b'"type":"turn_end"', chat.get_data())
        self.assertEqual(self.model.calls, 1)

        signed_out = self.client.delete("/api/auth")
        self.assertEqual(signed_out.json, {"authenticated": False, "required": True})
        self.assertIn("Max-Age=0", signed_out.headers["Set-Cookie"])
        self.assertIsNone(self.client.get_cookie(_COOKIE_NAME, path="/api"))
        self.assertEqual(self.client.get("/api/warm").status_code, 401)

    def test_cookie_is_secure_for_hosted_service_and_survives_another_instance(self):
        with patch.dict(os.environ, {"VERCEL": "1"}):
            first = create_app(self.service, owner_access_code=_TEST_CODE).test_client()
            second = create_app(self.service, owner_access_code=_TEST_CODE).test_client()
        result = self.sign_in(first, base_url="https://owner.example.test")
        self.assertIn("Secure", result.headers["Set-Cookie"])
        cookie = first.get_cookie(_COOKIE_NAME, domain="owner.example.test", path="/api")
        second.set_cookie(_COOKIE_NAME, cookie.value, domain="owner.example.test", path="/api")
        self.assertTrue(second.get("/api/auth", base_url="https://owner.example.test").json["authenticated"])

    def test_invalid_expired_or_rotated_signatures_do_not_grant_access(self):
        with patch("itsdangerous.timed.TimestampSigner.get_timestamp", return_value=100):
            self.sign_in()
        cookie = self.client.get_cookie(_COOKIE_NAME, path="/api")
        with patch("itsdangerous.timed.TimestampSigner.get_timestamp", return_value=100 + 12 * 60 * 60 + 1):
            self.assertEqual(self.client.get("/api/warm").status_code, 401)

        rotated = create_app(self.service, owner_access_code="different synthetic code").test_client()
        rotated.set_cookie(_COOKIE_NAME, cookie.value, path="/api")
        with patch("itsdangerous.timed.TimestampSigner.get_timestamp", return_value=101):
            self.assertEqual(rotated.get("/api/warm").status_code, 401)
            payload, signature = cookie.value.rsplit(".", 1)
            changed = ("b" if signature[0] == "a" else "a") + signature[1:]
            self.client.set_cookie(_COOKIE_NAME, f"{payload}.{changed}", path="/api")
            self.assertEqual(self.client.get("/api/warm").status_code, 401)

    def test_only_exact_json_code_is_accepted_and_failed_ips_are_throttled(self):
        for payload in (
            {"code": 7}, {"code": _TEST_CODE, "extra": True}, {"code": "wrong"},
            {"code": "wrong again"}, {"code": "still wrong"},
        ):
            failure = self.client.post("/api/auth", json=payload, environ_overrides={"REMOTE_ADDR": "192.0.2.1"})
            self.assertEqual(failure.status_code, 401)
            self.assertEqual(failure.json["code"], "invalid_access_code")
            self.assertNotIn("Set-Cookie", failure.headers)
        throttled = self.sign_in(environ_overrides={"REMOTE_ADDR": "192.0.2.1"})
        self.assertEqual(throttled.status_code, 429)
        self.assertGreater(int(throttled.headers["Retry-After"]), 0)
        self.assertEqual(self.sign_in(environ_overrides={"REMOTE_ADDR": "192.0.2.2"}).status_code, 200)

    def test_failed_attempt_window_expires_and_valid_attempt_clears_a_bucket(self):
        time = 0
        access = OwnerAccess(_TEST_CODE, _TEST_SIGNING_KEY, clock=lambda: time)
        for _ in range(4):
            self.assertEqual(access.attempt("fixture-ip", "wrong"), (False, 0))
        self.assertEqual(access.attempt("fixture-ip", _TEST_CODE), (True, 0))
        for _ in range(5):
            self.assertEqual(access.attempt("fixture-ip", "wrong"), (False, 0))
        self.assertEqual(access.attempt("fixture-ip", _TEST_CODE), (False, 600))
        time = 600
        self.assertEqual(access.attempt("fixture-ip", _TEST_CODE), (True, 0))

    def test_rejects_browser_cross_origin_actions_and_allows_local_next_proxy(self):
        wrong_origin = {"Origin": "https://other.example.test", "Sec-Fetch-Site": "same-site"}
        self.assertEqual(self.sign_in(headers=wrong_origin).status_code, 403)
        self.assertEqual(self.sign_in(headers={"Origin": "https://other.example.test"}).status_code, 403)
        local_proxy = {"Origin": "http://localhost:3000", "X-Forwarded-Host": "localhost:3000",
                       "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(self.sign_in(headers=local_proxy).status_code, 200)
        self.assertEqual(self.client.delete("/api/auth", headers=wrong_origin).status_code, 403)
        self.assertEqual(self.client.post("/api/chat", json=_CHAT_REQUEST, headers=wrong_origin).status_code, 403)
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(self.client.delete("/api/auth", headers=local_proxy).status_code, 200)

    def test_web_startup_requires_a_real_mode_code_and_allows_fake_without_one(self):
        with patch("backend.server._service_settings", return_value={}), patch("backend.server.load_service") as factory:
            with self.assertRaisesRegex(ValueError, "OWNER_ACCESS_CODE"):
                create_app()
            factory.assert_not_called()
            with self.assertRaisesRegex(ValueError, "Use OWNER_ACCESS_CODE"):
                create_app(owner_access_code=_TEST_CODE)
        with patch("backend.server._service_settings", return_value={"DEMO_MODE": "fake"}):
            with patch("backend.server.load_service", return_value=self.service):
                loop = TurnLoop()
                self.addCleanup(loop.close)
                client = create_app(turn_loop=loop).test_client()
        self.assertEqual(client.get("/api/auth").json, {"authenticated": True, "required": False})
        self.assertEqual(client.get("/api/warm").status_code, 200)

    def test_injected_test_service_can_exercise_routes_without_access_code(self):
        client = create_app(self.service).test_client()
        self.assertEqual(client.get("/api/auth").json, {"authenticated": True, "required": False})
        self.assertEqual(client.get("/api/warm").status_code, 200)


if __name__ == "__main__":
    unittest.main()
