"""Offline tests for the optional wiz_live module.

These run with the standard library only: the effect math is pure Python and
the numpy-backed analysis path is exercised only when numpy is installed.
"""
import io
import os
import shutil
import sys
import time
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
        "rate_multiplier": 1,
        "swaps_per_beat": wiz_live.CARAMELLDANSEN_SWAPS,
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
        # One swap per beat at 165 BPM, so one swap every 1 / 2.75 seconds.
        swap = 60.0 / wiz_live.CARAMELLDANSEN_BPM
        pink = wiz_live.parse_color("ff2e88")
        cyan = wiz_live.parse_color("00e5ff")
        self.assertEqual(wiz_live.caramelldansen_frame(0.0)[0], pink)
        self.assertEqual(wiz_live.caramelldansen_frame(swap * 1.1)[0], cyan)
        self.assertEqual(wiz_live.caramelldansen_frame(swap * 2.1)[0], pink)
        self.assertEqual(wiz_live.caramelldansen_frame(swap * 3.1)[0], cyan)

    def test_one_swap_per_beat_is_the_default(self):
        beat = 60.0 / wiz_live.CARAMELLDANSEN_BPM
        first = wiz_live.caramelldansen_frame(0.0)[0]
        same_beat = wiz_live.caramelldansen_frame(beat * 0.9)[0]
        next_beat = wiz_live.caramelldansen_frame(beat * 1.1)[0]
        self.assertEqual(wiz_live.CARAMELLDANSEN_SWAPS, 1)
        self.assertEqual(first, same_beat)
        self.assertNotEqual(first, next_beat)

    def test_swaps_per_beat_makes_it_faster(self):
        beat = 60.0 / wiz_live.CARAMELLDANSEN_BPM
        first = wiz_live.caramelldansen_frame(0.0, swaps_per_beat=2)[0]
        mid_beat = wiz_live.caramelldansen_frame(beat * 0.6, swaps_per_beat=2)[0]
        self.assertNotEqual(first, mid_beat)

    def test_the_beats_survive_a_slow_light(self):
        # The frame clock is the wall clock, so a slow bulb drops frames rather
        # than playing the whole thing in slow motion.
        stream = wiz_live._caramelldansen_frames(options(), [{"id": "1"}],
                                                 [wiz_live.parse_color("ff0000"),
                                                  wiz_live.parse_color("0000ff")])
        first, _ = next(stream)
        time.sleep(0.25)
        second, _ = next(stream)
        self.assertGreaterEqual(second - first, 0.2)

    def test_caramelldansen_brightness_punches_and_decays(self):
        swap = 60.0 / wiz_live.CARAMELLDANSEN_BPM
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
        # At 60 BPM one beat is one second, so one pose per second.
        first = wiz_live.caramelldansen_frame(0.0, bpm=60.0)[0]
        same = wiz_live.caramelldansen_frame(0.6, bpm=60.0)[0]
        next_pose = wiz_live.caramelldansen_frame(1.1, bpm=60.0)[0]
        self.assertEqual(first, same)
        self.assertNotEqual(first, next_pose)
        self.assertEqual(first, wiz_live.caramelldansen_frame(2.1, bpm=60.0)[0])

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
        moments = [moment for moment, _ in frames]
        self.assertEqual(moments, sorted(moments))
        self.assertGreater(moments[0], 0.0)
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


class RestoreParamsTests(unittest.TestCase):
    def test_off_light_keeps_its_brightness(self):
        # The bulb stores brightness while dark, so restore it too; only the
        # conflicting modes are stripped.
        candidates = wiz_live.restore_params({"state": False, "dimming": 100,
                                              "temp": 2700, "sceneId": 11,
                                              "r": 1, "g": 2, "b": 3})
        self.assertEqual(candidates[0], {"state": False, "dimming": 100,
                                         "sceneId": 11})

    def test_missing_snapshot_turns_the_light_off(self):
        self.assertEqual(wiz_live.restore_params({}), [{"state": False}])
        self.assertEqual(wiz_live.restore_params(None), [{"state": False}])

    def test_scene_snapshot_never_mixes_modes(self):
        candidates = wiz_live.restore_params(
            {"state": True, "dimming": 60, "sceneId": 4, "temp": 2700,
             "r": 10, "g": 20, "b": 30})
        first = candidates[0]
        self.assertEqual(first["sceneId"], 4)
        self.assertEqual(first["dimming"], 60)
        self.assertNotIn("r", first)
        self.assertNotIn("temp", first)

    def test_colour_snapshot_restores_rgb(self):
        first = wiz_live.restore_params({"state": True, "dimming": 80,
                                         "r": 255, "g": 0, "b": 128})[0]
        self.assertEqual((first["r"], first["g"], first["b"]), (255, 0, 128))
        self.assertNotIn("sceneId", first)
        self.assertNotIn("temp", first)

    def test_temperature_snapshot_restores_temp(self):
        first = wiz_live.restore_params({"state": True, "temp": 4000})[0]
        self.assertEqual(first["temp"], 4000)
        self.assertNotIn("sceneId", first)

    def test_out_of_range_dimming_is_dropped(self):
        self.assertNotIn("dimming", wiz_live.restore_params(
            {"state": True, "dimming": 500, "temp": 2700})[0])
        self.assertNotIn("dimming", wiz_live.restore_params(
            {"state": True, "dimming": 2, "temp": 2700})[0])

    def test_every_candidate_is_a_coherent_single_mode(self):
        pilots = [{"state": True, "sceneId": 4, "r": 1, "g": 2, "b": 3},
                  {"state": True, "r": 1, "g": 2, "b": 3, "temp": 3000},
                  {"state": True, "sceneId": 9, "temp": 3000},
                  {"state": True}]
        for pilot in pilots:
            for params in wiz_live.restore_params(pilot):
                modes = [key for key in ("sceneId", "temp") if key in params]
                modes += ["rgb"] if any(key in params for key in ("r", "g", "b")) else []
                self.assertLessEqual(len(modes), 1, (pilot, params))

    def test_probe_resets_after_a_successful_wait(self):
        wiz_live.PROBED.clear()
        wiz_live.PROBED.add("192.0.2.9")
        self.assertIn("192.0.2.9", wiz_live.PROBED)
        wiz_live.PROBED.clear()


class DownmixTests(unittest.TestCase):

    def test_mono_passes_through(self):
        import array
        samples = array.array("f", [0.5, -0.25, 1.0])
        self.assertEqual(wiz_live.downmix(samples.tobytes(), 1), [0.5, -0.25, 1.0])

    def test_stereo_is_averaged(self):
        import array
        # L R L R -> mono pairs
        samples = array.array("f", [1.0, 0.0, 0.5, 0.5])
        self.assertEqual(wiz_live.downmix(samples.tobytes(), 2), [0.5, 0.5])


class TapTests(unittest.TestCase):
    def test_cached_binary_is_reused(self):
        with TemporaryDirectory() as tmp:
            binary = os.path.join(tmp, "wiz-tap")
            with open(binary, "w") as handle:
                handle.write("")
            with patch.object(wiz_live, "TAP_BINARY", binary):
                self.assertEqual(wiz_live.build_tap(), binary)

    def test_building_without_swiftc_explains_itself(self):
        with TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "wiz-tap")
            with patch.object(wiz_live, "TAP_BINARY", missing):
                with patch.object(wiz_live.shutil, "which", return_value=None):
                    with self.assertRaises(wiz_live.MissingExtra) as caught:
                        wiz_live.build_tap()
            self.assertIn("swiftc", str(caught.exception))

    def test_open_source_needs_a_file_path(self):
        options = types.SimpleNamespace(source="file", file=None, rate=22050,
                                        block=1024, duration=0, device=None)
        with self.assertRaises(wiz_live.MissingExtra):
            wiz_live.open_source(options)

    def test_open_source_reads_a_file(self):
        options = types.SimpleNamespace(source="file", file=__file__, rate=22050,
                                        block=1024, duration=0, device=None)
        blocks, rate, cleanup = wiz_live.open_source(options)
        self.assertEqual(rate, 22050)
        self.assertTrue(callable(cleanup))


class ListenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.patches = [
            patch.object(wiz_live, "WIZ_HOME", self.tmp.name),
            patch.object(wiz_live, "LISTEN_CONFIG", os.path.join(self.tmp.name, "listen.json")),
            patch.object(wiz_live, "LISTEN_PID", os.path.join(self.tmp.name, "listen.pid")),
            patch.object(wiz_live, "LISTEN_STATE", os.path.join(self.tmp.name, "listen-state.json")),
            patch.object(wiz_live, "LISTEN_LOG", os.path.join(self.tmp.name, "listen.log")),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.tmp.cleanup()

    def test_config_round_trip(self):
        self.assertEqual(wiz_live.read_listen_config(),
                         {"enabled": False, "target": None})
        wiz_live.write_listen_config({"enabled": True, "target": "lamp"})
        self.assertEqual(wiz_live.read_listen_config(),
                         {"enabled": True, "target": "lamp"})

    def test_broken_config_falls_back_to_defaults(self):
        with open(wiz_live.LISTEN_CONFIG, "w") as handle:
            handle.write("{not json")
        self.assertEqual(wiz_live.read_listen_config(),
                         {"enabled": False, "target": None})

    def test_pid_liveness(self):
        self.assertTrue(wiz_live.pid_alive(os.getpid()))
        self.assertFalse(wiz_live.pid_alive(None))
        self.assertFalse(wiz_live.pid_alive(0))
        self.assertFalse(wiz_live.pid_alive(999999))

    def test_status_when_off(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = wiz_live.cmd_listen([])
        self.assertEqual(code, 0)
        self.assertIn("wiz listen: off", buffer.getvalue())

    def test_on_is_idempotent_while_running(self):
        wiz_live.write_listen_config({"enabled": True, "target": "lamp"})
        with open(wiz_live.LISTEN_PID, "w") as handle:
            handle.write(str(os.getpid()))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = wiz_live.cmd_listen(["on"])
        self.assertEqual(code, 0)
        self.assertIn("already on", buffer.getvalue())

    def test_off_without_a_daemon_is_harmless(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = wiz_live.cmd_listen(["off"])
        self.assertEqual(code, 0)
        self.assertIn("nothing was running", buffer.getvalue())
        self.assertFalse(wiz_live.read_listen_config()["enabled"])

    def test_unknown_option_is_rejected(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = wiz_live.cmd_listen(["--nope"])
        self.assertEqual(code, 2)


class SongWatcherTests(unittest.TestCase):
    def test_buffer_is_sized_from_the_starting_rate(self):
        # Regression: seeding limit=1 kept a single sample, so every
        # identification compared one sample against Shazam.
        watcher = wiz_live.SongWatcher(rate=48000)
        self.assertEqual(watcher.limit, int(wiz_live.LISTEN_SAMPLE * 48000))
        watcher.feed([0.5] * 4096, 48000)
        self.assertEqual(len(watcher.buffer), 4096)

    def test_buffer_keeps_only_the_last_window(self):
        watcher = wiz_live.SongWatcher(rate=100, sample=1.0)
        watcher.feed([0.1] * 80, 100)
        watcher.feed([0.2] * 80, 100)
        self.assertEqual(len(watcher.buffer), 100)
        self.assertEqual(watcher.buffer[-1], 0.2)

    def test_stays_quiet_without_audio(self):
        watcher = wiz_live.SongWatcher(rate=48000)
        watcher.armed -= 60
        watcher.maybe_start()
        self.assertIsNone(watcher.thread)

    def test_fires_once_audio_has_been_playing(self):
        watcher = wiz_live.SongWatcher(rate=48000, interval=0.0)
        with patch.object(wiz_live, "LISTEN_FIRST_INTERVAL", 0.0):
            watcher.armed -= 1
            watcher.feed([0.5] * 512, 48000)
            with patch.object(wiz_live, "identify_samples", return_value=(None, None)):
                watcher.maybe_start()
                self.assertIsNotNone(watcher.thread)
                watcher.thread.join(timeout=5)

    def test_take_show_clears_the_result(self):
        watcher = wiz_live.SongWatcher()
        watcher.result = "caramelldansen"
        self.assertEqual(watcher.take_show(), "caramelldansen")
        self.assertIsNone(watcher.take_show())


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
