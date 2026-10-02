#!/usr/bin/env python3
"""wiz live - optional audio-reactive shows for Philips WiZ lights.

The core ``wiz`` CLI stays dependency-free. This module is the optional half:
it captures audio, runs an FFT, and turns the result into WiZ ``setPilot``
frames over the same LAN protocol and the same ``~/.config/wiz/lights.json``
registry the core CLI already owns.

Commands (each one is also reachable as ``wiz <command>``):

  live [target]              audio-reactive visualizer
  detect [target]            listen, identify the song, theme the lights
  caramelldansen [target]    the meme: BPM-locked two-colour bounce
  shows                      list visualizer modes

Audio sources:

  --source mic               default; the machine microphone
  --source system            system/loopback input (BlackHole, Loopback, ...)
  --source file PATH         a decoded audio file, for repeatable demos

Required extras:

  numpy, sounddevice         live capture and FFT
  shazamio                   song identification in ``wiz detect``

Install them with the package extra:

  pip install "wizterm[live,recognize]"

Run without hardware using ``--dry-run``: frames are printed instead of sent.
"""
from __future__ import annotations

import argparse
import colorsys
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave

LIVE_VERSION = "0.1.0"

# The core CLI injects itself here so this module reuses its UDP transport,
# registry and target resolution instead of re-implementing them.
CORE = None

# ---------- constants ----------

BANDS = (
    ("bass", 20.0, 250.0),
    ("mid", 250.0, 4000.0),
    ("treble", 4000.0, 16000.0),
)

SHOW_MODES = {
    "bands": "bass -> red, mid -> green, treble -> blue",
    "energy": "warm colour when loud, cool colour when quiet",
    "rainbow": "hue follows the dominant band",
    "pulse": "fixed warm colour, brightness follows the music",
    "strobe": "white flash on every detected beat, dim in between",
    "spectrum": "colour hint from the spectrum plus an aggressive brightness pulse",
    "multi": "one frequency band per light, for two or more bulbs",
    "caramelldansen": "the meme: two colours swapping on every half beat",
}

DEFAULT_MODE = "bands"
DEFAULT_RATE = 22050
DEFAULT_BLOCK = 1024
DEFAULT_FPS = 12.0

CARAMELLDANSEN_BPM = 165.0
CARAMELLDANSEN_COLORS = ("ff2e88", "00e5ff")
CARAMELLDANSEN_FPS = 15.0

MICS = ("mic", "system", "file")

# Known meme titles, matched case-insensitively against the ``wiz detect``
# result, mapped to the show that should answer them.
KNOWN_MEMES = {
    "caramelldansen": "caramelldansen",
}

LOOPBACK_HINTS = ("blackhole", "loopback", "soundflower", "aggregate", "vb-cable")


class MissingExtra(RuntimeError):
    """Raised when an optional dependency is not installed."""


def core():
    """Return the core ``wiz`` module, importing it when possible."""
    global CORE
    if CORE is None:
        try:
            import wiz as CORE_MODULE  # noqa: N813 - deliberate late import
        except ImportError:
            CORE = _load_sibling_core()
        else:
            CORE = CORE_MODULE
    return CORE


def _load_sibling_core():
    """Load the installed ``wiz`` script, which may have no ``.py`` suffix."""
    import importlib.machinery
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("wiz.py", "wiz"):
        path = os.path.join(here, name)
        if not os.path.isfile(path):
            continue
        # An extensionless script has no inferred loader, so name one.
        loader = importlib.machinery.SourceFileLoader("wiz", path)
        spec = importlib.util.spec_from_file_location("wiz", path, loader=loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    raise MissingExtra(
        "the core wiz module was not found next to wiz_live.py in %s" % here
    )


def require(module_name, feature, extra):
    """Import an optional dependency or explain exactly how to install it."""
    try:
        return __import__(module_name)
    except ImportError as exc:
        raise MissingExtra(
            "%s needs the optional '%s' extra (%s missing).\n"
            "  install: pip install \"wizterm[%s]\"\n"
            "  or run:  uv run --with %s wiz_live.py ..."
            % (feature, extra, module_name, extra, module_name)
        ) from exc


# ---------- pure colour helpers ----------

def clamp(value, low, high):
    return low if value < low else high if value > high else value


def lerp(start, end, ratio):
    return start + (end - start) * ratio


def parse_color(text):
    """Parse RRGGBB / #RRGGBB into an (r, g, b) tuple of ints."""
    if not isinstance(text, str):
        raise ValueError("colour must be a string like ff2e88")
    value = text.strip().lstrip("#")
    if len(value) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in value):
        raise ValueError("colour must be six hex digits, e.g. ff2e88")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def rgb_to_hex(rgb):
    return "%02x%02x%02x" % tuple(int(clamp(channel, 0, 255)) for channel in rgb)


def hsv_to_rgb(hue, saturation=1.0, value=1.0):
    """HSV with hue in 0..1, returns 0..255 ints."""
    red, green, blue = colorsys.hsv_to_rgb(hue % 1.0, clamp(saturation, 0.0, 1.0),
                                           clamp(value, 0.0, 1.0))
    return (int(round(red * 255)), int(round(green * 255)), int(round(blue * 255)))


def scale_rgb(rgb, factor):
    return tuple(int(round(clamp(channel * factor, 0, 255))) for channel in rgb)


def mix_rgb(first, second, ratio):
    ratio = clamp(ratio, 0.0, 1.0)
    return tuple(int(round(lerp(a, b, ratio))) for a, b in zip(first, second))


# ---------- analysis ----------

def band_energies(freqs, magnitudes, bands=BANDS):
    """Linear energy per band from parallel frequency/magnitude sequences."""
    energies = []
    for _name, low, high in bands:
        total = 0.0
        for frequency, magnitude in zip(freqs, magnitudes):
            if low <= frequency < high:
                total += magnitude * magnitude
        energies.append(total)
    return energies


def spectral_flux(previous, current):
    """Total positive change between two magnitude spectra."""
    if previous is None:
        return 0.0
    return sum(max(0.0, now - before) for before, now in zip(previous, current))


def normalize_levels(energies, scale):
    """Map raw band energies to 0..1 using a per-band reference scale."""
    return [clamp(math.sqrt(energy) / scale, 0.0, 1.0) if scale > 0 else 0.0
            for energy in energies]


class AutoGain:
    """Rolling reference level per band so quiet and loud tracks both fill the range."""

    def __init__(self, bands=3, floor=1e-6, decay=0.985, attack=0.35):
        self.scale = [floor] * bands
        self.floor = floor
        self.decay = decay
        self.attack = attack

    def update(self, energies):
        for index, energy in enumerate(energies):
            peak = math.sqrt(max(energy, 0.0))
            if index >= len(self.scale):
                self.scale.append(self.floor)
            if peak > self.scale[index]:
                self.scale[index] = lerp(self.scale[index], peak, self.attack)
            else:
                self.scale[index] = max(self.floor, self.scale[index] * self.decay)
        return [
            clamp(math.sqrt(max(energy, 0.0)) / self.scale[index], 0.0, 1.0)
            if self.scale[index] > 0 else 0.0
            for index, energy in enumerate(energies)
        ]


class BeatDetector:
    """Two-threshold beat detector over spectral flux (rolling mean + k * std)."""

    def __init__(self, window=15, sensitivity=1.5):
        self.history = []
        self.window = max(3, int(window))
        self.sensitivity = sensitivity

    def feed(self, flux):
        """Return True when this flux value counts as a beat."""
        beat = False
        if len(self.history) >= 3:
            mean = sum(self.history) / len(self.history)
            variance = sum((value - mean) ** 2 for value in self.history) / len(self.history)
            threshold = mean + self.sensitivity * math.sqrt(variance)
            beat = flux > threshold and flux > 1e-9
        self.history.append(flux)
        if len(self.history) > self.window:
            self.history.pop(0)
        return beat


# ---------- effects (pure, time-driven, unit-testable) ----------

CARAMELLDANSEN_FLOOR = 25


def caramelldansen_frame(moment, bpm=CARAMELLDANSEN_BPM, colors=CARAMELLDANSEN_COLORS,
                         swaps_per_beat=2, dim_floor=CARAMELLDANSEN_FLOOR, dim_peak=100):
    """Deterministic two-colour bounce state at ``moment`` seconds.

    The colour swaps every ``1 / swaps_per_beat`` of a beat and the brightness
    decays from the swap until the next one, which is the light equivalent of
    the meme's hop. Returns ``(rgb, dimming)``.
    """
    palette = [parse_color(value) if isinstance(value, str) else tuple(value)
               for value in colors]
    if not palette:
        raise ValueError("caramelldansen needs at least one colour")
    beat = max(moment, 0.0) * bpm / 60.0
    step = beat * max(1, int(swaps_per_beat))
    index = int(math.floor(step)) % len(palette)
    progress = step - math.floor(step)
    # Brightness punches on every swap and falls away until the next one.
    energy = (1.0 - progress) ** 2
    dimming = int(round(clamp(dim_floor + (dim_peak - dim_floor) * energy, 10, 100)))
    return palette[index], dimming


def mode_frame(mode, levels, level, beat, phase, index=0):
    """Colour for one visualizer frame. ``levels`` are 0..1 band levels."""
    bass, mid, treble = (list(levels) + [0.0, 0.0, 0.0])[:3]
    if mode == "bands":
        return (int(round(bass * 255)), int(round(mid * 255)), int(round(treble * 255)))
    if mode == "energy":
        warm, cool = (255, 120, 0), (0, 90, 255)
        return mix_rgb(cool, warm, level)
    if mode == "rainbow":
        dominant = max(range(3), key=lambda position: (bass, mid, treble)[position])
        return hsv_to_rgb((phase + dominant / 3.0) % 1.0, 1.0, 0.35 + 0.65 * level)
    if mode == "pulse":
        return scale_rgb((255, 170, 60), 0.25 + 0.75 * level)
    if mode == "strobe":
        return (255, 255, 255) if beat else scale_rgb((255, 90, 20), 0.15 + 0.35 * level)
    if mode == "spectrum":
        hue = (0.02 + 0.62 * bass - 0.30 * treble) % 1.0
        return hsv_to_rgb(hue, 0.9, 0.2 + 0.8 * (bass ** 0.6 if beat else level))
    if mode == "multi":
        return scale_rgb((255, 255, 255), (bass, mid, treble)[index % 3])
    raise ValueError("unknown mode '%s'" % mode)


def mode_dimming(mode, level, beat):
    if mode in ("pulse",):
        return int(round(clamp(35 + 65 * level, 10, 100)))
    if mode == "spectrum":
        return int(round(clamp(35 + 65 * (level ** 0.5), 10, 100)))
    if mode in ("bands", "energy", "rainbow", "multi"):
        return int(round(clamp(45 + 55 * level, 10, 100)))
    if mode == "strobe":
        return 100 if beat else 30
    return 80


# ---------- audio capture ----------

def decode_file(path, rate=DEFAULT_RATE):
    """Decode any ffmpeg-readable audio file into one mono float list."""
    if not os.path.isfile(path):
        raise OSError("audio file not found: %s" % path)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MissingExtra("ffmpeg is required to decode audio files")
    process = subprocess.run(
        [ffmpeg, "-v", "error", "-i", path, "-f", "f32le", "-acodec", "pcm_f32le",
         "-ac", "1", "-ar", str(rate), "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if process.returncode != 0:
        raise OSError("ffmpeg failed: %s" % process.stderr.decode("utf-8", "replace").strip())
    import array
    samples = array.array("f")
    samples.frombytes(process.stdout)
    return list(samples)


def list_input_devices():
    sounddevice = require("sounddevice", "live capture", "live")
    return sounddevice.query_devices()


def pick_input_device(source):
    """Resolve ``system`` to a loopback-looking input device index."""
    sounddevice = require("sounddevice", "live capture", "live")
    devices = sounddevice.query_devices()
    if source == "mic":
        default = sounddevice.default.device[0]
        return default if default is not None and default >= 0 else None
    for index, device in enumerate(devices):
        if device.get("max_input_channels", 0) < 1:
            continue
        name = str(device.get("name", "")).lower()
        if any(hint in name for hint in LOOPBACK_HINTS):
            return index
    raise MissingExtra(
        "no loopback input device found; install BlackHole and route system audio to it, "
        "or use --source mic / --source file"
    )


def mic_blocks(source, rate, block, device=None):
    """Yield float blocks from a live input device until the caller stops."""
    sounddevice = require("sounddevice", "live capture", "live")
    import array
    index = pick_input_device(source) if device is None else device
    with sounddevice.RawInputStream(samplerate=rate, blocksize=block, channels=1,
                                    dtype="float32", device=index) as stream:
        while True:
            raw, _overflowed = stream.read(block)
            samples = array.array("f")
            samples.frombytes(raw)
            yield list(samples)


def file_blocks(path, rate, block):
    samples = decode_file(path, rate)
    for start in range(0, len(samples), block):
        yield samples[start:start + block]


# ---------- transport ----------

def resolve_lights(state, target):
    """Reuse the core CLI's target resolution, including ``@name`` forms."""
    wiz = core()
    return wiz.resolve_targets(state, normalize_target(target))


def normalize_target(value):
    """Accept both ``lamp`` and ``@lamp``; refuse an empty ``@``."""
    if value is None:
        return None
    text = str(value).strip()
    if text.startswith("@"):
        text = text[1:].strip()
        if not text:
            raise ValueError("target after '@' cannot be empty")
    return text or None


def meme_show_for(title):
    """Return the show keyed to a recognised track title, or None."""
    if not title:
        return None
    lowered = title.lower()
    for keyword, show in KNOWN_MEMES.items():
        if keyword in lowered:
            return show
    return None


def hint_for_kind(record):
    if record.get("kind") == "tunable-white":
        return " (tunable white: RGB is ignored, only brightness will move)"
    if record.get("kind") == "dimmable":
        return " (dimmable: colour is ignored, only brightness will move)"
    return ""


def send_frame(record, rgb, dimming, tolerate_failure=True):
    wiz = core()
    params = {"state": True, "r": rgb[0], "g": rgb[1], "b": rgb[2],
              "dimming": int(clamp(dimming, 10, 100))}
    try:
        wiz.set_pilot(record["ip"], params)
        return True
    except (OSError, ValueError) as exc:
        if not tolerate_failure:
            raise
        print("  %s  unreachable (%s)" % (wiz._record_prefix(record), exc))
        return False


def restore(records):
    """Return each light to the state it had before a show."""
    wiz = core()
    for record, pilot in records:
        params = {"state": bool(pilot.get("state", True))}
        for key in ("dimming", "temp", "r", "g", "b", "sceneId"):
            if key in pilot:
                params[key] = pilot[key]
        try:
            wiz.set_pilot(record["ip"], params)
            print("  restored %s" % wiz._record_prefix(record))
        except (OSError, ValueError):
            print("  could not restore %s" % wiz._record_prefix(record))


def snapshot(records):
    wiz = core()
    saved = []
    for record in records:
        try:
            saved.append((record, wiz.get_pilot(record["ip"])))
        except (OSError, ValueError):
            pass
    return saved


# ---------- commands ----------

def cmd_shows(_args=None):
    print("wiz live modes:")
    for name in sorted(SHOW_MODES):
        print("  %-16s %s" % (name, SHOW_MODES[name]))
    print("")
    print("sources: mic (default), system (loopback), file")
    print("examples:")
    print("  wiz live --source mic")
    print("  wiz live @lamp --mode spectrum --sensitivity 2.0")
    print("  wiz live @lamp --source file song.mp3")
    print("  wiz caramelldansen @lamp")
    return 0


def _load_targets(target):
    wiz = core()
    state = wiz.load_state()
    try:
        records = resolve_lights(state, target)
    except ValueError as exc:
        print("wiz live: %s" % exc)
        return None
    if not records:
        print("no tracked lights matched; run 'wiz' or 'wiz find' first")
        return None
    return records


def _frame_printer(records):
    def show(moment, frames):
        stamp = "%7.3f" % moment
        for record, (rgb, dimming) in zip(records, frames):
            print("%s  %s  #%s  dim=%3d" % (stamp, record.get("name", "-"),
                                            rgb_to_hex(rgb), dimming))
    return show


def _render(records, frames_iter, options, duration=None):
    """Drive ``records`` from an iterator of (moment, rgb, dimming) batches."""
    started = time.monotonic()
    interval = 1.0 / max(options.fps, 1.0)
    last_sent = {}
    chapter = 0
    for moment, frames in frames_iter:
        if duration is not None and moment > duration:
            break
        if options.dry_run:
            _frame_printer(records)(moment, frames)
        else:
            for record, (rgb, dimming) in zip(records, frames):
                previous = last_sent.get(record.get("id"))
                if previous == (rgb, dimming):
                    continue
                if send_frame(record, rgb, dimming):
                    last_sent[record.get("id")] = (rgb, dimming)
        chapter += 1
        if not options.dry_run:
            owed = started + chapter * interval - time.monotonic()
            if owed > 0:
                time.sleep(owed)
    return chapter


def _analysis_frames(blocks, mode, options, records):
    """Generator of (moment, [(rgb, dimming), ...]) from raw audio blocks."""
    numpy = require("numpy", "live analysis", "live")
    rate = options.rate
    frequencies = numpy.fft.rfftfreq(options.block, d=1.0 / rate)
    gain = AutoGain()
    beats = BeatDetector(sensitivity=options.sensitivity)
    previous = None
    phase = 0.0

    for position, block in enumerate(blocks):
        if len(block) < options.block // 4:
            return
        window = numpy.hanning(len(block))
        spectrum = numpy.abs(numpy.fft.rfft(numpy.asarray(block, dtype=float) * window))
        cut = len(frequencies)
        spectrum = spectrum[:cut]
        flux = spectral_flux(previous, spectrum)
        previous = spectrum
        levels = gain.update(band_energies(frequencies, spectrum))
        level = clamp(sum(levels) / 3.0 * options.sensitivity, 0.0, 1.0)
        beat = beats.feed(flux * options.sensitivity)
        phase = (phase + 0.02 * level) % 1.0
        moment = position * options.block / rate
        frames = []
        for index, _record in enumerate(records):
            rgb = mode_frame(mode, levels, level, beat, phase, index)
            dimming = mode_dimming(mode, level, beat)
            if options.brightness_boost != 1.0:
                rgb = scale_rgb(rgb, options.brightness_boost)
            frames.append((rgb, dimming))
        yield moment, frames


def _caramelldansen_frames(options, records, palette):
    interval = 1.0 / max(options.fps, 1.0)
    moment = 0.0
    while True:
        rgb, dimming = caramelldansen_frame(
            moment, bpm=options.bpm, colors=palette,
            swaps_per_beat=options.rate_multiplier)
        yield moment, [(rgb, dimming) for _record in records]
        moment += interval


def cmd_live(argv):
    options = _live_parser().parse_args(argv)
    if options.list_devices:
        for index, device in enumerate(list_input_devices()):
            print("[%2d] in=%-2s %s" % (index, device.get("max_input_channels"), device["name"]))
        return 0
    # `--file` is the interesting part of `--source file`, so let it imply the
    # source, and accept the path in the target slot for the shorthand form
    # `wiz live --source file song.mp3`.
    if options.file and options.source != "file":
        options.source = "file"
    if options.source == "file" and not options.file and options.target \
            and os.path.isfile(options.target):
        options.file = options.target
        options.target = None
    mode = options.mode
    if mode == "caramelldansen":
        forwarded = []
        if options.target:
            forwarded.append(str(options.target))
        if options.duration:
            forwarded += ["--duration", str(options.duration)]
        if options.dry_run:
            forwarded.append("--dry-run")
        return cmd_caramelldansen(forwarded)
    if mode not in SHOW_MODES:
        print("wiz live: unknown mode '%s' (try 'wiz shows')" % mode)
        return 1
    records = _load_targets(options.target)
    if records is None:
        return 1
    for record in records:
        note = hint_for_kind(record)
        if note:
            print("  %s%s" % (record.get("name", "-"), note))
    if options.dry_run:
        print("dry run: no UDP is sent")
    if options.source == "file":
        if not options.file:
            print("wiz live: --source file needs a path, e.g. "
                  "wiz live @lamp --file song.mp3")
            return 1
        blocks = file_blocks(options.file, options.rate, options.block)
    else:
        blocks = mic_blocks(options.source, options.rate, options.block, options.device)

    saved = [] if options.dry_run else snapshot(records)
    print("mode %s on %d light(s); Ctrl-C to stop" % (mode, len(records)))
    try:
        frames = _analysis_frames(blocks, mode, options, records)
        count = _render(records, frames, options, duration=options.duration or None)
        print("stopped after %d frame(s)" % count)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        if saved:
            restore(saved)
    return 0


def cmd_caramelldansen(argv):
    parser = _caramelldansen_parser()
    options = parser.parse_args(argv)
    records = _load_targets(options.target)
    if records is None:
        return 1
    try:
        palette = [parse_color(value) for value in options.colors.split(",") if value]
    except ValueError as exc:
        print("wiz caramelldansen: %s" % exc)
        return 1
    if options.dry_run:
        print("dry run: no UDP is sent")
    saved = [] if options.dry_run else snapshot(records)
    print("caramelldansen %.0f BPM, colours %s, %d light(s); Ctrl-C to stop"
          % (options.bpm, ", ".join(options.colors.split(",")), len(records)))
    try:
        frames = _caramelldansen_frames(options, records, palette)
        count = _render(records, frames, options, duration=options.duration or None)
        print("stopped after %d frame(s)" % count)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        if saved:
            restore(saved)
    return 0


def record_sample(options):
    """Record ``options.seconds`` of input into a temporary mono WAV file."""
    sounddevice = require("sounddevice", "live capture", "live")
    import array
    index = pick_input_device(options.source)
    print("listening for %d s ..." % options.seconds)
    frames = []
    with sounddevice.RawInputStream(samplerate=options.rate, channels=1,
                                    dtype="int16", device=index) as stream:
        for _ in range(int(options.seconds * options.rate / options.block)):
            raw, _overflowed = stream.read(options.block)
            samples = array.array("h")
            samples.frombytes(raw)
            frames.append(samples)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        sample_path = handle.name
    with wave.open(sample_path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(options.rate)
        for samples in frames:
            wav.writeframes(samples.tobytes())
    return sample_path


def cmd_detect(argv):
    options = _detect_parser().parse_args(argv)
    if options.file and not os.path.isfile(options.file):
        print("wiz detect: audio file not found: %s" % options.file)
        return 1
    shazamio = require("shazamio", "song identification", "recognize")
    import asyncio

    records = _load_targets(options.target)
    if records is None:
        return 1

    if options.file:
        sample_path = options.file
        print("identifying %s ..." % os.path.basename(sample_path))
        result = asyncio.run(shazamio.Shazam().recognize(sample_path))
    else:
        sample_path = record_sample(options)
        try:
            print("identifying ...")
            result = asyncio.run(shazamio.Shazam().recognize(sample_path))
        finally:
            try:
                os.unlink(sample_path)
            except OSError:
                pass

    track = (result or {}).get("track") or {}
    title = track.get("title")
    if not title:
        print("no match; the recognizer heard %s" % ((result or {}).get("matches") or "nothing"))
        return 1
    artist = track.get("subtitle", "")
    print("match: %s%s" % (title, (" - " + artist) if artist else ""))
    show = meme_show_for(title)
    if show:
        print("known meme detected, starting '%s'" % show)
        if options.apply:
            return cmd_caramelldansen([str(options.target)] if options.target else [])
        return 0
    if options.apply:
        print("no meme palette for this track; starting the default visualizer")
        return cmd_live([str(options.target)] if options.target else [])
    return 0


# ---------- argument parsing ----------

def _add_target(parser):
    parser.add_argument("target", nargs="?", default=None,
                        help="light ID, name or @name; default is every tracked light")


def _add_common(parser):
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS,
                        help="frames per second sent to the bulbs (default %(default)s)")
    parser.add_argument("--brightness-boost", type=float, default=1.0,
                        help="multiply RGB values, e.g. 3.0 for maximum punch")
    parser.add_argument("--dry-run", action="store_true",
                        help="print frames instead of sending them over UDP")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="stop after this many seconds (0 = run until Ctrl-C)")


def _live_parser():
    parser = argparse.ArgumentParser(prog="wiz live", description=SHOW_MODES["bands"])
    _add_target(parser)
    _add_common(parser)
    parser.add_argument("--mode", "-m", default=DEFAULT_MODE, choices=sorted(SHOW_MODES))
    parser.add_argument("--source", "-s", default="mic", choices=MICS)
    parser.add_argument("--file", "-f", default=None, metavar="PATH",
                        help="decode an audio file; implies --source file")
    parser.add_argument("--device", "-d", type=int, default=None,
                        help="input device index (see --list-devices)")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--rate", type=int, default=DEFAULT_RATE)
    parser.add_argument("--block", type=int, default=DEFAULT_BLOCK)
    parser.add_argument("--sensitivity", type=float, default=1.5)
    return parser


def _caramelldansen_parser():
    parser = argparse.ArgumentParser(
        prog="wiz caramelldansen",
        description="two colours swapping on every half beat, %s BPM by default"
                    % int(CARAMELLDANSEN_BPM))
    _add_target(parser)
    _add_common(parser)
    parser.set_defaults(fps=CARAMELLDANSEN_FPS)
    parser.add_argument("--bpm", type=float, default=CARAMELLDANSEN_BPM)
    parser.add_argument("--colors", default=",".join(CARAMELLDANSEN_COLORS),
                        help="comma-separated hex colours, default %(default)s")
    parser.add_argument("--rate-multiplier", dest="rate_multiplier", type=int, default=2,
                        help="colour swaps per beat (default %(default)s)")
    return parser


def _detect_parser():
    parser = argparse.ArgumentParser(prog="wiz detect",
                                     description="identify the playing song and theme the lights")
    _add_target(parser)
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--source", "-s", default="mic", choices=MICS[:2])
    parser.add_argument("--file", "-f", default=None,
                        help="identify an audio file instead of recording from a device")
    parser.add_argument("--rate", type=int, default=DEFAULT_RATE)
    parser.add_argument("--block", type=int, default=DEFAULT_BLOCK)
    parser.add_argument("--apply", action="store_true",
                        help="start the matching show after a successful match")
    return parser


COMMANDS = {
    "shows": cmd_shows,
    "live": cmd_live,
    "visualize": cmd_live,
    "visualise": cmd_live,
    "caramelldansen": cmd_caramelldansen,
    "detect": cmd_detect,
    "listen": cmd_detect,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if argv[0] in ("-V", "--version", "version"):
        print("wiz live %s" % LIVE_VERSION)
        return 0
    command = argv[0]
    handler = COMMANDS.get(command)
    if handler is None:
        print("wiz live: unknown command '%s'" % command)
        print("commands: %s" % ", ".join(sorted(COMMANDS)))
        return 2
    try:
        return handler(argv[1:])
    except MissingExtra as exc:
        print("wiz live: %s" % exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
