"""Category 21: real implementations, and the two-slot web fixes."""
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ufo import config as config_mod
from ufo.actions import info, web


class _Home(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        cfg = config_mod.Config()
        try:
            cfg.storage.home = str(self.tmp)
        except Exception:
            pass
        self._previous = config_mod.active()
        config_mod.set_active(cfg)
        self.addCleanup(config_mod.set_active, self._previous)


class TestCalculatedExactly(_Home):
    """Arithmetic and unit conversion never touch a model."""

    def test_order_of_operations(self):
        self.assertEqual(info.calculate("2 + 2 * 17", ""), "2 + 2 * 17 = 36")

    def test_parentheses_and_power(self):
        self.assertIn("= 64", info.calculate("(2 + 2) ** 3", ""))
        self.assertIn("= 256", info.calculate("2 ^ 8", ""))

    def test_percent_of(self):
        self.assertIn("= 15", info.calculate("15% of 100", ""))
        self.assertIn("= 30", info.calculate("20 percent of 150", ""))

    def test_it_refuses_non_arithmetic_rather_than_guessing(self):
        out = info.calculate("what is the meaning of life", "")
        self.assertIn("not arithmetic", out)

    def test_nothing_asked_for(self):
        self.assertIn("say what to work out", info.calculate("", ""))

    def test_length_conversion(self):
        out = info.convert_units("5 km in miles")
        self.assertIn("miles", out)
        self.assertAlmostEqual(float(out.split("=")[1].split()[0]), 3.10686, places=4)

    def test_mass_conversion(self):
        out = info.convert_units("2 kg to grams")
        self.assertIn("2000", out.replace(",", ""))

    def test_volume_conversion(self):
        self.assertIn("3.785", info.convert_units("1 gallon in litres"))

    def test_temperature_conversion(self):
        out = info.convert_units("100 c to f")
        self.assertIn("212", out)

    def test_temperature_fahrenheit_to_celsius(self):
        self.assertIn("37.77", info.convert_units("100 f to c"))

    def test_mismatched_families_are_refused(self):
        out = info.convert_units("5 km to kg")
        self.assertIn("different things", out)

    def test_conversion_reaches_calculate(self):
        self.assertIn("miles", info.calculate("5 km in miles", ""))

    def test_an_unknown_unit_is_not_a_conversion(self):
        self.assertIsNone(info.convert_units("5 blorks in flurbs"))


class TestNoExternalDataSources(unittest.TestCase):
    """Live data needs a public service, and this install has none.

    These handlers used to call open-meteo, Google News, OpenStreetMap, OSRM
    and an exchange-rate feed. They stay registered — a capability that has gone
    unavailable must say so, not vanish mid-sentence — but they must answer from
    the local install only, with no request going out.
    """

    #: Every handler that used to reach the network, with the thing it needs.
    HANDLERS = (("weather", "weather"),
                ("news", "news"),
                ("directions", "directions"),
                ("currency", "currency"))

    def test_none_of_them_attempt_a_request(self):
        """The real guarantee: no socket is opened, whatever the user says."""
        for name, _ in self.HANDLERS:
            with self.subTest(handler=name):
                def boom(*a, **k):
                    raise AssertionError(f"{name} tried to reach the network")

                with mock.patch.object(info, "_get_json", boom), \
                        mock.patch.object(info, "_get_text", boom), \
                        mock.patch.object(info.urllib.request, "urlopen", boom):
                    getattr(info, name)("London", "GBP to JPY")

    def test_each_one_admits_it_needs_a_public_service(self):
        """A missing capability has to say so, not answer from memory.

        A local model asked for the weather would happily invent one. Saying
        "this comes from a public service" is the honest answer.
        """
        for name, needle in self.HANDLERS:
            with self.subTest(handler=name):
                out = getattr(info, name)("London", "")
                self.assertIn("no local source", out.lower())
                self.assertIn("public service", out.lower())
                self.assertIn("Ollama-only", out)
                self.assertIn(needle, out.lower())

    def test_they_do_not_invent_a_number(self):
        """No fabricated temperature, rate or distance."""
        out = info.currency("100", "GBP to JPY")
        self.assertNotIn("20,000", out)
        self.assertNotIn("200.0000", out)



class TestModelBacked(_Home):
    """Understanding goes to the model, and says so when there is not one."""

    def test_no_model_is_reported_not_faked(self):
        cfg = config_mod.active()
        cfg.llm.provider = "gemini"
        cfg.llm.model = ""
        cfg.llm.base_url = ""
        with mock.patch.dict(os.environ, {}, clear=True):
            out = info.answer("what is the capital of France", "")
        # Either a real answer, or an honest refusal. Never a fabricated one.
        self.assertTrue(
            "will not guess" in out or "no model is configured" in out
            or "Paris" in out, out)

    def test_a_model_failure_never_becomes_an_answer(self):
        from ufo.errors import LLMError

        with mock.patch("ufo.agent.llm.Client") as client:
            client.return_value.complete.side_effect = LLMError("provider down")
            out = info.explain("recursion", "for a beginner")
        self.assertIn("will not guess", out)
        self.assertNotIn("recursion is", out)

    def test_a_real_answer_is_passed_through(self):
        from ufo.agent.llm import Response

        with mock.patch("ufo.agent.llm.Client") as client:
            client.return_value.complete.return_value = Response(
                text="It means to explain itself again.", model="test",
                provider="test")
            out = info.explain("recursion", "for a beginner")
        self.assertIn("explain itself again", out)

    def test_translate_asks_for_a_language_rather_than_guessing_one(self):
        out = info.translate("good morning", "")
        self.assertIn("which language", out)

    def test_read_aloud_speaks_or_prints_but_never_claims_silently(self):
        out = info.read_aloud("hello there", "")
        # A voice may exist on the test machine or not; either way the text must
        # have been read or shown, never dropped.
        self.assertTrue("read" in out or "no speech voice" in out, out)
        if "no speech voice" in out:
            self.assertIn("hello there", out)


class TestWebTwoSlotFixes(_Home):
    """These handlers used `target or args` and dropped the second half."""

    def test_scrape_uses_the_mode_not_just_the_url(self):
        page = ('<html><head><title>The Title</title></head><body>'
                '<a href="https://x.test/one">one</a>'
                '<a href="https://x.test/two">two</a>'
                '<table><tr><th>a</th><td>1</td></tr></table>'
                '</body></html>')
        with mock.patch("ufo.actions.network.fetch_text", return_value=page):
            links = web.scrape("https://x.test", "links")
            title = web.scrape("https://x.test", "title")
            table = web.scrape("https://x.test", "table")
        self.assertIn("https://x.test/one", links)
        self.assertIn("https://x.test/two", links)
        self.assertEqual(title.strip(), "The Title")
        self.assertIn("a | 1", table)

    def test_scrape_without_a_mode_still_returns_text(self):
        with mock.patch("ufo.actions.network.fetch_text",
                        return_value="<body>hello world</body>"):
            self.assertIn("hello world", web.scrape("https://x.test", ""))

    def test_download_honours_the_destination(self):
        with mock.patch("ufo.actions.network.download",
                        return_value="ok") as fetch:
            out = web.download("https://x.test/file.zip", "D:/downloads")
        self.assertIn("D:/downloads", out)
        self.assertIn("file.zip", fetch.call_args[0][1])
        self.assertNotIn("Downloads\\", fetch.call_args[0][1])

    def test_download_defaults_to_the_downloads_folder(self):
        with mock.patch("ufo.actions.network.download",
                        return_value="ok") as fetch:
            out = web.download("https://x.test/file.zip", "")
        self.assertIn("Downloads", out)
        self.assertIn("file.zip", fetch.call_args[0][1])

    def test_block_and_unblock_give_different_answers(self):
        self.assertIn("hosts file", web.block_site("example.com", "block"))
        self.assertIn("unblock", web.block_site("example.com", "unblock").lower())

    def test_bookmark_list_does_not_open_the_manager(self):
        self.assertIn("browser profile", web.bookmark("example.com", "list"))

    def test_bookmark_add_explains_the_safe_route(self):
        self.assertIn("corrupt", web.bookmark("example.com", "add").lower())

    def test_clear_data_understands_what_versus_where(self):
        # "cookies" is the *what*; the old code read it as a site name.
        with mock.patch.object(web, "_clear_site") as clear_site, \
             mock.patch.object(web, "_clear_all") as clear_all:
            web.clear_data("cookies", "example.com")
        self.assertTrue(clear_site.called)
        self.assertFalse(clear_all.called)

    def test_clear_data_all_with_no_site(self):
        with mock.patch.object(web, "_clear_all") as clear_all:
            web.clear_data("cache", "")
        self.assertTrue(clear_all.called)
        self.assertEqual(clear_all.call_args[0][0], "cache")

    def test_purchase_mentions_the_site_when_given(self):
        with mock.patch.object(web, "search"):
            self.assertIn("shop.test", web.purchase("a laptop", "shop.test"))


if __name__ == "__main__":
    unittest.main()