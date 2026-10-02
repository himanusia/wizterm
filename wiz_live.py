#!/usr/bin/env python3
"""wiz live - optional audio-reactive shows for Philips WiZ lights.

The core ``wiz`` CLI stays dependency-free. This module is the optional half:
it captures audio, runs an FFT, and turns the result into WiZ ``setPilot``
frames over the same LAN protocol and the same ``~/.config/wiz/lights.json``
registry the core CLI already owns.

Commands (each one is also reachable as ``wiz <command>``):

  listen on|off [target]     the automatic listener: tap system audio, react,
                             name the track, switch shows. on/off is the config
  live [target]              audio-reactive visualizer
  detect [target]            listen, identify the song, theme the lights
  caramelldansen [target]    the meme: BPM-locked two-colour bounce
  shows                      list visualizer modes

Audio sources:

  --source mic               default; the machine microphone
  --source system            macOS: a Core Audio process tap, so Spotify and
                             anything else is captured directly with no
                             BlackHole or other loopback driver. Elsewhere: a
                             loopback input device.
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
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave

LIVE_VERSION = "0.2.6"

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
    "caramelldansen": "the meme: full brightness, two colours swapping once per beat",
}

DEFAULT_MODE = "bands"
DEFAULT_RATE = 22050
DEFAULT_BLOCK = 1024
DEFAULT_FPS = 12.0

CARAMELLDANSEN_BPM = 165.0
CARAMELLDANSEN_COLORS = ("ff2e88", "00e5ff")
CARAMELLDANSEN_FPS = 15.0
# The meme swaps pose once per beat. Two swaps per beat is a strobe, not a
# dance, and it is past the point where a room full of people wants to look at
# it.
CARAMELLDANSEN_SWAPS = 1

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

CARAMELLDANSEN_DIM = 100


def caramelldansen_frame(moment, bpm=CARAMELLDANSEN_BPM, colors=CARAMELLDANSEN_COLORS,
                         swaps_per_beat=CARAMELLDANSEN_SWAPS):
    """The meme state at ``moment`` seconds, as ``(rgb, dimming)``.

    Only the colour moves. The meme swaps pose, it does not fade, so the light
    stays at full brightness and flips between the palette colours once per
    beat. ``swaps_per_beat`` higher than 1 is a strobe, not a dance.
    """
    palette = [parse_color(value) if isinstance(value, str) else tuple(value)
               for value in colors]
    if not palette:
        raise ValueError("caramelldansen needs at least one colour")
    beat = max(moment, 0.0) * bpm / 60.0
    step = beat * max(1, int(swaps_per_beat))
    index = int(math.floor(step)) % len(palette)
    return palette[index], CARAMELLDANSEN_DIM


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


# ---------- system audio tap (macOS 14.4+, no loopback driver) ----------

TAP_BINARY = os.path.join(os.path.expanduser("~/.config/wiz/bin"), "wiz-tap")
TAP_SOURCE_NAME = "wiz_tap.swift"


def tap_source_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), TAP_SOURCE_NAME)


def build_tap(force=False):
    """Compile wiz_tap.swift once into ~/.config/wiz/bin and reuse it."""
    if os.path.isfile(TAP_BINARY) and not force:
        return TAP_BINARY
    swiftc = shutil.which("swiftc")
    if not swiftc:
        raise MissingExtra(
            "the system audio tap needs swiftc (macOS command line tools): "
            "run 'xcode-select --install'"
        )
    source = tap_source_path()
    if not os.path.isfile(source):
        raise MissingExtra("%s was not found next to wiz_live.py" % source)
    os.makedirs(os.path.dirname(TAP_BINARY), exist_ok=True)
    build = subprocess.run([swiftc, "-O", "-o", TAP_BINARY, source],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if build.returncode != 0:
        raise MissingExtra("building the audio tap failed:\n%s"
                           % build.stderr.decode("utf-8", "replace").strip()[-600:])
    return TAP_BINARY


def downmix(payload, channels):
    """Interleaved float32 bytes to a mono float list."""
    if channels <= 1:
        import array
        samples = array.array("f")
        samples.frombytes(payload)
        return list(samples)
    try:
        import numpy
    except ImportError:
        import array
        samples = array.array("f")
        samples.frombytes(payload)
        frames = len(samples) // channels
        mono = [0.0] * frames
        for index in range(frames):
            base = index * channels
            mono[index] = sum(samples[base:base + channels]) / channels
        return mono
    frames = numpy.frombuffer(payload, dtype="<f4")
    frames = frames[: len(frames) - (len(frames) % channels)]
    return frames.reshape(-1, channels).mean(axis=1).tolist()


class SystemTap:
    """Live system audio through a Core Audio process tap.

    This is the same mechanism Atoll uses: ``CATapDescription`` plus
    ``AudioHardwareCreateProcessTap``, wrapped in a private aggregate device.
    No BlackHole or other loopback driver is involved, and it works on macOS
    14.4 and newer.
    """

    def __init__(self, processes=(), seconds=None, block=2048):
        self.processes = [name for name in processes if name]
        self.seconds = seconds
        self.block = block
        self.rate = 0
        self.channels = 1
        self.process = None

    def _command(self):
        command = [build_tap()]
        for name in self.processes:
            command += ["--process", name]
        if self.seconds:
            command += ["--seconds", str(int(self.seconds))]
        return command

    def __enter__(self):
        self.process = subprocess.Popen(
            self._command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.rate, self.channels = self._read_format()
        return self

    def _read_format(self):
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            line = self.process.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if text.startswith("FORMAT "):
                parts = text.split()
                return int(parts[1]), int(parts[2])
            if text.startswith("error:"):
                raise MissingExtra(text)
        raise MissingExtra(
            "the audio tap did not start; grant this terminal 'System Audio "
            "Recording' permission in System Settings > Privacy & Security"
        )

    def blocks(self):
        frame_bytes = 4 * max(self.channels, 1)
        chunk = max(self.block, 256) * frame_bytes
        pending = b""
        while True:
            data = self.process.stdout.read(chunk)
            if not data:
                break
            pending += data
            usable = len(pending) - (len(pending) % frame_bytes)
            if usable <= 0:
                continue
            payload, pending = pending[:usable], pending[usable:]
            yield downmix(payload, self.channels)

    def stop(self):
        if not self.process:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        for pipe in (self.process.stdout, self.process.stderr):
            try:
                pipe.close()
            except (AttributeError, OSError):
                pass

    def __exit__(self, *_exc):
        self.stop()
        return False


def open_source(options):
    """Return (blocks, rate, cleanup) for the requested audio source."""
    if options.source == "file":
        if not options.file:
            raise MissingExtra("--source file needs a path, e.g. --file song.mp3")
        return file_blocks(options.file, options.rate, options.block), options.rate, (lambda: None)
    if options.source == "system" and sys.platform == "darwin":
        tap = SystemTap(seconds=options.duration or None, block=options.block)
        tap.__enter__()
        return tap.blocks(), tap.rate, tap.stop
    return mic_blocks(options.source, options.rate, options.block, options.device), \
        options.rate, (lambda: None)


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


# Lights already probed with a blocking setPilot, so a dead bulb is reported
# once and streaming frames can skip the ~350 ms acknowledgement wait.
PROBED = set()


def send_frame(record, rgb, dimming, tolerate_failure=True):
    wiz = core()
    ip = record.get("ip")
    if not ip:
        return False
    params = {"state": True, "r": rgb[0], "g": rgb[1], "b": rgb[2],
              "dimming": int(clamp(dimming, 10, 100))}
    # A WiZ bulb needs roughly 350 ms to acknowledge a colour change and
    # getPilot is answered in ~25 ms, so waiting for the setPilot reply would
    # throttle a show to about 3 frames per second. Probe a light once so an
    # offline bulb is still reported, then stream fire-and-forget.
    wait = ip not in PROBED
    try:
        if wait:
            wiz.set_pilot(ip, params)
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.sendto(json.dumps({"method": "setPilot", "params": params}).encode(),
                            (ip, wiz.PORT))
            finally:
                sock.close()
        PROBED.add(ip)
        return True
    except (OSError, ValueError) as exc:
        if not tolerate_failure:
            raise
        print("  %s  unreachable (%s)" % (wiz._record_prefix(record), exc))
        return False


def restore_params(pilot):
    """Coherent setPilot candidates that recreate a snapshot, safest first.

    A bulb rejects one command that mixes conflicting modes, for example
    ``sceneId`` together with ``r/g/b`` and ``temp``, so send exactly one of
    them plus the power state and brightness. Brightness and colour are stored
    even while a light is off, so restoring them keeps the room identical for
    the next time it is switched on.
    """
    if not pilot:
        return [{"state": False}]
    state = bool(pilot.get("state", True))
    base = {"state": state}
    dimming = pilot.get("dimming")
    if isinstance(dimming, int) and 10 <= dimming <= 100:
        base["dimming"] = dimming
    plain = {key: value for key, value in base.items()}
    if pilot.get("sceneId"):
        primary = dict(base)
        primary["sceneId"] = int(pilot["sceneId"])
        return [primary, plain]
    if any(key in pilot for key in ("r", "g", "b")):
        primary = dict(base)
        primary.update({key: int(pilot.get(key, 0)) for key in ("r", "g", "b")})
        return [primary, plain]
    if pilot.get("temp"):
        primary = dict(base)
        primary["temp"] = pilot["temp"]
        return [primary, plain]
    return [plain]


def restore(records):
    """Return each light to the state it had before a show."""
    wiz = core()
    for record, pilot in records:
        last_error = None
        for params in restore_params(pilot):
            try:
                wiz.set_pilot(record["ip"], params)
                print("  restored %s" % wiz._record_prefix(record))
                break
            except (OSError, ValueError, RuntimeError) as exc:
                last_error = exc
        else:
            print("  could not restore %s (%s)" % (wiz._record_prefix(record), last_error))


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
    print("wiz listen on|off      the automatic listener (system audio, no driver)")
    print("")
    print("sources: mic (default), system, file")
    print("  system   macOS: Core Audio process tap, no loopback driver needed")
    print("           elsewhere: a loopback input device (BlackHole, VB-Cable, ...)")
    print("examples:")
    print("  wiz listen on @lamp            # set it and forget it")
    print("  wiz live --source system")
    print("  wiz live @lamp --mode spectrum --sensitivity 2.0")
    print("  wiz live @lamp --file song.mp3")
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
        # Pace even a dry run, so the printed frames carry real timings.
        owed = started + chapter * interval - time.monotonic()
        if owed > 0:
            time.sleep(owed)
    return chapter


class Analyser:
    """Turns raw audio blocks into per-light frames."""

    def __init__(self, rate, block, sensitivity=1.5, brightness_boost=1.0):
        self.numpy = require("numpy", "live analysis", "live")
        self.rate = rate
        self.block = block
        self.sensitivity = sensitivity
        self.brightness_boost = brightness_boost
        self.frequencies = self.numpy.fft.rfftfreq(block, d=1.0 / rate)
        self.gain = AutoGain()
        self.beats = BeatDetector(sensitivity=sensitivity)
        self.previous = None
        self.phase = 0.0
        self.level = 0.0
        self.beat = False
        self.levels = [0.0, 0.0, 0.0]

    def feed(self, block):
        """Absorb one block of mono samples; False when it is too short to use."""
        block = list(block)
        if len(block) < max(64, self.block // 4):
            return False
        numpy = self.numpy
        window = numpy.hanning(len(block))
        spectrum = numpy.abs(numpy.fft.rfft(numpy.asarray(block, dtype=float) * window))
        spectrum = spectrum[: len(self.frequencies)]
        flux = spectral_flux(self.previous, spectrum)
        self.previous = spectrum
        self.levels = self.gain.update(band_energies(self.frequencies, spectrum))
        self.level = clamp(sum(self.levels) / 3.0 * self.sensitivity, 0.0, 1.0)
        self.beat = self.beats.feed(flux * self.sensitivity)
        self.phase = (self.phase + 0.02 * self.level) % 1.0
        return True

    def frame(self, mode, index=0):
        rgb = mode_frame(mode, self.levels, self.level, self.beat, self.phase, index)
        if self.brightness_boost != 1.0:
            rgb = scale_rgb(rgb, self.brightness_boost)
        return rgb, mode_dimming(mode, self.level, self.beat)


def _analysis_frames(blocks, mode, options, records):
    """Generator of (moment, [(rgb, dimming), ...]) from raw audio blocks."""
    analyser = Analyser(options.rate, options.block, options.sensitivity,
                        options.brightness_boost)
    for position, block in enumerate(blocks):
        if not analyser.feed(block):
            return
        moment = position * options.block / options.rate
        yield moment, [analyser.frame(mode, index) for index in range(len(records))]


def _caramelldansen_frames(options, records, palette):
    """Frames whose beat clock is the wall clock, not the frame counter.

    A slow light then drops frames instead of playing the whole thing in slow
    motion, which is what pulled the effect out of time with the music.
    """
    started = time.monotonic()
    while True:
        moment = time.monotonic() - started
        rgb, dimming = caramelldansen_frame(
            moment, bpm=options.bpm, colors=palette,
            swaps_per_beat=options.swaps_per_beat)
        yield moment, [(rgb, dimming) for _record in records]


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
    if options.source == "file" and not options.file:
        print("wiz live: --source file needs a path, e.g. wiz live @lamp --file song.mp3")
        return 1

    try:
        blocks, rate, cleanup = open_source(options)
    except MissingExtra as exc:
        print("wiz live: %s" % exc)
        return 3
    options.rate = rate

    saved = [] if options.dry_run else snapshot(records)
    print("mode %s on %d light(s) from %s audio; Ctrl-C to stop"
          % (mode, len(records), options.source))
    try:
        frames = _analysis_frames(blocks, mode, options, records)
        count = _render(records, frames, options, duration=options.duration or None)
        print("stopped after %d frame(s)" % count)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        cleanup()
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


# ---------- wiz listen: run once, then it reacts to whatever plays ----------

WIZ_HOME = os.path.expanduser("~/.config/wiz")
LISTEN_CONFIG = os.path.join(WIZ_HOME, "listen.json")
LISTEN_PID = os.path.join(WIZ_HOME, "listen.pid")
LISTEN_STATE = os.path.join(WIZ_HOME, "listen-state.json")
LISTEN_LOG = os.path.join(WIZ_HOME, "listen.log")

# Everything below is automatic: `wiz listen on` is the whole configuration.
LISTEN_MODE = "spectrum"       # colour hint plus a brightness pulse, reads best
LISTEN_FPS = 12.0
LISTEN_BLOCK = 2048            # window size: finer bass resolution than 1024
LISTEN_SPEED = 1.4             # brightness boost, tuned for a lit room
LISTEN_INTERVAL = 25.0         # seconds between song identifications
LISTEN_FIRST_INTERVAL = 10.0   # identify sooner on the first track
LISTEN_SAMPLE = 8.0            # seconds of audio handed to the recognizer
LISTEN_PROCESSES = ()          # empty = tap all system audio, Spotify included


def listen_defaults():
    return {"enabled": False, "target": None}


def read_listen_config():
    try:
        with open(LISTEN_CONFIG, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return listen_defaults()
    config = listen_defaults()
    if isinstance(data, dict):
        config["enabled"] = bool(data.get("enabled"))
        target = data.get("target")
        config["target"] = str(target) if target else None
    return config


def write_listen_config(config):
    os.makedirs(WIZ_HOME, exist_ok=True)
    with open(LISTEN_CONFIG, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")


def read_state():
    try:
        with open(LISTEN_STATE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(**fields):
    state = read_state()
    state.update(fields)
    try:
        os.makedirs(WIZ_HOME, exist_ok=True)
        with open(LISTEN_STATE, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
            handle.write("\n")
    except OSError:
        pass


def read_pid():
    try:
        with open(LISTEN_PID, encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def listen_log(message):
    stamp = time.strftime("%H:%M:%S")
    line = "%s %s\n" % (stamp, message)
    try:
        os.makedirs(WIZ_HOME, exist_ok=True)
        with open(LISTEN_LOG, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass
    # The daemon's own stderr already points at that log file; only echo when a
    # human is watching a terminal.
    try:
        if sys.stderr.isatty():
            sys.stderr.write(line)
            sys.stderr.flush()
    except (AttributeError, ValueError):
        pass


class SongWatcher:
    """Identifies what is playing in the background, without stalling the lights."""

    def __init__(self, rate=0, interval=LISTEN_INTERVAL, sample=LISTEN_SAMPLE):
        self.rate = rate
        self.interval = interval
        self.sample = sample
        # Size the ring from the starting rate too: it is only recomputed when
        # the rate *changes*, so seeding it wrong would keep one sample.
        self.limit = max(1, int(sample * rate)) if rate else 1
        self.buffer = []
        self.thread = None
        self.result = None
        self.armed = time.monotonic()
        self.first = True
        self.loud_until = 0.0

    def feed(self, block, rate):
        if rate and rate != self.rate:
            self.rate = rate
            self.limit = max(1, int(self.sample * rate))
        self.buffer.extend(block)
        if len(self.buffer) > self.limit:
            del self.buffer[: len(self.buffer) - self.limit]
        peak = 0.0
        for value in block:
            magnitude = value if value >= 0 else -value
            if magnitude > peak:
                peak = magnitude
        if peak > 0.01:
            self.loud_until = time.monotonic() + 4.0

    def maybe_start(self):
        """Kick off an identification when audio is playing and one is due."""
        if self.thread and self.thread.is_alive():
            return
        now = time.monotonic()
        if now > self.loud_until:
            return  # nothing is playing, nothing to identify
        # Identify early on the first track, then settle into the interval.
        delay = LISTEN_FIRST_INTERVAL if self.first else self.interval
        if now - self.armed < delay:
            return
        self.first = False
        self.armed = now
        audio = list(self.buffer)
        self.thread = threading.Thread(target=self._identify, args=(audio,), daemon=True)
        self.thread.start()

    def _identify(self, audio):
        seconds = len(audio) / self.rate if self.rate else 0.0
        peak = max((abs(value) for value in audio), default=0.0)
        listen_log("identifying %.1fs of audio (peak %.2f)" % (seconds, peak))
        try:
            title, artist = identify_samples(audio, self.rate)
        except (MissingExtra, OSError, ValueError) as exc:
            listen_log("song identification skipped: %s" % exc)
            return
        if not title:
            listen_log("no song match")
            return
        show = meme_show_for(title)
        listen_log("now playing: %s%s%s" % (title, " - " if artist else "",
                                            artist))
        write_state(song="%s%s%s" % (title, " - " if artist else "", artist),
                    show=show or LISTEN_MODE)
        if show:
            self.result = show

    def take_show(self):
        show, self.result = self.result, None
        return show


def identify_samples(samples, rate):
    """Recognize a mono sample list; returns (title, artist) or (None, None)."""
    shazamio = require("shazamio", "song identification", "recognize")
    import array
    import asyncio
    if not samples:
        return None, None
    peak = max(max(samples), -min(samples)) or 1.0
    scale = 0.95 / peak if peak > 0.95 else 1.0
    pcm = array.array("h")
    for value in samples:
        clamped = value * scale
        clamped = -1.0 if clamped < -1.0 else 1.0 if clamped > 1.0 else clamped
        pcm.append(int(clamped * 32767))
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        sample_path = handle.name
    try:
        with wave.open(sample_path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(int(rate))
            wav.writeframes(pcm.tobytes())
        result = asyncio.run(shazamio.Shazam().recognize(sample_path))
    finally:
        try:
            os.unlink(sample_path)
        except OSError:
            pass
    track = (result or {}).get("track") or {}
    title = track.get("title")
    if not title:
        return None, None
    return title, track.get("subtitle", "")


def listen_loop(records, stop):
    """The whole show: tap the system audio, drive the lights, name the song."""
    options = argparse.Namespace(
        rate=DEFAULT_RATE, block=LISTEN_BLOCK, sensitivity=1.5,
        brightness_boost=LISTEN_SPEED, fps=LISTEN_FPS, duration=None,
        dry_run=False, source="system", file=None, device=None, target=None)
    tap = SystemTap(processes=LISTEN_PROCESSES, block=options.block)
    tap.__enter__()
    options.rate = tap.rate
    listen_log("tap live: %d Hz, %d channel(s), %d light(s)"
               % (tap.rate, tap.channels, len(records)))

    analyser = Analyser(options.rate, options.block, options.sensitivity,
                        options.brightness_boost)
    watcher = SongWatcher(rate=tap.rate)
    mode = LISTEN_MODE
    interval = 1.0 / max(LISTEN_FPS, 1.0)
    last_sent = {}
    next_send = time.monotonic()
    try:
        for block in tap.blocks():
            if stop["now"]:
                break
            # Every block is analysed and buffered: the tap drops audio when the
            # reader falls behind, and a gappy buffer never matches a song. Only
            # the UDP write is rate limited, never the read.
            if not analyser.feed(block):
                continue
            watcher.feed(block, options.rate)
            watcher.maybe_start()
            switched = watcher.take_show()
            if switched and switched != mode:
                mode = switched
                listen_log("switching to the '%s' show" % mode)
            now = time.monotonic()
            if now < next_send:
                continue
            next_send = now + interval
            for index, record in enumerate(records):
                frame = analyser.frame(mode, index)
                if last_sent.get(record.get("id")) == frame:
                    continue
                if send_frame(record, frame[0], frame[1]):
                    last_sent[record.get("id")] = frame
    finally:
        tap.stop()


def cmd_listen_daemon(_argv=None):
    """Foreground worker started by `wiz listen on`."""
    config = read_listen_config()
    records = _load_targets(config.get("target"))
    if records is None:
        listen_log("no lights to drive, exiting")
        return 1
    stop = {"now": False}

    def on_signal(_signum, _frame):
        stop["now"] = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    saved = snapshot(records)
    write_state(pid=os.getpid(), started=time.time(), song=None, show=LISTEN_MODE,
                lights=[record.get("name") or record.get("id") for record in records])
    listen_log("listening on system audio; %d light(s)" % len(records))
    try:
        while not stop["now"]:
            try:
                listen_loop(records, stop)
            except MissingExtra as exc:
                listen_log("tap unavailable: %s" % exc)
                stop["now"] = True
            if not stop["now"]:
                listen_log("tap ended, reconnecting")
                time.sleep(2.0)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            restore(saved)
        except Exception as exc:  # never let cleanup hide the real exit reason
            listen_log("restore failed: %s" % exc)
        write_state(pid=None, song=None)
        listen_log("listening stopped, lights restored")
    return 0


def cmd_listen(argv):
    action = "status"
    target = None
    for argument in argv:
        if argument in ("on", "off", "status", "start", "stop"):
            action = argument
        elif argument.startswith("-"):
            print("wiz listen: unknown option '%s'" % argument)
            return 2
        else:
            target = argument
    if action in ("start",):
        action = "on"
    if action in ("stop",):
        action = "off"

    if action == "on":
        config = read_listen_config()
        try:
            chosen = normalize_target(target) if target else config.get("target")
        except ValueError as exc:
            print("wiz listen: %s" % exc)
            return 2
        config["enabled"] = True
        config["target"] = chosen
        write_listen_config(config)
        existing = read_pid()
        if pid_alive(existing):
            print("wiz listen: already on (pid %s)" % existing)
            return 0
        os.makedirs(WIZ_HOME, exist_ok=True)
        handle = open(LISTEN_LOG, "a")
        child = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "_listen-daemon"],
            stdout=handle, stderr=handle, stdin=subprocess.DEVNULL,
            start_new_session=True)
        with open(LISTEN_PID, "w", encoding="utf-8") as pid_file:
            pid_file.write(str(child.pid))
        print("wiz listen on (pid %s)" % child.pid)
        print("  source  system audio, tapped directly (no BlackHole needed)")
        print("  lights  %s" % (chosen or "every tracked light"))
        print("  mode    %s, switched automatically when a known track plays" % LISTEN_MODE)
        print("  log     %s" % LISTEN_LOG)
        return 0

    if action == "off":
        config = read_listen_config()
        config["enabled"] = False
        write_listen_config(config)
        pid = read_pid()
        if pid_alive(pid):
            os.kill(pid, signal.SIGTERM)
            for _ in range(60):
                if not pid_alive(pid):
                    break
                time.sleep(0.1)
            print("wiz listen off (stopped pid %s, lights restored)" % pid)
        else:
            print("wiz listen off (nothing was running)")
        try:
            os.unlink(LISTEN_PID)
        except OSError:
            pass
        return 0

    config = read_listen_config()
    pid = read_pid()
    running = pid_alive(pid)
    state = read_state()
    print("wiz listen: %s" % ("on" if running else "off"))
    if running:
        uptime = time.time() - float(state.get("started") or time.time())
        print("  pid     %s (up %dm %02ds)" % (pid, int(uptime // 60), int(uptime % 60)))
        print("  lights  %s" % ", ".join(state.get("lights") or []) or "-")
        print("  song    %s" % (state.get("song") or "-"))
        print("  show    %s" % (state.get("show") or LISTEN_MODE))
    elif config["enabled"]:
        print("  config says on but no process is running; run 'wiz listen on'")
        return 1
    print("  config  %s" % LISTEN_CONFIG)
    return 0


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
        description="full brightness, two colours swapping once per beat, %s BPM by default"
                    % int(CARAMELLDANSEN_BPM))
    _add_target(parser)
    _add_common(parser)
    parser.set_defaults(fps=CARAMELLDANSEN_FPS)
    parser.add_argument("--bpm", type=float, default=CARAMELLDANSEN_BPM)
    parser.add_argument("--colors", default=",".join(CARAMELLDANSEN_COLORS),
                        help="comma-separated hex colours, default %(default)s")
    parser.add_argument("--swaps-per-beat", dest="swaps_per_beat", type=int,
                        default=CARAMELLDANSEN_SWAPS,
                        help="colour swaps per beat: 1 is the meme, 2 is a strobe "
                             "(default %(default)s)")
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
    "listen": cmd_listen,
    "_listen-daemon": cmd_listen_daemon,
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
