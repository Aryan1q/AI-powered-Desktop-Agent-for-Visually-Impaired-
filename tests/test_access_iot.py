"""Categories 25 and 26: smart home and accessibility."""
import unittest
from unittest import mock

from ufo.actions import access, life
from ufo.capabilities import catalog


class TestIoTDiscovery(unittest.TestCase):

    def test_vendor_lookup_uses_three_octets(self):
        # The OUI is the first three octets. Joining the first two matches
        # nothing, which is how every device came back unlabelled.
        self.assertEqual(life._vendor_of("B8:27:EB:11:22:33"), "Raspberry Pi")
        self.assertEqual(life._vendor_of("68:54:FD:AA:BB:CC"), "Amazon")

    def test_unknown_vendor_is_blank_not_guessed(self):
        self.assertEqual(life._vendor_of("AA:BB:CC:DD:EE:FF"), "")

    def test_short_or_empty_mac_is_blank(self):
        self.assertEqual(life._vendor_of(""), "")
        self.assertEqual(life._vendor_of("B8:27"), "")

    def test_discovery_announces_that_nothing_was_sent(self):
        with mock.patch.object(life, "_ssdp_devices", return_value=[]), \
             mock.patch.object(life, "_neighbours", return_value=[]):
            out = life.iot_discover()
        self.assertIn("nothing has been sent", out)

    def test_discovery_names_a_known_device_type(self):
        found = [("192.168.1.5", "urn:google-cast-service", "", "Google Inc")]
        with mock.patch.object(life, "_ssdp_devices", return_value=found), \
             mock.patch.object(life, "_neighbours", return_value=[]):
            out = life.iot_discover()
        self.assertIn("Chromecast", out)
        self.assertIn("192.168.1.5", out)

    def test_unknown_service_type_is_reported_not_guessed(self):
        found = [("192.168.1.9", "urn:made:up:thing", "", "")]
        with mock.patch.object(life, "_ssdp_devices", return_value=found), \
             mock.patch.object(life, "_neighbours", return_value=[]):
            out = life.iot_discover()
        self.assertIn("urn:made:up:thing", out)
        self.assertIn("nothing has been sent", out)

    def test_neighbour_rows_are_parsed(self):
        script_output = "10.0.0.1|AA:BB:CC:DD:EE:FF|router\n\n"
        with mock.patch.object(life, "_run_ps", return_value=script_output):
            rows = life._neighbours()
        self.assertEqual(rows, [("10.0.0.1", "AA:BB:CC:DD:EE:FF", "router")])

    def test_bad_json_is_not_an_error(self):
        with mock.patch.object(life, "_run_ps", return_value="not json"):
            self.assertEqual(life._ssdp_devices(), [])


class TestIoTControl(unittest.TestCase):

    def test_verb_must_be_on_off_or_toggle(self):
        out = life.iot_control("the lamp", "sideways")
        self.assertIn("on, off or toggle", out)

    def test_unknown_device_is_not_guessed_at(self):
        with mock.patch.object(life, "_ssdp_devices", return_value=[]):
            out = life.iot_control("the lamp", "off")
        self.assertIn("found no device I can speak to", out)
        self.assertIn("vendor's app", out)

    def test_airplay_is_declined_because_the_protocol_is_private(self):
        found = [("192.168.1.7", "urn:apple:device:AirPlay:1", "", "")]
        with mock.patch.object(life, "_ssdp_devices", return_value=found):
            out = life.iot_control("the speaker", "off")
        self.assertIn("AirPlay", out)
        self.assertIn("will not guess", out)

    def test_cast_device_gets_a_documented_message(self):
        found = [("192.168.1.5", "urn:google-cast-service", "", "")]
        with mock.patch.object(life, "_ssdp_devices", return_value=found), \
             mock.patch.object(life, "_run_ps", return_value="200") as run:
            out = life.iot_control("the chromecast", "off")
        self.assertIn("192.168.1.5", out)
        script = run.call_args[0][0]
        self.assertIn("8009/v2/ipc", script)
        self.assertIn("PAUSE", script)

    def test_cast_error_is_reported_not_swallowed(self):
        found = [("192.168.1.5", "urn:google-cast-service", "", "")]
        with mock.patch.object(life, "_ssdp_devices", return_value=found), \
             mock.patch.object(life, "_run_ps", return_value="error: timed out"):
            out = life.iot_control("the chromecast", "off")
        self.assertIn("did not answer", out)

    def test_cast_on_asks_status_without_changing_anything(self):
        found = [("192.168.1.5", "urn:google-cast-service", "", "")]
        with mock.patch.object(life, "_ssdp_devices", return_value=found), \
             mock.patch.object(life, "_run_ps", return_value="200") as run:
            out = life.iot_control("the chromecast", "on")
        self.assertIn("nothing was changed", out)
        self.assertIn("GET_STATUS", run.call_args[0][0])


class TestIoTScene(unittest.TestCase):

    def test_scene_needs_a_name(self):
        self.assertIn("which scene", life.iot_scene())

    def test_known_app_is_opened_and_the_limit_stated(self):
        with mock.patch.object(life.shell, "run_powershell") as run:
            out = life.iot_scene("movie night in smartthings", "")
        self.assertTrue(run.called)
        self.assertIn("its own to run", out)

    def test_unknown_scene_points_at_home_assistant(self):
        with mock.patch.object(life.shell, "run_powershell") as run:
            out = life.iot_scene("the wombat scene", "")
        self.assertFalse(run.called)
        self.assertIn("Home Assistant", out)


class TestAccessibilityWiring(unittest.TestCase):

    def test_every_advertised_setting_has_its_own_capability(self):
        names = {c.name for c in catalog.iter_capabilities([26])}
        for expected in ("access_screen_reader", "access_high_contrast",
                         "access_text_size", "access_sticky_keys",
                         "access_color_filters", "access_captions",
                         "access_voice_typing", "access_status"):
            self.assertIn(expected, names)

    def test_screen_reader_is_not_miswired_to_the_settings_page(self):
        cap = catalog.get("access_screen_reader")
        self.assertEqual(cap.handler, "access:screen_reader")

    def test_every_accessibility_handler_exists(self):
        import importlib

        for c in catalog.iter_capabilities([26]):
            mod, _, fn = c.handler.partition(":")
            m = importlib.import_module(f"ufo.actions.{mod}")
            self.assertTrue(callable(getattr(m, fn, None)), c.handler)


class TestAccessibilityBehaviour(unittest.TestCase):

    def test_sticky_keys_reports_how_to_undo_itself(self):
        # A stuck modifier silently changes what every key press means, so the
        # handler has to say how to turn it back off.
        with mock.patch.object(access.shell, "run_powershell") as run:
            out = access.sticky_keys("on", "")
        self.assertIn("sticky keys on", out)
        self.assertIn("off", out)
        self.assertIn("510", run.call_args[0][0])

    def test_sticky_keys_off_uses_the_default_flags(self):
        with mock.patch.object(access.shell, "run_powershell") as run:
            out = access.sticky_keys("off", "")
        self.assertIn("sticky keys off", out)
        self.assertIn("506", run.call_args[0][0])

    def test_colour_filters_toggle(self):
        with mock.patch.object(access.shell, "run_powershell") as run:
            self.assertIn("colour filters on", access.color_filters("on", ""))
            self.assertIn("'1'", run.call_args[0][0])
            self.assertIn("colour filters off", access.color_filters("off", ""))
            self.assertIn("'0'", run.call_args[0][0])

    def test_voice_typing_off_does_not_open_the_panel(self):
        with mock.patch.object(access.shell, "run_powershell") as run:
            access.voice_typing("off", "")
        self.assertNotIn("ms-settings:speech", run.call_args[0][0])

    def test_voice_typing_on_opens_the_speech_panel(self):
        with mock.patch.object(access.shell, "run_powershell") as run:
            out = access.voice_typing("on", "")
        self.assertIn("ms-settings:speech", run.call_args[0][0])
        self.assertIn("Win+H", out)

    def test_text_size_is_clamped(self):
        with mock.patch.object(access.shell, "run_powershell") as run:
            out = access.text_size("5000", "")
        self.assertIn("500%", out)
        self.assertIn("480", run.call_args[0][0])       # 500% of 96 DPI

    def test_text_size_reset_returns_to_normal(self):
        with mock.patch.object(access.shell, "run_powershell") as run:
            out = access.text_size("reset", "")
        self.assertIn("back to normal", out)
        self.assertIn("96", run.call_args[0][0])

    def test_status_reads_back_rather_than_reporting_what_was_asked(self):
        def fake(script, **kw):
            if "AppliedFontDPI" in script:
                return "192"
            if "HighContrast" in script:
                return "02"
            if "Get-Process" in script:
                return "yes"
            return ""

        with mock.patch.object(access, "_run_powershell", side_effect=fake):
            out = access.status()
        self.assertIn("text scale", out)
        self.assertIn("200%", out)                     # 192 DPI, not "whatever you asked for"
        self.assertIn("narrator", out)
        self.assertIn("on", out)

    def test_status_survives_powershell_failing(self):
        with mock.patch.object(access, "_run_powershell", return_value=""):
            out = access.status()
        self.assertIn("accessibility settings", out)


if __name__ == "__main__":
    unittest.main()
