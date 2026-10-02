---
name: wiz-lan-control
description: "Control Philips WiZ smart lights on the local network via the wiz CLI: status, on/off, brightness, presets, RGB, ambience, names, discovery, plus optional audio-reactive shows, song detection and meme effects. Use for any WiZ light request."
version: 2.0.1
category: smart-home
---

# WiZ Light Control

Agent skill for `wiz` 0.11.0, a single-file Python CLI speaking the WiZ Local
API: JSON over UDP port 38899, LAN-only, no cloud, no dependencies. Audio
shows live in the optional `wiz_live.py` module and are loaded only on demand.

## Prerequisite check

```bash
command -v wiz && wiz --version && wiz || echo "wiz not installed"
```

If missing, install per https://github.com/himanusia/wizterm. Preferred:
`pipx install git+https://github.com/himanusia/wizterm.git`; fallback: download
raw `wiz.py` to `~/.local/bin/wiz`. If the machine is not on the same network
as the bulbs, discovery reports no responses; say so instead of retrying.

## Discovery and registry

```bash
wiz                          # discover, then show tracked light status
wiz list                     # show cached registry without discovery
wiz find                     # refresh discovery
wiz find --include-forgotten # re-adopt devices explicitly forgotten
wiz update [options]           # update CLI + selected harness skill copies
```

A discovered device receives a local numeric ID. Its WiZ MAC address is stored
as the stable UID when firmware reports it, including a `getSystemConfig` fallback
when the initial registration response omits the MAC. A DHCP IP change therefore
does not create a duplicate. The registry and local names live in
`~/.config/wiz/lights.json`. The original IP/name-only cache is migrated when
it is next written.

A discovered MAC is authoritative. If a different MAC appears at an old IP, the
old record is retained as offline and the new device gets a new ID. Never let a
reused DHCP address inherit another light's name. Offline records must not be
sent UDP commands.

A bare `wiz` is read-only apart from refreshing this local registry. Control
commands still target only tracked lights unless given a direct IP.

## Updating

```bash
wiz update --check                    # read-only check
wiz update                            # stable main ref; Hermes skill by default
wiz update --force                    # reapply same/newer versions
wiz update --ref <branch-or-tag> # explicit source ref
wiz update --harness codex            # also sync Codex global skill
wiz update --harness claude           # also sync Claude Code skill
wiz update --harness opencode         # also sync OpenCode skill
wiz update --harness all              # sync all supported global targets
```

The updater fetches `wiz.py`, `pyproject.toml`, and this portable skill over HTTPS,
checks matching version metadata, compiles the candidate without executing it,
refuses downgrades, and atomically updates the installed `wiz` script plus the
selected skill targets. The default target is the active Hermes skill. Other
global targets are Codex `~/.agents/skills/wiz-lan-control/SKILL.md`, Claude Code
`~/.claude/skills/wiz-lan-control/SKILL.md`, and OpenCode
`~/.config/opencode/skills/wiz-lan-control/SKILL.md`. It does not update other
Hermes profiles or project-local skill copies. Reload the relevant harness
session after a skill update. Use `--check` when no files should change.

## Commands

```bash
wiz on | off                       # all tracked lights
wiz <10-100>                       # brightness % (implies on)
wiz night                         # 10% @ 2700K
wiz warm                          # 2700K
wiz white                         # 4000K
wiz cool                          # 6500K
wiz temp <2700-6500>               # color temperature in Kelvin
wiz preset                         # list default + color presets
wiz preset <name> [@target]        # apply a named preset
wiz color <name> [@target]         # named color preset alias
wiz rgb RRGGBB [@target]           # accepts ff8800 or #ff8800
wiz ambience                       # list ambience/scene IDs and names
wiz ambience <id|name> [@target]   # apply known ambience
wiz scene <id|name> [@target]      # ambience alias
wiz rename <name> [@target]        # assign a local friendly name
wiz forget [target]                # remove from this CLI's registry
wiz add <ip>                       # manually register a bulb by IP
```

## RGB and presets

Use six hexadecimal digits, with or without `#`. Quote a value containing `#`
when running it in a shell:

```bash
wiz rgb ff8800 @desk
wiz rgb '#ff8800' @desk
wiz preset                         # print all available preset names
wiz preset orange @desk
wiz color blue @desk
```

Default lighting presets are `night`, `warm`, `white`, and `cool`. Built-in
color presets include `red`, `orange`, `yellow`, `green`, `cyan`, `blue`,
`purple`, `pink`, and `magenta`. RGB/color commands may be ignored by
white-only bulbs.

## Ambience / scene catalog

WiZ calls these light modes or effects in different app versions; the local
protocol sends them as `sceneId`. Do not invent a mapping. Run:

```bash
wiz ambience
wiz ambience help
wiz ambience 1 @desk       # Ocean
wiz scene 1000 @desk       # Rhythm
```

The catalog includes standard modes, known custom-mode IDs, and `Rhythm`.
Firmware and bulb class determine what actually works. Newer firmware may
expose additional numeric IDs, which the CLI continues to accept.

## Targeting and lifecycle

Targets can be a numeric ID, a friendly name, or an IP. Prefixing with `@` is
recommended for unambiguous scripts. Interactive commands also accept the
natural target-first form `wiz <target> <command> [args]`. Names are
case-insensitive and support a prefix match. An explicit empty target such as
`@` is invalid and must fail.

```bash
wiz rename desk @1
wiz on @desk
wiz 40 @desk
wiz warm 192.0.2.50
wiz off 2

wiz desk ambience romance
wiz desk on
wiz 2 cool
wiz forget @desk
wiz forget @1
wiz forget                    # forget every tracked light
wiz find --include-forgotten  # re-adopt forgotten devices
```

`forget` only removes a device from this CLI and adds it to the ignored list. It
does not reset the physical bulb or remove it from the official WiZ app. A
re-adopted device receives a new local numeric ID; old IDs are never reused.

## Live audio shows (optional)

Audio-reactive shows are optional: `wiz.py` stays dependency-free and imports
`wiz_live.py` only for these words as the first argument:

```bash
wiz listen on [@target]            # start: tap system audio, lights follow
wiz listen                         # status: running? what song? which lights?
wiz listen off                     # stop and restore the previous light state
```

Configuration is on/off only. The daemon taps system audio (Spotify included),
drives the lights from it, identifies the track roughly every 25 s, and switches
to the matching show when it recognises a known meme. State lives in
`~/.config/wiz/listen.json`, `listen.pid`, `listen-state.json` and `listen.log`.

Manual, one-off commands:

```bash
wiz shows                          # list modes (no audio needed)
wiz live [@target]                 # microphone, default 'bands' mode
wiz live @lamp --mode spectrum     # bands|energy|rainbow|pulse|strobe|spectrum|multi
wiz live @lamp --file song.mp3          # repeatable, perfectly synced demo
wiz live @lamp --source system     # system audio
wiz detect [@target] --seconds 8   # identify the playing song
wiz detect [@target] --file song.mp3
wiz detect [@target] --apply       # identify, then start the matching show
wiz caramelldansen [@target]       # two colours swapping on every half beat
```

Capture: on macOS 14.4+ `wiz_tap.swift` is compiled once into
`~/.config/wiz/bin/wiz-tap` and taps system audio with `CATapDescription` plus
`AudioHardwareCreateProcessTap`, exactly like Atoll. No BlackHole or other
loopback driver. It needs `swiftc`, and macOS asks for "System Audio Recording"
permission for the terminal on first use. Elsewhere `--source system` falls back
to a loopback input device.

Useful flags: `--fps` (default 12; keep at or below ~15), `--sensitivity`,
`--brightness-boost`, `--duration`, `--dry-run` (prints frames, sends no UDP),
`--list-devices`.

Extras: `numpy` + `sounddevice` for capture and FFT, `shazamio` for song
identification. If the interpreter running `wiz` lacks them, put them in
`~/.config/wiz/venv` and the live commands re-exec into that venv
automatically. Install with `pipx install "wizterm[live,recognize]"`, or run
ad-hoc with `uv run --with numpy --with sounddevice wiz_live.py`. Without them
the CLI prints the exact install line instead of failing obscurely.

`wiz_live.py` must sit next to the installed `wiz` script, or `WIZ_LIVE_HOME`
must point at its directory. A curl install of `wiz.py` alone does not include
it; fetch `wiz_live.py` too.

Etiquette:

- Snapshot before a show and restore afterwards. `wiz live` and
  `wiz caramelldansen` do this themselves, including on Ctrl-C; only use
  `--dry-run` or an explicit duration for scripted/live tests.
- RGB modes do nothing on tunable-white or dimmable-only bulbs; check `wiz list`
  for the device kind first. The CLI prints a warning for those targets.
- Stay under ~15 frames per second and avoid resending identical frames. The
  renderer already skips unchanged frames.
- `wiz detect` sends a short audio fingerprint to Shazam. Say so before running
  it, and prefer `--file` when the user cares about what leaves the machine.

## Behavior rules

- A bare control command applies to all tracked lights; append a target for one.
- `wiz list` and status output show the detected device kind (`RGB`, `tunable white`,
  `dimmable`, or `unknown`) between the name and IP.
- Direct IP control works before a device is registered.
- After a set command, the CLI reads and prints the resulting state.
- Unreachable lights are reported once; the process does not retry-loop on UDP
  timeouts, and its exit code is non-zero if any target failed.
- The protocol is LAN-only by design. Do not claim remote/cloud control.
- White-spectrum models may clamp out-of-range temperatures automatically.
- Testing etiquette: note the current state first and restore it afterwards.
