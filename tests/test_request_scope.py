import unittest

from backend.request_scope import unsupported_request


class RequestScopeTests(unittest.TestCase):
    def test_multi_guest_requests_get_a_single_guest_choice(self):
        for phrase in (
            "Book a call with Iris and Lee tomorrow.",
            "Schedule Rowan Fox and Sandy Quinn for 3pm.",
            "Book a demo with iris & lee.",
            "Arrange a meeting with Iris (iris@example.com) and Lee.",
            "Book a chat for Iris and the design manager.",
            "Book iris@example.com and lee@example.com for an intro.",
            "Book a call for three people.",
            "Add Morgan to that meeting.",
        ):
            with self.subTest(phrase=phrase):
                reply = unsupported_request(phrase)
                self.assertIn("one guest", reply or "")
                self.assertNotIn("example.com", reply or "")
        self.assertIn("one guest", unsupported_request("iris@example.com; lee@example.com", collecting_guest=True) or "")
        self.assertIn("one guest", unsupported_request("Also invite Rowan.", collecting_guest=True) or "")

    def test_series_is_an_explicit_choice_to_book_one_occurrence(self):
        for phrase in (
            "Set up a weekly catch-up Friday at 2.",
            "Schedule a call every other Monday with Kim.",
            "Book a monthly check-in with a candidate.",
            "Reserve time for a meeting series.",
            "Book a meeting with Kim on Tuesdays at 2pm.",
        ):
            with self.subTest(phrase=phrase):
                reply = unsupported_request(phrase)
                self.assertIn("Would you like to book the first occurrence?", reply or "")
        self.assertIn("one occurrence", unsupported_request("Move the entire series back an hour.") or "")
        self.assertIn("first occurrence", unsupported_request("Every Tuesday at 9.", collecting_guest=True) or "")
        self.assertIsNone(unsupported_request("Book the first occurrence on Friday at 2pm with Kim."))

    def test_personal_calendar_blocks_redirect_to_the_calendar_app(self):
        for phrase in (
            "Block off 2–3pm tomorrow on my calendar.",
            "Hold 9am to 10am on my calendar for lunch.",
            "Mark me busy Thursday afternoon.",
            "Schedule focus time tomorrow.",
            "Set up a personal calendar block at 3pm.",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn("add a busy event in your calendar app", unsupported_request(phrase) or "")

    def test_supported_requests_keep_their_normal_path(self):
        for phrase in (
            "What's on my schedule next week?",
            "Show my recurring meetings.",
            "Move this week's weekly check-in to 3pm.",
            "Book with Iris tomorrow and send her the link.",
            "Book with Iris tomorrow and leave a note.",
            "Book a call with Iris Tuesday or Friday.",
            "Book a meeting with Iris, iris@example.com, tomorrow.",
            "Reserve a time between Monday and Wednesday.",
            "Book a meeting about the quarterly roadmap with Iris.",
            "What times do I have open for lunch tomorrow?",
        ):
            with self.subTest(phrase=phrase):
                self.assertIsNone(unsupported_request(phrase))
        self.assertIsNone(unsupported_request("iris@example.com and IRIS@example.com", collecting_guest=True))


if __name__ == "__main__":
    unittest.main()
