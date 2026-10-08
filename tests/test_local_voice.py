"""Offline voice recognition and the phone's local transcription mode.

The point of `phone.transcription = "local"` is that a spoken instruction never
reaches a third party. These tests cover the parts that decide whether that is
actually true, without loading a speech model or touching the network.
"""
import base64
import io
import struct
import unittest
import wave

from tests._support import ROOT  # noqa: F401


def make_wav(seconds=0.25, rate=16_000, channels=1):
    """A 16-bit PCM WAV of a quiet tone, enough to exercise the decoder."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(3000 * ((i % 100) / 100.0 - 0.5)))
            for i in range(int(rate * seconds) * channels))
        handle.writeframes(frames)
    return buffer.getvalue()


class TestWhisperLanguage(unittest.TestCase):
    """The page and the config both say `en-US`; the model only takes `en`.

    Passing the tag straight through returned a 400 for every local-mode
    instruction, which is the one path the whole offline setup depends on.
    """

    def test_a_region_tag_is_reduced_to_its_base(self):
        from ufo.voice import _whisper_language

        for tag in ("en-US", "en-GB", "pt-BR", "zh-Hans-CN"):
            with self.subTest(tag=tag):
                self.assertEqual(_whisper_language(tag), tag.split("-")[0])

    def test_a_bare_code_is_kept(self):
        from ufo.voice import _whisper_language

        for code in ("en", "de", "hi", "yue"):
            with self.subTest(code=code):
                self.assertEqual(_whisper_language(code), code)

    def test_case_and_underscores_do_not_matter(self):
        from ufo.voice import _whisper_language

        self.assertEqual(_whisper_language("EN_us"), "en")

    def test_unknown_and_empty_fall_back_to_detection(self):
        """Returning a bad code would raise; returning None lets whisper decide."""
        from ufo.voice import _whisper_language

        for value in ("", "   ", "klingon", "xx-YY"):
            with self.subTest(value=value):
                self.assertIsNone(_whisper_language(value))

    def test_the_real_config_default_is_accepted(self):
        from ufo.config import Config
        from ufo.voice import _whisper_language

        self.assertEqual(_whisper_language(Config().phone.language), "en")


class TestWavDecoding(unittest.TestCase):
    """UFO decodes the audio itself rather than through PyAV."""

    def test_mono_at_target_rate_is_returned_unchanged(self):
        from ufo.voice import _wav_to_float32

        samples = _wav_to_float32(make_wav(rate=16_000))
        self.assertEqual(samples.dtype.name, "float32")
        self.assertAlmostEqual(len(samples), 16_000 * 0.25, delta=8)

    def test_values_stay_inside_the_normalised_range(self):
        from ufo.voice import _wav_to_float32

        samples = _wav_to_float32(make_wav())
        self.assertGreater(float(samples.min()), -1.0)
        self.assertLessEqual(float(samples.max()), 1.0)

    def test_a_phone_rate_is_resampled_to_sixteen_k(self):
        """Phones record at 44.1 or 48 kHz; the model wants 16 kHz."""
        from ufo.voice import _wav_to_float32

        for rate in (44_100, 48_000):
            with self.subTest(rate=rate):
                samples = _wav_to_float32(make_wav(seconds=0.5, rate=rate))
                self.assertAlmostEqual(len(samples), 16_000 * 0.5,
                                       delta=int(0.5 * rate * 0.02) + 32)

    def test_stereo_is_mixed_down_to_mono(self):
        from ufo.voice import _wav_to_float32

        samples = _wav_to_float32(make_wav(seconds=0.2, channels=2))
        self.assertAlmostEqual(len(samples), 16_000 * 0.2, delta=8)

    def test_eight_bit_audio_is_refused_loudly(self):
        """Silently misreading it would transcribe noise as a real instruction."""
        from ufo.voice import _wav_to_float32

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(1)
            handle.setframerate(16_000)
            handle.writeframes(bytes(4_000))
        with self.assertRaises(ValueError):
            _wav_to_float32(buffer.getvalue())

    def test_garbage_is_not_silently_transcribed(self):
        from ufo.voice import _wav_to_float32

        with self.assertRaises(Exception):
            _wav_to_float32(b"this is not a wav file at all")


class TestLocalTranscriptionEntryPoint(unittest.TestCase):
    def test_empty_audio_short_circuits(self):
        """An empty recording must not load a 150 MB model to say nothing."""
        from ufo.voice import transcribe_wav_bytes

        self.assertEqual(transcribe_wav_bytes(b""), "")

    def test_missing_engine_raises_a_named_runtime_error(self):
        from unittest import mock

        from ufo.voice import WhisperSTT, transcribe_wav_bytes

        saved = (WhisperSTT._model, WhisperSTT._model_name, WhisperSTT._engine)
        WhisperSTT._model = WhisperSTT._model_name = WhisperSTT._engine = None
        try:
            with mock.patch.dict("sys.modules", {"faster_whisper": None,
                                                "whisper": None}):
                with self.assertRaises(RuntimeError) as ctx:
                    transcribe_wav_bytes(make_wav())
        finally:
            (WhisperSTT._model, WhisperSTT._model_name,
             WhisperSTT._engine) = saved
        self.assertIn("whisper", str(ctx.exception).lower())


class TestPhonePageMode(unittest.TestCase):
    def test_local_mode_reaches_the_page(self):
        from ufo.voice.phone import render_page

        page = render_page("en-US", "local")
        self.assertIn('const MODE = "local";', page)

    def test_browser_mode_still_works(self):
        from ufo.voice.phone import render_page

        self.assertIn('const MODE = "browser";', render_page("en-US", "browser"))

    def test_gemini_mode_can_never_reach_the_page(self):
        """Gemini sent audio to Google. This install is local-only.

        The mode is not merely unlisted: an unrecognised mode falls back to
        "local", so a stale config cannot resurrect the cloud recogniser.
        """
        from ufo.voice.phone import render_page

        page = render_page("en-US", "gemini")
        self.assertNotIn('const MODE = "gemini";', page)
        self.assertIn('const MODE = "local";', page)

    def test_an_unknown_mode_falls_back_to_local(self):
        """Never let a typo quietly pick a different recogniser."""
        from ufo.voice.phone import render_page

        self.assertIn('const MODE = "local";',
                      render_page("en-US", "not-a-mode"))

    def test_local_mode_records_audio_instead_of_using_web_speech(self):
        from ufo.voice.phone import render_page

        page = render_page("en-US", "local")
        self.assertIn('(MODE === "local") ? await recordWav(6)', page)
        self.assertIn('if (MODE === "browser") startRecognition();', page)


class TestPhoneTranscriptionRouting(unittest.TestCase):
    """`_transcribe` must not reach for a cloud model in local mode."""

    def _listener(self, transcription):
        from ufo.voice.phone import PhoneListener, PhoneListenerConfig

        cfg = PhoneListenerConfig(transcription=transcription)
        listener = PhoneListener.__new__(PhoneListener)
        listener.cfg = cfg
        listener.requests = 0
        listener.last = None
        return listener

    def _post(self, listener, audio_b64, language="en-US"):
        return listener._transcribe(audio_b64, {"language": language})

    def test_local_mode_calls_the_local_engine(self):
        from unittest import mock

        listener = self._listener("local")
        audio = base64.b64encode(make_wav()).decode()
        with mock.patch("ufo.voice.transcribe_wav_bytes",
                        return_value="open notepad") as engine:
            self.assertEqual(self._post(listener, audio), "open notepad")
        self.assertEqual(engine.call_count, 1)

    def test_local_mode_never_touches_the_cloud_module(self):
        from unittest import mock

        listener = self._listener("local")
        audio = base64.b64encode(make_wav()).decode()
        with mock.patch("ufo.voice.model_speech.transcribe_wav") as cloud:
            with mock.patch("ufo.voice.transcribe_wav_bytes", return_value="x"):
                self._post(listener, audio)
        cloud.assert_not_called()

    def test_gemini_mode_still_uses_the_model_module(self):
        from unittest import mock

        listener = self._listener("gemini")
        audio = base64.b64encode(make_wav()).decode()
        with mock.patch("ufo.voice.model_speech.transcribe_wav",
                        return_value="from the cloud") as cloud:
            self.assertEqual(self._post(listener, audio), "from the cloud")
        self.assertEqual(cloud.call_count, 1)

    def test_a_missing_local_engine_explains_the_fix(self):
        from unittest import mock

        listener = self._listener("local")
        audio = base64.b64encode(make_wav()).decode()
        with mock.patch("ufo.voice.transcribe_wav_bytes",
                        side_effect=RuntimeError("no engine")):
            with self.assertRaises(ValueError) as ctx:
                self._post(listener, audio)
        self.assertIn("faster-whisper", str(ctx.exception))

    def test_bad_base64_is_rejected(self):
        listener = self._listener("local")
        with self.assertRaises(ValueError):
            self._post(listener, "not base64 !!!")


if __name__ == "__main__":
    unittest.main()
