"""Login-URL guard: a real password must never be typed into an arbitrary page."""
import unittest

from config import Config, host_allowed


class UrlGuardTests(unittest.TestCase):
    def test_known_portals_are_allowed(self):
        for url in [
            "https://mc.manuscriptcentral.com/journal-name",
            "https://mc.manuscriptcentral.com",
            "https://www.editorialmanager.com/example",
            "https://editorialmanager.com/example",
            "https://sub.manuscriptcentral.com/x",
        ]:
            with self.subTest(url=url):
                self.assertIsNone(Config.url_problem(url))

    def test_plain_http_is_rejected(self):
        problem = Config.url_problem("http://mc.manuscriptcentral.com/journal")
        self.assertIsNotNone(problem)
        self.assertIn("https", problem or "")

    def test_unknown_host_is_rejected(self):
        self.assertIsNotNone(Config.url_problem("https://evil.example/login"))
        # A suffix that merely contains an allowed domain must not pass.
        self.assertIsNotNone(Config.url_problem("https://manuscriptcentral.com.evil.example/"))

    def test_local_and_file_urls_are_rejected(self):
        self.assertIsNotNone(Config.url_problem("file:///etc/passwd"))
        self.assertIsNotNone(Config.url_problem("http://127.0.0.1:8080/login"))
        self.assertIsNotNone(Config.url_problem(""))

    def test_allowlist_can_be_extended_deliberately(self):
        self.assertTrue(host_allowed("https://portal.example-journal.org/login", ["example-journal.org"]))
        self.assertFalse(host_allowed("https://portal.example-journal.org/login", ["manuscriptcentral.com"]))


if __name__ == "__main__":
    unittest.main()