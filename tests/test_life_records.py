"""Category 32 and the local record store that replaces provider refusals."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ufo.actions import life


class _Home(unittest.TestCase):
    """Every test gets its own state directory, so records never leak."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._patches = [
            mock.patch.object(life, "_life_home", lambda: self.tmp),
            mock.patch.object(life, "_finance_home", lambda: self.tmp / "fin"),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.tmp / "fin").mkdir(parents=True, exist_ok=True)


class TestRecordStore(_Home):

    def test_save_and_load(self):
        life.save_record("trip", "LHR to DEL", when="14 May")
        rows = life.load_records("trip")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["when"], "14 May")

    def test_saving_the_same_name_updates_rather_than_duplicates(self):
        life.save_record("trip", "LHR to DEL", when="14 May")
        life.save_record("trip", "lhr to del", when="15 May")
        rows = life.load_records("trip")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["when"], "15 May")

    def test_kinds_are_kept_apart(self):
        life.save_record("trip", "x")
        life.save_record("parcel", "x")
        self.assertEqual(len(life.load_records("trip")), 1)
        self.assertEqual(len(life.load_records("parcel")), 1)

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(Exception):
            life.save_record("nonsense", "x")

    def test_forget(self):
        life.save_record("trip", "LHR to DEL")
        self.assertTrue(life.forget_record("LHR to DEL"))
        self.assertEqual(life.load_records(), [])

    def test_forgetting_what_is_not_there(self):
        self.assertFalse(life.forget_record("nothing"))

    def test_corrupt_store_reads_as_empty(self):
        (self.tmp / "records.json").write_text("{oops", encoding="utf-8")
        self.assertEqual(life.load_records(), [])


class TestRecordTrip(_Home):

    def test_date_is_split_out_of_the_route(self):
        # Splitting on the first " on " alone put "LHR to DEL" in the date.
        life.record_trip("LHR to DEL on 14 May, flight AI 173")
        row = life.load_records("trip")[0]
        self.assertEqual(row["name"], "LHR to DEL")
        self.assertEqual(row["when"], "14 May")
        self.assertIn("AI 173", row["detail"])

    def test_trip_without_a_date(self):
        life.record_trip("just the train to Leeds")
        row = life.load_records("trip")[0]
        self.assertEqual(row["when"], "")

    def test_itinerary_reads_records_back_and_says_they_are_not_live(self):
        life.record_trip("LHR to DEL on 14 May")
        out = life.itinerary()
        self.assertIn("LHR to DEL", out)
        self.assertIn("not live provider data", out)

    def test_itinerary_when_empty_asks_for_a_trip(self):
        self.assertIn("not told me about any trips", life.itinerary())


class TestFlightStatus(_Home):

    def test_never_claims_live_data(self):
        out = life.flight_status("AI 173")
        self.assertNotIn("on time", out.lower())
        self.assertIn("cannot see the live board", out)

    def test_reads_a_recorded_flight_back(self):
        life.record_trip("LHR to DEL on 14 May, flight AI 173")
        out = life.flight_status("AI 173")
        self.assertIn("AI 173", out)
        self.assertIn("not live provider data", out)

    def test_asks_for_a_number(self):
        self.assertIn("give me the flight number", life.flight_status())


class TestParcel(_Home):

    def test_due_date_in_args_makes_a_reminder(self):
        # `target or args` threw the due date away here, so the reminder silently
        # never happened whenever a number was also present.
        with mock.patch("ufo.actions.comms.remind", return_value="ok") as rem:
            out = life.track_parcel("TRK123", "friday")
        self.assertTrue(rem.called, out)
        self.assertIn("friday", out)
        self.assertIn("friday", str(rem.call_args))

    def test_due_date_inline_in_the_target(self):
        with mock.patch("ufo.actions.comms.remind", return_value="ok") as rem:
            out = life.track_parcel("TRK123 due friday", "")
        self.assertTrue(rem.called, out)

    def test_no_due_date_records_without_reminding(self):
        with mock.patch("ufo.actions.comms.remind") as rem:
            out = life.track_parcel("TRK123", "")
        self.assertFalse(rem.called)
        self.assertIn("noted", out)
        self.assertEqual(len(life.load_records("parcel")), 1)

    def test_asks_for_a_number(self):
        self.assertIn("give me the tracking number", life.track_parcel())


class TestBoardingPass(_Home):

    def test_needs_something_to_search_for(self):
        self.assertIn("give me something to look for",
                      life.boarding_pass())

    def test_finds_and_opens_a_local_file(self):
        folder = self.tmp / "Downloads"
        folder.mkdir()
        (folder / "AI173-boarding-pass.pdf").write_text("x", encoding="utf-8")
        with mock.patch.object(Path, "home", staticmethod(lambda: self.tmp)), \
             mock.patch.object(life.shell, "run_powershell") as run:
            out = life.boarding_pass("AI 173")
        self.assertIn("AI173-boarding-pass.pdf", out)
        self.assertTrue(run.called)

    def test_no_match_says_so_rather_than_opening_something_random(self):
        with mock.patch.object(Path, "home", staticmethod(lambda: self.tmp)), \
             mock.patch.object(life.shell, "run_powershell") as run:
            out = life.boarding_pass("ZZ999")
        self.assertIn("no boarding pass matching", out)
        self.assertFalse(run.called)


class TestSubscriptions(_Home):

    def test_empty_tells_the_user_what_to_do(self):
        self.assertIn("subscriptions add", life.subscription())

    def test_csv_is_kept_alongside_the_records(self):
        (self.tmp / "fin" / "subscriptions.csv").write_text(
            "name,amount,renews\nNetflix,15,2026-01-01\n", encoding="utf-8")
        life.save_record("subscription", "Spotify", amount="9.99")
        out = life.subscription()
        self.assertIn("Netflix", out)
        self.assertIn("Spotify", out)
        self.assertIn("not the providers' live billing data", out)


class TestProviderRefusalsStillExplainThemselves(_Home):

    def test_needs_provider_points_at_the_local_alternative(self):
        out = life._needs_provider("live tracking")
        self.assertIn("will not guess", out)
        self.assertIn("local record", out)


if __name__ == "__main__":
    unittest.main()