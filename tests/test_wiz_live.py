"""Offline tests for the optional wiz_live module.

These run with the standard library only: the effect math is pure Python and
the numpy-backed analysis path is exercised only when numpy is installed.
"""
import io
import os
import shutil
import sys
import types
import unittest
from contextlib import redirect_stdout
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wiz  # noqa: E402
import wiz_live  # noqa: E402


def options(**overrides):
    base = {
        "bpm": 165.0,
        "fps": 15.0,
        "rate_multiplier": 2,
        "rate": 22050,
        "block": 1024,
        "sensitivity": 1.5,
        "brightness_boost": 1.0,
        "dry_run": True,
        "duration": 0.0,
        "target": None,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


class ColourTests(unittest.TestCase):
    def test_parse_color_accepts_plain_and_hash_forms(self):
        self.assertEqual(wiz_live.parse_color("ff2e88"), (255, 46, 136))
        self.assertEqual(wiz_live.parse_color("#00E5FF"), (0, 229, 255))

    def test_parse_color_rejects_bad_input(self):
        for bad in ("", "fff", "gggggg", "ff2e8", None, 123456):
            with self.assertRaises(ValueError):
                wiz_live.parse_color(bad)

    def test_rgb_to_hex_clamps_and_formats(self):
        self.assertEqual(wiz_live.rgb_to_hex((255, 0, 0)), "ff0000")
        self.assertEqual(wiz_live.rgb_to_hex((300, -5, 128)), "ff0080")

    def test_scale_and_mix(self):
        self.assertEqual(wiz_live.scale_rgb((100, 200, 300), 0.5), (50, 100, 150))
        self.assertEqual(wiz_live.mix_rgb((0, 0, 0), (255, 255, 255), 0.5), (128, 128, 128))


class AnalysisTests(unittest.TestCase):
    def test_band_energies_splits_the_spectrum(self):
        freqs = [30.0, 100.0, 1000.0, 5000.0, 20000.0]
        magnitudes = [2.0, 0.0, 1.0, 3.0, 0.0]
        bass, mid, treble = wiz_live.band_energies(freqs, magnitudes)
        self.assertEqual(bass, 4.0)     # 30 Hz, magnitude 2
        self.assertEqual(mid, 1.0)      # 1000 Hz, magnitude 1
        self.assertEqual(treble, 9.0)   # 5000 Hz, magnitude 3

    def test_spectral_flux_only_counts_positive_change(self):
        self.assertEqual(wiz_live.spectral_flux(None, [1.0, 2.0]), 0.0)
        self.assertAlmostEqual(wiz_live.spectral_flux([1.0, 5.0], [3.0, 2.0]), 2.0)

    def test_normalize_levels_clamps_to_unit_range(self):
        self.assertEqual(wiz_live.normalize_levels([4.0, 1.0], 2.0), [1.0, 0.5])
        self.assertEqual(wiz_live.normalize_levels([9.0], 0.0), [0.0])

    def test_auto_gain_adapts_to_loud_input(self):
        gain = wiz_live.AutoGain()
        levels = []
        for _ in range(60):
            levels = gain.update([100.0, 100.0, 100.0])
        self.assertAlmostEqual(levels[0], 1.0)
        quiet = gain.update([1.0, 1.0, 1.0])
        self.assertLess(quiet[0], levels[0])

    def test_beat_detector_fires_on_a_spike(self):
        detector = wiz_live.BeatDetector(window=8, sensitivity=1.5)
        for _ in range(8):
            detector.feed(1.0)
        self.assertFalse(detector.feed(1.0))
        self.assertTrue(detector.feed(500.0))

    def test_beat_detector_is_quiet_before_it_has_history(self):
        detector = wiz_live.BeatDetector()
        self.assertFalse(detector.feed(50.0))


class EffectTests(unittest.TestCase):
    def test_caramelldansen_alternates_the_palette(self):
        # Two swaps per beat at 165 BPM means one swap every 1 / 5.5 seconds.
        swap = 60.0 / wiz_live.CARAMELLDANSEN_BPM / 2
        pink = wiz_live.parse_color("ff2e88")
        cyan = wiz_live.parse_color("00e5ff")
        self.assertEqual(wiz_live.caramelldansen_frame(0.0)[0], pink)
        self.assertEqual(wiz_live.caramelldansen_frame(swap * 1.1)[0], cyan)
        self.assertEqual(wiz_live.caramelldansen_frame(swap * 2.1)[0], pink)
        self.assertEqual(wiz_live.caramelldansen_frame(swap * 3.1)[0], cyan)

    def test_caramelldansen_brightness_punches_and_decays(self):
        swap = 60.0 / wiz_live.CARAMELLDANSEN_BPM / 2
        _rgb, at_hit = wiz_live.caramelldansen_frame(0.0)
        _rgb, midway = wiz_live.caramelldansen_frame(swap * 0.5)
        _rgb, before_next = wiz_live.caramelldansen_frame(swap * 0.9)
        self.assertEqual(at_hit, 100)
        self.assertGreater(at_hit, midway)
        self.assertGreater(midway, before_next)
        self.assertGreaterEqual(before_next, 10)

    def test_caramelldansen_dimming_stays_in_protocol_range(self):
        for step in range(200):
            _rgb, dimming = wiz_live.caramelldansen_frame(step * 0.037)
            self.assertGreaterEqual(dimming, 10)
            self.assertLessEqual(dimming, 100)

    def test_caramelldansen_respects_a_custom_bpm(self):
        # One beat at 60 BPM is one second, so half a beat is the swap.
        first = wiz_live.caramelldansen_frame(0.0, bpm=60.0)[0]
        second = wiz_live.caramelldansen_frame(0.6, bpm=60.0)[0]
        third = wiz_live.caramelldansen_frame(1.1, bpm=60.0)[0]
        self.assertNotEqual(first, second)
        self.assertEqual(first, third)

    def test_caramelldansen_needs_a_palette(self):
        with self.assertRaises(ValueError):
            wiz_live.caramelldansen_frame(0.0, colors=[])

    def test_mode_frames_stay_in_range_and_are_deterministic(self):
        levels = [0.9, 0.4, 0.2]
        for mode in wiz_live.SHOW_MODES:
            if mode == "caramelldansen":
                continue
            for index in range(3):
                rgb = wiz_live.mode_frame(mode, levels, 0.5, True, 0.25, index)
                self.assertEqual(len(rgb), 3, mode)
                for channel in rgb:
                    self.assertGreaterEqual(channel, 0, mode)
                    self.assertLessEqual(channel, 255, mode)
                self.assertEqual(rgb, wiz_live.mode_frame(mode, levels, 0.5, True, 0.25, index))
                dimming = wiz_live.mode_dimming(mode, 0.5, True)
                self.assertGreaterEqual(dimming, 10, mode)
                self.assertLessEqual(dimming, 100, mode)

    def test_multi_mode_gives_each_light_its_own_band(self):
        flat = [0.3, 0.6, 0.9]
        frames = [wiz_live.mode_frame("multi", flat, 0.5, False, 0.0, index)
                  for index in range(3)]
        self.assertEqual(len(set(frames)), 3)

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            wiz_live.mode_frame("nope", [0, 0, 0], 0.0, False, 0.0)

    def test_caramelldansen_frame_stream_advances(self):
        frames = []
        stream = wiz_live._caramelldansen_frames(options(), [{"id": "1"}],
                                                 [wiz_live.parse_color("ff0000"),
                                                  wiz_live.parse_color("0000ff")])
        for _ in range(4):
            frames.append(next(stream))
        self.assertEqual([moment for moment, _ in frames], [0.0, 1 / 15.0, 2 / 15.0, 3 / 15.0])
        # One frame per record, no more.
        self.assertTrue(all(len(payload) == 1 for _moment, payload in frames))


class DispatcherTests(unittest.TestCase):
    def test_normalize_target_accepts_both_forms(self):
        self.assertEqual(wiz_live.normalize_target("lamp"), "lamp")
        self.assertEqual(wiz_live.normalize_target("@lamp"), "lamp")
        self.assertEqual(wiz_live.normalize_target("  @lamp  "), "lamp")
        self.assertIsNone(wiz_live.normalize_target(None))
        with self.assertRaises(ValueError):
            wiz_live.normalize_target("@")

    def test_meme_matching_is_case_insensitive(self):
        self.assertEqual(
            wiz_live.meme_show_for("Caramelldansen (SpeedyCake Remix)"),
            "caramelldansen",
        )
        self.assertEqual(wiz_live.meme_show_for("CARAMELLDANSEN"), "caramelldansen")
        self.assertIsNone(wiz_live.meme_show_for("Some Other Song"))
        self.assertIsNone(wiz_live.meme_show_for(None))

    def test_detect_reports_a_missing_file(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = wiz_live.cmd_detect(["--file", "/definitely/not/here.wav"])
        self.assertEqual(code, 1)
        self.assertIn("not found", buffer.getvalue())

    def test_core_exposes_the_live_commands(self):
        for command in ("live", "shows", "detect", "caramelldansen"):
            self.assertIn(command, wiz.LIVE_COMMANDS)

    def test_core_routes_shows_to_the_optional_module(self):
        buffer = io.StringIO()
        with patch.object(sys, "argv", ["wiz", "shows"]), redirect_stdout(buffer):
            code = wiz.main()
        self.assertEqual(code, 0)
        self.assertIn("caramelldansen", buffer.getvalue())

    def test_unknown_live_command_exits_non_zero(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = wiz_live.main(["definitely-not-a-command"])
        self.assertEqual(code, 2)


class MetadataTests(unittest.TestCase):
    def test_live_interpreter_is_none_without_a_venv(self):
        with patch.object(wiz, "LIVE_VENV_PYTHONS", ()):
            self.assertIsNone(wiz._live_interpreter())

    def test_live_interpreter_returns_the_venv_python(self):
        with TemporaryDirectory() as tmp:
            python = os.path.join(tmp, "bin", "python")
            os.makedirs(os.path.dirname(python))
            with open(python, "w") as handle:
                handle.write("")
            with patch.object(wiz, "LIVE_VENV_PYTHONS", (python,)):
                self.assertEqual(wiz._live_interpreter(), python)

    def test_module_importable_matches_reality(self):
        self.assertTrue(wiz._module_importable("json"))
        self.assertFalse(wiz._module_importable("definitely_not_a_module"))

    def test_sibling_core_loads_the_installed_script_name(self):
        # The installed command is `wiz` with no .py suffix; the loader must
        # still find and execute it.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with TemporaryDirectory() as tmp:
            shutil.copyfile(os.path.join(root, "wiz.py"), os.path.join(tmp, "wiz"))
            with patch.object(wiz_live, "__file__", os.path.join(tmp, "wiz_live.py")):
                module = wiz_live._load_sibling_core()
                self.assertTrue(hasattr(module, "VERSION"))

    def test_sibling_core_reports_a_missing_core(self):
        with TemporaryDirectory() as tmp:
            with patch.object(wiz_live, "__file__", os.path.join(tmp, "wiz_live.py")):
                with self.assertRaises(wiz_live.MissingExtra):
                    wiz_live._load_sibling_core()

    def test_versions_agree(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as handle:
            project = handle.read()
        self.assertIn('version = "%s"' % wiz.VERSION, project)
        self.assertTrue(wiz_live.LIVE_VERSION)

    def test_missing_extras_are_reported_clearly(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            with self.assertRaises(wiz_live.MissingExtra) as caught:
                wiz_live.require("numpy", "live analysis", "live")
            self.assertIn("wizterm[live]", str(caught.exception))
        else:
            self.assertIsNotNone(wiz_live.require("numpy", "live analysis", "live"))


class NumpyAnalysisTests(unittest.TestCase):
    def setUp(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("numpy is not installed")

    def test_analysis_frames_follow_a_tone(self):
        import numpy
        rate, block = 22050, 1024
        tone = (numpy.sin(2 * numpy.pi * 60 * numpy.arange(block * 8) / rate)).tolist()
        blocks = [tone[start:start + block] for start in range(0, len(tone), block)]
        stream = wiz_live._analysis_frames(blocks, "bands", options(), [{"id": "1"}])
        frames = list(stream)
        self.assertTrue(frames)
        for _moment, payload in frames:
            self.assertEqual(len(payload), 1)
            rgb, dimming = payload[0]
            self.assertGreater(rgb[0] + rgb[1] + rgb[2], 0)
            self.assertGreaterEqual(dimming, 10)


if __name__ == "__main__":
    unittest.main()
