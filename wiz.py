#!/usr/bin/env python3
"""wiz - control Philips WiZ lights over your LAN.

No cloud, no bridge, no dependencies. A bare ``wiz`` refreshes discovery and
shows the current status. Every discovered device gets a local numeric ID,
while its WiZ MAC address is retained as the stable identity when the DHCP IP
changes. Friendly names are local aliases for those IDs.

Usage:
  wiz                              discover, then show status of tracked lights
  wiz list                         show status from the local registry only
  wiz find                         re-discover all WiZ lights on the network
  wiz find --include-forgotten     re-adopt lights previously forgotten
  wiz --version                    print the CLI version
  wiz update [options]             update CLI and selected agent skill copies
  wiz on | off                     turn every tracked light on / off
  wiz <10-100>                     set brightness percent (turns lights on)
  wiz night | warm | white | cool  temperature presets (night = dim warm)
  wiz temp <2700-6500>             color temperature in Kelvin
  wiz preset                       list default lighting and color presets
  wiz preset <name> [target]       apply a named preset
  wiz color <name> [target]        apply a named color preset
  wiz rgb RRGGBB [target]          set an RGB color (also accepts #RRGGBB)
  wiz ambience                     list known ambience/scene IDs and names
  wiz ambience <id> [target]       activate an ambience by ID or known name
  wiz <target> ambience <id>       natural target-first form
  wiz scene <id> [target]          activate an ambience by ID or known name
  wiz rename <name> [target]       give the targeted light(s) a friendly name
  wiz forget [target]              remove light(s) from this CLI's registry
  wiz add <ip>                     manually add a light by IP

Optional audio-reactive shows (needs the 'live' extra, see README):
  wiz shows                        list visualizer modes
  wiz live [target]                audio-reactive visualizer (mic, loopback or file)
  wiz detect [target]              identify the playing song and theme the lights
  wiz caramelldansen [target]      the meme: two colours swapping on every half beat

RGB examples:
  wiz rgb ff8800 @desk
  wiz rgb '#ff8800' @desk

Targets can be a numeric ID, a friendly name, or an IP address. Prefixing a
target with ``@`` makes the intent explicit, for example ``wiz off @desk``.
Without a target, control commands apply to every tracked light.

Configuration lives in ~/.config/wiz/lights.json. The file is a local registry;
forgetting a light never resets the physical bulb or removes it from WiZ's app.

Protocol: WiZ Local API - JSON over UDP port 38899 (same LAN as the bulbs).
"""
import ast
import json
import os
import re
import shutil
import socket
import stat
import string
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

VERSION = "0.11.0"
STATE_VERSION = 2
PORT = 38899
CONF_DIR = os.path.expanduser("~/.config/wiz")
CACHE_FILE = os.path.join(CONF_DIR, "lights.json")
REPOSITORY_URL = "https://github.com/himanusia/wizterm"
DEFAULT_UPDATE_REF = "main"
MAX_UPDATE_BYTES = 512 * 1024
UPDATE_HARNESSES = ("hermes", "codex", "claude", "opencode")
DISCOVERY_WAIT = 2.0
CMD_TIMEOUT = 1.5
SYSTEM_CONFIG_TIMEOUT = 0.75

PRESETS = {"night": {"dimming": 10, "temp": 2700},
           "warm": {"temp": 2700},
           "white": {"temp": 4000},
           "cool": {"temp": 6500}}

COLOR_PRESETS = {
    "red": "ff0000",
    "orange": "ff8800",
    "yellow": "ffd000",
    "green": "00ff66",
    "cyan": "00ffff",
    "blue": "0066ff",
    "purple": "8833ff",
    "pink": "ff4f9a",
    "magenta": "ff00ff",
}

# Standard scene/effect IDs published by community WiZ integrations. Newer
# firmware can expose additional IDs, so numeric scene IDs remain accepted.
AMBIENCE = {
    1: "Ocean",
    2: "Romance",
    3: "Sunset",
    4: "Party",
    5: "Fireplace",
    6: "Cozy",
    7: "Forest",
    8: "Pastel colors",
    9: "Wake-up",
    10: "Bedtime",
    11: "Warm white",
    12: "Daylight",
    13: "Cool white",
    14: "Night light",
    15: "Focus",
    16: "Relax",
    17: "True colors",
    18: "TV time",
    19: "Plantgrowth",
    20: "Spring",
    21: "Summer",
    22: "Fall",
    23: "Deep dive",
    24: "Jungle",
    25: "Mojito",
    26: "Club",
    27: "Christmas",
    28: "Halloween",
    29: "Candlelight",
    30: "Golden white",
    31: "Pulse",
    32: "Steampunk",
    33: "Diwali",
    34: "White",
    35: "Alarm",
    36: "Snowy sky",
    40: "Dim-to-warm",
    1000: "Rhythm",
}
for _custom_index in range(1, 11):
    AMBIENCE[255 + _custom_index] = "Custom Mode %d" % _custom_index


# Commands that may use the natural target-first form:
#   wiz <target> <command> [command args]
TARGET_FIRST_COMMANDS = frozenset({
    "status", "rename", "forget", "on", "off", "night", "warm", "white", "cool",
    "temp", "preset", "color", "rgb", "ambience", "ambiance", "scene",
})
DEVICE_KIND_LABELS = {
    "rgb": "RGB",
    "tunable-white": "tunable white",
    "dimmable": "dimmable",
    "unknown": "unknown",
}


# Number of positional arguments before an optional trailing target.
COMMAND_ARGUMENTS = {
    "on": 0,
    "off": 0,
    "status": 0,
    "list": 0,
    "night": 0,
    "warm": 0,
    "white": 0,
    "cool": 0,
    "temp": 1,
    "preset": 1,
    "color": 1,
    "rgb": 1,
    "ambience": 1,
    "ambiance": 1,
    "scene": 1,
    "rename": 1,
    "forget": 0,
}


# ---------- registry ----------

def empty_state():
    return {"version": STATE_VERSION, "next_id": 1, "ignored": [], "lights": []}


def canonical_uid(value):
    """Return a normalized WiZ MAC UID, or None for an unknown identifier."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text.startswith("mac:"):
        text = text[4:]
    text = text.replace(":", "").replace("-", "").replace(".", "")
    if len(text) != 12 or any(char not in string.hexdigits for char in text):
        return None
    return "mac:" + text


def display_uid(uid):
    uid = canonical_uid(uid)
    if not uid:
        return "-"
    raw = uid[4:]
    return ":".join(raw[index:index + 2] for index in range(0, 12, 2))


def classify_module(module_name):
    text = str(module_name or "").strip().upper()
    if "RGB" in text:
        return "rgb"
    if "SHTWW" in text or "TUNABLE" in text:
        return "tunable-white"
    if text and ("DIM" in text or text.endswith("_D")):
        return "dimmable"
    return None


def normalize_device_kind(value):
    text = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    return text if text in DEVICE_KIND_LABELS else None


def display_device_kind(record):
    kind = normalize_device_kind(record.get("kind")) or "unknown"
    return DEVICE_KIND_LABELS[kind]


def _numeric_id(value):
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _allocate_id(state):
    used = {str(light.get("id")) for light in state.get("lights", [])}
    candidate = max(_numeric_id(state.get("next_id")) or 1, 1)
    while str(candidate) in used:
        candidate += 1
    state["next_id"] = candidate + 1
    return str(candidate)


def _new_record(state, ip, uid=None, name=None):
    return {
        "id": _allocate_id(state),
        "uid": canonical_uid(uid),
        "ip": str(ip),
        "name": name or None,
    }


def _sort_lights(lights):
    return sorted(lights, key=lambda light: (
        _numeric_id(light.get("id")) or 0,
        str(light.get("ip", "")),
    ))


def _clean_ignored(values):
    cleaned = []
    for value in values if isinstance(values, list) else []:
        value = str(value)
        if value.startswith("mac:"):
            value = canonical_uid(value)
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned


def load_state():
    """Load the v2 registry and migrate the original IP/name cache in memory."""
    try:
        with open(CACHE_FILE) as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return empty_state()

    if not isinstance(data, dict):
        return empty_state()

    state = empty_state()
    requested_next_id = _numeric_id(data.get("next_id"))
    if requested_next_id:
        state["next_id"] = requested_next_id
    state["ignored"] = _clean_ignored(data.get("ignored", []))

    raw_lights = data.get("lights", [])
    if not isinstance(raw_lights, list):
        raw_lights = []

    # v1 stored only {ip, name}; sorting makes their first IDs deterministic.
    if data.get("version") != STATE_VERSION:
        raw_lights = sorted(
            (entry for entry in raw_lights if isinstance(entry, dict)),
            key=lambda entry: str(entry.get("ip", "")),
        )

    used_ids = set()
    for entry in raw_lights:
        if not isinstance(entry, dict):
            continue
        raw_ip = entry.get("ip")
        raw_last_ip = entry.get("last_ip")
        if not raw_ip and not raw_last_ip:
            continue
        record_id = str(entry.get("id", ""))
        if not _numeric_id(record_id) or record_id in used_ids:
            record_id = _allocate_id(state)
        else:
            used_ids.add(record_id)
            state["next_id"] = max(state["next_id"], int(record_id) + 1)
        record = {
            "id": record_id,
            "uid": canonical_uid(entry.get("uid") or entry.get("mac")),
            "ip": str(raw_ip) if raw_ip else None,
            "name": entry.get("name") or None,
        }
        kind = normalize_device_kind(entry.get("kind"))
        if kind:
            record["kind"] = kind
        if raw_last_ip:
            record["last_ip"] = str(raw_last_ip)
        state["lights"].append(record)

    state["lights"] = _sort_lights(state["lights"])
    return state


def save_state(state):
    os.makedirs(CONF_DIR, exist_ok=True)
    payload = {
        "version": STATE_VERSION,
        "next_id": max(_numeric_id(state.get("next_id")) or 1, 1),
        "ignored": _clean_ignored(state.get("ignored", [])),
        "lights": [],
    }
    for light in _sort_lights(state.get("lights", [])):
        entry = {
            "id": str(light["id"]),
            "uid": canonical_uid(light.get("uid")),
            "ip": str(light["ip"]) if light.get("ip") else None,
            "name": light.get("name") or None,
        }
        kind = normalize_device_kind(light.get("kind"))
        if kind:
            entry["kind"] = kind
        if light.get("last_ip"):
            entry["last_ip"] = str(light["last_ip"])
        payload["lights"].append(entry)
    temporary = CACHE_FILE + ".tmp"
    with open(temporary, "w") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)
        handle.write("\n")
    os.replace(temporary, CACHE_FILE)


def _record_by_uid(lights, uid):
    if not uid:
        return None
    for light in lights:
        if canonical_uid(light.get("uid")) == uid:
            return light
    return None


def _record_by_ip(lights, ip):
    for light in lights:
        if light.get("ip") == ip:
            return light
    return None


def _ignored_keys(ip, uid):
    keys = ["ip:" + str(ip)] if ip else []
    if uid:
        keys.insert(0, uid)
    return keys


def _remove_ignored_keys(state, keys):
    state["ignored"] = [key for key in state.get("ignored", []) if key not in keys]


def _detach_record(record):
    """Keep a conflicting device visible without routing traffic to its old IP."""
    if record.get("ip"):
        record["last_ip"] = record["ip"]
    record["ip"] = None


def merge_discovered(state, discovered, include_ignored=False):
    """Merge discovery results and return the tracked records that were seen."""
    tracked = []
    for item in discovered:
        if isinstance(item, str):
            item = {"ip": item}
        if not isinstance(item, dict) or not item.get("ip"):
            continue
        ip = str(item["ip"])
        uid = canonical_uid(item.get("uid") or item.get("mac"))
        kind = normalize_device_kind(item.get("kind"))
        keys = _ignored_keys(ip, uid)
        if not include_ignored and any(key in state["ignored"] for key in keys):
            continue
        if include_ignored:
            _remove_ignored_keys(state, keys)

        by_uid = _record_by_uid(state["lights"], uid)
        by_ip = _record_by_ip(state["lights"], ip)
        if uid:
            # A reported UID is authoritative. Only use the IP fallback when
            # the current record has no UID of its own; never overwrite a
            # known device identity merely because its IP was reused.
            record = by_uid
            if by_ip and by_ip is not record:
                if by_ip.get("uid"):
                    _detach_record(by_ip)
                elif record is None:
                    record = by_ip
        else:
            # Without a reported UID, the current IP is the only safe handle.
            record = by_ip

        if record is None:
            record = _new_record(state, ip, uid)
            state["lights"].append(record)
        else:
            old_ip = record.get("ip")
            if old_ip and old_ip != ip:
                record["last_ip"] = old_ip
            record["ip"] = ip
            record.pop("last_ip", None)
            if uid:
                record["uid"] = uid
        if kind:
            record["kind"] = kind
        tracked.append(record)
    state["lights"] = _sort_lights(state["lights"])
    return tracked


# ---------- protocol ----------

def udp_call(ip, payload, timeout=CMD_TIMEOUT):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(json.dumps(payload).encode(), (ip, PORT))
        data, _addr = sock.recvfrom(2048)
        return json.loads(data.decode())
    finally:
        sock.close()


def get_pilot(ip):
    resp = udp_call(ip, {"method": "getPilot", "params": {}})
    return resp.get("result", {})


def get_system_config(ip):
    resp = udp_call(
        ip,
        {"method": "getSystemConfig", "params": {}},
        timeout=SYSTEM_CONFIG_TIMEOUT,
    )
    result = resp.get("result")
    return result if isinstance(result, dict) else {}


def enrich_discovery_uid(record):
    """Backfill stable UID and device kind from read-only system config."""
    if not record.get("ip"):
        return record
    if record.get("uid") and record.get("kind"):
        return record
    try:
        config = get_system_config(record["ip"])
        uid = canonical_uid(config.get("mac") or config.get("deviceMac"))
        kind = classify_module(config.get("moduleName"))
        if uid:
            record["uid"] = uid
        if kind:
            record["kind"] = kind
    except (socket.timeout, OSError, ValueError, TypeError):
        pass
    return record


def set_pilot(ip, params):
    resp = udp_call(ip, {"method": "setPilot", "params": params})
    ok = resp.get("result", {}).get("success", False)
    if not ok:
        raise RuntimeError("bulb rejected the command")
    time.sleep(0.35)  # let the bulb apply before anyone reads state back


def parse_discovery_packet(packet):
    """Extract the stable MAC UID from a WiZ registration response."""
    try:
        if isinstance(packet, bytes):
            packet = packet.decode("utf-8")
        data = json.loads(packet)
    except (UnicodeDecodeError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result = data.get("result")
    if not isinstance(result, dict):
        result = {}
    uid = canonical_uid(result.get("mac") or result.get("deviceMac") or data.get("mac"))
    return {"uid": uid} if uid else {}


def discover():
    """Broadcast on port 38899; every WiZ device on the LAN answers."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            my_ip = probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        my_ip = "1.1.1.1"

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(DISCOVERY_WAIT)
    registration = json.dumps({
        "method": "registration",
        "params": {"phoneIp": my_ip, "phoneMac": "", "register": False},
    })
    found = {}
    try:
        for broadcast in ("255.255.255.255", my_ip.rsplit(".", 1)[0] + ".255"):
            try:
                sock.sendto(registration.encode(), (broadcast, PORT))
            except OSError:
                pass
        while True:
            packet, addr = sock.recvfrom(2048)
            record = {"ip": addr[0]}
            record.update(parse_discovery_packet(packet))
            previous = found.get(addr[0])
            if previous and not record.get("uid"):
                record["uid"] = previous.get("uid")
            found[addr[0]] = record
    except socket.timeout:
        pass
    finally:
        sock.close()
    records = [found[ip] for ip in sorted(found)]
    return [enrich_discovery_uid(record) for record in records]


# ---------- self-update ----------

def _version_tuple(value):
    match = re.match(
        r"^\s*(\d+)\.(\d+)\.(\d+)"
        r"(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?\s*$",
        str(value),
    )
    if not match:
        return None
    prerelease = match.group(4)
    if prerelease is None:
        # A stable release sorts after every prerelease of the same version.
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)), 1, ())
    identifiers = []
    for identifier in prerelease.split("."):
        if not identifier or (identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0")):
            return None
        if identifier.isdigit():
            identifiers.append((0, int(identifier)))
        else:
            identifiers.append((1, identifier))
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), 0, tuple(identifiers))


def _extract_project_version(text):
    match = re.search(r"^version\s*=\s*[\"']([^\"']+)[\"']", text, re.MULTILINE)
    return match.group(1) if match else None


def _extract_source_version(text):
    try:
        tree = ast.parse(text, filename="wiz.py")
    except SyntaxError:
        return None
    versions = []
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if any(isinstance(target, ast.Name) and target.id == "VERSION" for target in targets):
            value = node.value
            if hasattr(ast, "Constant") and isinstance(value, ast.Constant) and isinstance(value.value, str):
                versions.append(value.value)
            elif hasattr(ast, "Str") and isinstance(value, ast.Str):
                versions.append(value.s)
    return versions[0] if len(versions) == 1 else None


def _extract_skill_version(text):
    match = re.search(r"^version:\s*([^\s]+)", text, re.MULTILINE)
    return match.group(1) if match else None


def _validate_update_ref(ref):
    if not isinstance(ref, str) or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$", ref):
        raise ValueError("invalid update ref")
    if ".." in ref or "@{" in ref or ref.endswith((".", "/")):
        raise ValueError("invalid update ref")
    return ref


def _update_url(ref, path):
    return "%s/raw/%s/%s" % (REPOSITORY_URL, quote(ref, safe="/"), quote(path, safe="/"))


def _fetch_url(url):
    request = Request(url, headers={"User-Agent": "wizterm/%s" % VERSION})
    with urlopen(request, timeout=20) as response:
        data = response.read(MAX_UPDATE_BYTES + 1)
    if len(data) > MAX_UPDATE_BYTES:
        raise ValueError("remote update file is too large")
    return data.decode("utf-8-sig")


def _is_wiz_script(path):
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8-sig") as handle:
            text = handle.read(MAX_UPDATE_BYTES + 1)
    except (OSError, UnicodeError):
        return False
    if len(text.encode("utf-8")) > MAX_UPDATE_BYTES:
        return False
    return (
        _extract_source_version(text) is not None
        and "def main(" in text
        and 'REPOSITORY_URL = "https://github.com/himanusia/wizterm"' in text
    )


def _update_targets():
    candidates = [os.path.expanduser("~/.local/bin/wiz")]
    argv_path = os.path.abspath(os.path.expanduser(sys.argv[0]))
    if os.path.basename(argv_path) == "wiz":
        candidates.insert(0, argv_path)
    targets = []
    for candidate in candidates:
        candidate = os.path.abspath(candidate)
        if candidate not in targets and _is_wiz_script(candidate):
            targets.append(candidate)
    return targets


def _default_hermes_home():
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = local_appdata or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.abspath(os.path.join(base, "hermes"))
    return os.path.abspath(os.path.expanduser("~/.hermes"))


def _hermes_home():
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    default_home = _default_hermes_home()
    active_profile_path = os.path.join(default_home, "active_profile")
    try:
        with open(active_profile_path, encoding="utf-8") as handle:
            active_profile = handle.read().strip()
    except (OSError, UnicodeError):
        active_profile = ""
    if active_profile and active_profile != "default":
        raise ValueError(
            "HERMES_HOME is unset while Hermes profile '%s' is active; "
            "set HERMES_HOME before updating the skill" % active_profile
        )
    return default_home


def _path_is_within(path, root):
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except ValueError:
        return False


def _hermes_skill_legacy_paths(hermes_home):
    skills_root = os.path.join(hermes_home, "skills")
    return [
        os.path.join(skills_root, "smart-home", "wiz", "SKILL.md"),
        os.path.join(skills_root, "wiz", "SKILL.md"),
    ]


def _hermes_skill_path():
    hermes_home = _hermes_home()
    override = os.environ.get("WIZ_HERMES_SKILL_PATH", "").strip()
    if override:
        path = os.path.abspath(os.path.expanduser(override))
        if not _path_is_within(path, os.path.join(hermes_home, "skills")):
            raise ValueError("WIZ_HERMES_SKILL_PATH must stay under HERMES_HOME/skills")
        return path
    desired = os.path.join(
        hermes_home, "skills", "smart-home", "wiz-lan-control", "SKILL.md",
    )
    if os.path.lexists(desired):
        return desired
    legacy = [path for path in _hermes_skill_legacy_paths(hermes_home) if os.path.lexists(path)]
    return legacy[0] if legacy else desired


def _skill_paths(harnesses):
    home = os.path.expanduser("~")
    paths = []
    for harness in harnesses:
        if harness == "hermes":
            path = _hermes_skill_path()
            if path not in paths:
                paths.append(path)
            if not os.environ.get("WIZ_HERMES_SKILL_PATH", "").strip():
                for legacy_path in _hermes_skill_legacy_paths(_hermes_home()):
                    if os.path.lexists(legacy_path) and legacy_path not in paths:
                        paths.append(legacy_path)
            continue
        if harness == "codex":
            path = os.path.join(home, ".agents", "skills", "wiz-lan-control", "SKILL.md")
        elif harness == "claude":
            path = os.path.join(home, ".claude", "skills", "wiz-lan-control", "SKILL.md")
        elif harness == "opencode":
            path = os.path.join(home, ".config", "opencode", "skills", "wiz-lan-control", "SKILL.md")
        else:
            raise ValueError("unknown harness '%s'" % harness)
        if path not in paths:
            paths.append(path)
    return paths


def _parse_harnesses(value):
    names = [part.strip().lower() for part in str(value).split(",")]
    if not names or any(not name for name in names):
        raise ValueError("harness list cannot be empty")
    if "all" in names:
        if len(names) != 1:
            raise ValueError("all cannot be combined with another harness")
        return UPDATE_HARNESSES
    invalid = [name for name in names if name not in UPDATE_HARNESSES]
    if invalid:
        raise ValueError("unknown harness '%s'" % invalid[0])
    return tuple(dict.fromkeys(names))


def _read_text_or_empty(path):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            return handle.read()
    except (OSError, UnicodeError):
        return ""


def _safe_update_path(path):
    path = os.path.abspath(os.path.expanduser(path))
    real_path = os.path.realpath(path)
    if os.path.normcase(real_path) != os.path.normcase(path):
        # macOS exposes /var and /tmp as stable system aliases. Allow only
        # those exact prefix translations; any additional link below them is
        # still rejected.
        allowed_system_alias = False
        if sys.platform == "darwin":
            for logical, physical in (("/var", "/private/var"), ("/tmp", "/private/tmp")):
                if path == logical or path.startswith(logical + os.sep):
                    expected = physical + path[len(logical):]
                    allowed_system_alias = os.path.normcase(real_path) == os.path.normcase(expected)
                    if allowed_system_alias:
                        break
        if not allowed_system_alias:
            raise OSError("refusing to update through a symlink or junction: %s" % path)
    return path


def _transactional_write_updates(updates):
    entries = []
    seen = set()
    try:
        for raw_path, text, executable in updates:
            path = _safe_update_path(raw_path)
            if path in seen:
                raise OSError("duplicate update target: %s" % path)
            seen.add(path)
            directory = os.path.dirname(path)
            os.makedirs(directory, exist_ok=True)
            # Re-check after creating missing parents to close the common
            # symlink-parent path before staging or replacing anything.
            path = _safe_update_path(path)
            if os.path.lexists(path) and not os.path.isfile(path):
                raise OSError("update target is not a regular file: %s" % path)
            mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else (
                0o755 if executable else 0o644
            )
            entry = {
                "path": path,
                "stage": None,
                "backup": None,
                "mode": mode,
                "existed": os.path.exists(path),
                "moved": False,
                "committed": False,
            }
            entries.append(entry)

            fd, stage = tempfile.mkstemp(prefix=".wiz-update-", dir=directory)
            entry["stage"] = stage
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(stage, mode)

            if entry["existed"]:
                fd, backup = tempfile.mkstemp(prefix=".wiz-backup-", dir=directory)
                entry["backup"] = backup
                with os.fdopen(fd, "wb") as destination:
                    with open(path, "rb") as source:
                        shutil.copyfileobj(source, destination)
                    destination.flush()
                    os.fsync(destination.fileno())
                os.chmod(backup, mode)

        for entry in entries:
            _safe_update_path(entry["path"])
            if entry["backup"]:
                os.replace(entry["path"], entry["backup"])
                entry["moved"] = True
            os.replace(entry["stage"], entry["path"])
            entry["committed"] = True
    except Exception as exc:
        rollback_errors = []
        for entry in reversed(entries):
            try:
                if entry["committed"] and os.path.lexists(entry["path"]):
                    os.unlink(entry["path"])
                if entry["moved"] and entry["backup"] and os.path.lexists(entry["backup"]):
                    os.replace(entry["backup"], entry["path"])
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise OSError(
                "update failed (%s); rollback also failed: %s" %
                (exc, "; ".join(rollback_errors))
            )
        raise
    finally:
        for entry in entries:
            for temporary in (entry["stage"], entry["backup"]):
                if temporary and os.path.lexists(temporary):
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass


def _atomic_write_update(path, text, executable=False):
    _transactional_write_updates([(path, text, executable)])


def _parse_update_args(args):
    ref = DEFAULT_UPDATE_REF
    harness_value = "hermes"
    check = False
    force = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("--help", "-h"):
            if len(args) != 1:
                raise ValueError("--help cannot be combined with other update options")
            return {"help": True}
        if arg == "--check":
            check = True
        elif arg == "--force":
            force = True
        elif arg == "--ref":
            index += 1
            if index >= len(args):
                raise ValueError("--ref needs a branch or tag")
            ref = args[index]
        elif arg.startswith("--ref="):
            ref = arg.split("=", 1)[1]
        elif arg == "--harness":
            index += 1
            if index >= len(args):
                raise ValueError("--harness needs a harness name or all")
            harness_value = args[index]
        elif arg.startswith("--harness="):
            harness_value = arg.split("=", 1)[1]
        else:
            raise ValueError("unknown update option '%s'" % arg)
        index += 1
    return {
        "ref": _validate_update_ref(ref),
        "harnesses": _parse_harnesses(harness_value),
        "check": check,
        "force": force,
        "help": False,
    }


def cmd_update(args):
    try:
        options = _parse_update_args(args)
        if options.get("help"):
            print("usage: wiz update [--check] [--force] [--ref REF] [--harness LIST]")
            print("updates the CLI and selected harness skill copies from GitHub")
            print("harnesses: hermes (default), codex, claude, opencode, or all")
            return 0
        ref = options["ref"]
        remote_source = _fetch_url(_update_url(ref, "wiz.py"))
        remote_project = _fetch_url(_update_url(ref, "pyproject.toml"))
        remote_skill = _fetch_url(_update_url(ref, "skills/wiz/SKILL.md"))
        project_version = _extract_project_version(remote_project)
        source_version = _extract_source_version(remote_source)
        source_version_missing = source_version is None
        # A legacy source may still be classified as an older downgrade, but
        # it is never installable without its own VERSION constant.
        source_version_for_compare = source_version or project_version
        skill_version = _extract_skill_version(remote_skill)
        source_tuple = _version_tuple(source_version_for_compare)
        project_tuple = _version_tuple(project_version)
        skill_tuple = _version_tuple(skill_version)
        current_tuple = _version_tuple(VERSION)
        if not source_tuple or not project_tuple or not skill_tuple or not current_tuple:
            raise ValueError("remote update has invalid version metadata")
        if source_tuple != project_tuple:
            raise ValueError("remote CLI and package versions do not match")
        if source_tuple < current_tuple:
            print("wiz: remote %s is older than local %s; refusing downgrade" % (project_version, VERSION))
            return 0
        if source_version_missing:
            raise ValueError("remote wiz.py is missing VERSION metadata")
        compile(remote_source, "wiz.py", "exec")

        skill_paths = _skill_paths(options["harnesses"])
        skill_states = []
        for skill_path in skill_paths:
            current_skill_version = _extract_skill_version(_read_text_or_empty(skill_path))
            current_skill_tuple = _version_tuple(current_skill_version) or (0, 0, 0, 0, ())
            skill_states.append((skill_path, current_skill_version, current_skill_tuple))
        update_skill_paths = [
            skill_path for skill_path, _version, current_skill_tuple in skill_states
            if skill_tuple >= current_skill_tuple
            and (options["force"] or skill_tuple > current_skill_tuple)
        ]
        update_cli = options["force"] or source_tuple > current_tuple
        update_skill = bool(update_skill_paths)
        print("local CLI %s -> remote CLI %s (%s)" % (VERSION, project_version, ref))
        print("remote skill %s; targets: %s" % (
            skill_version, ", ".join(path for path, _version, _tuple in skill_states)))
        for skill_path, current_skill_version, _current_skill_tuple in skill_states:
            print("local skill %s -> remote skill %s (%s)" % (
                current_skill_version or "missing", skill_version, skill_path))
        if options["check"]:
            if not update_cli and not update_skill:
                print("already up to date")
            else:
                print("update available")
            return 0
        if not update_cli and not update_skill:
            print("already up to date")
            return 0

        updates = []
        targets = []
        if update_cli:
            targets = _update_targets()
            if not targets:
                raise OSError("no installed wiz executable found to update")
            updates.extend((target, remote_source, True) for target in targets)
        updates.extend((skill_path, remote_skill, False) for skill_path in update_skill_paths)
        _transactional_write_updates(updates)
        if update_cli:
            print("updated CLI: %s" % ", ".join(targets))
        if update_skill:
            print("updated skills: %s" % ", ".join(update_skill_paths))
        print("reload the relevant harness session to load skill updates")
        return 0
    except (HTTPError, URLError, OSError, UnicodeError, SyntaxError, ValueError) as exc:
        print("wiz: update failed: %s" % exc)
        return 1


# ---------- command helpers ----------

def normalize_leading_target(argv):
    """Accept ``wiz <target> <command> ...`` as a natural target-first form."""
    argv = list(argv)
    if len(argv) < 2:
        return argv, None
    leading, command = argv[0], argv[1]
    if leading in TARGET_FIRST_COMMANDS or leading in (
        "update", "find", "add", "list", "help", "--help", "-h",
        "version", "--version", "-V",
    ):
        return argv, None
    if command not in TARGET_FIRST_COMMANDS and not command.isdigit():
        return argv, None
    target = leading[1:] if leading.startswith("@") else leading
    target = target.strip()
    if not target:
        sys.exit("wiz: target after '@' cannot be empty")
    return [command] + argv[2:], target


def split_target(cmd, args):
    """Split the optional trailing target from command arguments."""
    args = list(args)
    if args and args[-1].startswith("@"):
        target = args[-1][1:].strip()
        if not target:
            sys.exit("wiz: target after '@' cannot be empty")
        return args[:-1], target
    expected = COMMAND_ARGUMENTS.get(cmd)
    if expected is None and cmd.isdigit():
        expected = 0
    if expected is not None and len(args) > expected:
        target = args[-1].strip()
        if not target:
            sys.exit("wiz: target cannot be empty")
        return args[:-1], target
    return args, None


def resolve_targets(state, target=None):
    lights = _sort_lights(state.get("lights", []))
    if target is None:
        return lights
    target = str(target).strip()
    if not target:
        sys.exit("wiz: target cannot be empty")
    if looks_like_ip(target):
        matches = [light for light in lights if light.get("ip") == target]
        if matches:
            return matches
        # Direct IP control remains useful before a device is registered.
        return [{"id": "-", "uid": None, "ip": target, "name": None, "_synthetic": True}]

    exact_id = [light for light in lights if str(light.get("id")) == target]
    if exact_id:
        return exact_id

    uid = canonical_uid(target)
    if uid:
        uid_matches = [light for light in lights if canonical_uid(light.get("uid")) == uid]
        if uid_matches:
            return uid_matches

    lowered = target.lower()
    name_matches = [
        light for light in lights
        if light.get("name") and light["name"].lower().startswith(lowered)
    ]
    if name_matches:
        return name_matches
    sys.exit("wiz: no light, ID, or name matching '%s'" % target)


def looks_like_ip(text):
    parts = str(text).split(".")
    return (len(parts) == 4
            and all(part.isdigit() and 0 <= int(part) <= 255 for part in parts))


def parse_temp(value):
    try:
        kelvin = int(value)
    except ValueError:
        sys.exit("wiz: temp must be a number in Kelvin, e.g. 'wiz temp 3500'")
    return max(2200, min(kelvin, 6500))


def parse_rgb(value):
    text = str(value).strip().lower()
    if text.startswith("#"):
        text = text[1:]
    if len(text) != 6 or any(char not in string.hexdigits for char in text):
        sys.exit("wiz: rgb expects hex like ff8800 or '#ff8800'")
    return {
        "r": int(text[0:2], 16),
        "g": int(text[2:4], 16),
        "b": int(text[4:6], 16),
        "state": True,
    }


def _normalized_label(value):
    return " ".join(str(value).strip().lower().replace("_", " ").replace("-", " ").split())


def preset_params(value):
    name = str(value).strip().lower().replace("_", "-")
    if name in PRESETS:
        params = dict(PRESETS[name])
        params["state"] = True
        return params
    if name in COLOR_PRESETS:
        return parse_rgb(COLOR_PRESETS[name])
    sys.exit("wiz: unknown preset '%s' (run 'wiz preset' for help)" % value)


def ambience_id(value):
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    normalized = _normalized_label(text)
    for scene_id, name in AMBIENCE.items():
        if _normalized_label(name) == normalized:
            return scene_id
    sys.exit("wiz: unknown ambience '%s' (run 'wiz ambience' for help)" % value)


def build_params(cmd, args):
    """Translate a command into setPilot params, or None for read-only cmds."""
    if cmd == "on":
        return {"state": True}
    if cmd == "off":
        return {"state": False}
    if cmd.isdigit():
        return {"dimming": max(10, min(int(cmd), 100)), "state": True}
    if cmd in PRESETS:
        params = dict(PRESETS[cmd])
        params["state"] = True
        return params
    if cmd == "temp":
        return {"temp": parse_temp(args[0]), "state": True}
    if cmd in ("preset", "color"):
        return preset_params(args[0])
    if cmd == "rgb":
        return parse_rgb(args[0])
    if cmd in ("ambience", "ambiance", "scene"):
        return {"sceneId": ambience_id(args[0]), "state": True}
    return None


def _record_name(record):
    return record.get("name") or "-"


def _record_prefix(record):
    ip = record.get("ip")
    if not ip:
        last_ip = record.get("last_ip")
        ip = (str(last_ip) + " (offline)") if last_ip else "-"
    return "[%3s] %-12s %-14s %-15s" % (
        record.get("id", "-"),
        _record_name(record),
        display_device_kind(record),
        ip,
    )


def apply(record, params):
    ip = record.get("ip")
    if not ip:
        print("  %s  ✗ no current IP" % _record_prefix(record))
        return False
    try:
        if params is None:
            pilot = get_pilot(ip)
            state = "ON " if pilot.get("state") else "off"
            bits = ["dim=%s%%" % pilot.get("dimming")]
            if pilot.get("sceneId"):
                scene_id = pilot["sceneId"]
                scene_name = AMBIENCE.get(scene_id)
                scene_label = "%s (%s)" % (scene_id, scene_name) if scene_name else str(scene_id)
                bits.append("scene=%s" % scene_label)
            if pilot.get("temp"):
                bits.append("%sK" % pilot["temp"])
            if any(key in pilot for key in ("r", "g", "b")):
                bits.append("rgb=%02x%02x%02x" % (
                    pilot.get("r", 0), pilot.get("g", 0), pilot.get("b", 0)))
            if record.get("uid"):
                bits.append("mac=%s" % display_uid(record["uid"]))
            print("  %s  %s  %s" % (_record_prefix(record), state, "  ".join(bits)))
            return True

        set_pilot(ip, params)
        pilot = get_pilot(ip)
        state = "ON " if pilot.get("state") else "off"
        print("  %s  -> %s  %s" % (_record_prefix(record), state, label_for(params)))
        return True
    except (socket.timeout, OSError, ValueError):
        print("  %s  ✗ unreachable" % _record_prefix(record))
        return False
    except RuntimeError as exc:
        print("  %s  ✗ %s" % (_record_prefix(record), exc))
        return False


def label_for(params):
    labels = []
    if "dimming" in params:
        labels.append("%s%%" % params["dimming"])
    if "temp" in params:
        labels.append("%sK" % params["temp"])
    if "r" in params:
        labels.append("#%02x%02x%02x" % (params["r"], params["g"], params["b"]))
    if "sceneId" in params:
        scene_id = params["sceneId"]
        scene_name = AMBIENCE.get(scene_id)
        labels.append("ambience %s (%s)" % (scene_id, scene_name) if scene_name else "scene %s" % scene_id)
    if params.get("state") is False:
        labels.append("power off")
    return ", ".join(labels) or "power on"


def cmd_preset_help():
    print("Built-in lighting presets:")
    for name in ("night", "warm", "white", "cool"):
        params = PRESETS[name]
        details = []
        if "dimming" in params:
            details.append("%s%%" % params["dimming"])
        if "temp" in params:
            details.append("%sK" % params["temp"])
        print("  %-10s %s" % (name, ", ".join(details)))
    print("Built-in color presets:")
    for name in sorted(COLOR_PRESETS):
        print("  %-10s #%s" % (name, COLOR_PRESETS[name]))
    print("Use: wiz preset <name> [@target]  (or: wiz color <name> [@target])")
    return 0


def cmd_ambience_help():
    print("Known WiZ ambience/scene IDs (firmware support may vary):")
    for scene_id in sorted(AMBIENCE):
        print("  %-4s %s" % (scene_id, AMBIENCE[scene_id]))
    print("Use: wiz ambience <id|name> [@target]")
    print("Alias: wiz scene <id|name> [@target]")
    print("Custom modes and newer firmware effects may expose additional IDs.")
    return 0


def cmd_rgb_help():
    print("RGB expects six hexadecimal digits:")
    print("  wiz rgb ff8800 @desk       orange")
    print("  wiz rgb '#ff8800' @desk    same color with #")
    print("Use: wiz rgb RRGGBB [@target]")
    return 0


def _validate_args(cmd, args):
    expected = COMMAND_ARGUMENTS.get(cmd)
    if expected is not None and len(args) != expected:
        if cmd == "rename":
            sys.exit("usage: wiz rename <name> [@target]")
        if cmd == "temp":
            sys.exit("usage: wiz temp <kelvin> [@target]")
        if cmd in ("preset", "color"):
            sys.exit("usage: wiz %s <name> [@target]" % cmd)
        if cmd == "rgb":
            sys.exit("usage: wiz rgb RRGGBB [@target]")
        if cmd in ("ambience", "ambiance", "scene"):
            sys.exit("usage: wiz ambience <id|name> [@target]")
        sys.exit("usage: wiz %s [@target]" % cmd)


# ---------- subcommands ----------

def cmd_status(state, target=None, auto_discover=False):
    if auto_discover:
        found = discover()
        if found:
            merge_discovered(state, found)
            save_state(state)
        elif not state["lights"]:
            print("no WiZ lights answered the broadcast (are you on the same network?)")
            return 1

    targets = resolve_targets(state, target)
    if not targets:
        print("no tracked lights; run 'wiz' or 'wiz find' to discover one")
        return 1
    print("%d light(s):" % len(targets))
    results = [apply(light, None) for light in targets]
    return 0 if all(results) else 1


def cmd_find(state, include_forgotten=False):
    found = discover()
    if not found:
        print("no WiZ lights answered the broadcast (are you on the same network?)")
        return 1
    tracked = merge_discovered(state, found, include_ignored=include_forgotten)
    save_state(state)
    print("found %d light(s), tracking %d:" % (len(found), len(tracked)))
    results = [apply(light, None) for light in tracked]
    if not tracked:
        print("  all discovered lights are forgotten; use --include-forgotten to re-adopt")
        return 0
    return 0 if all(results) else 1


def cmd_rename(state, args, targets):
    _validate_args("rename", args)
    if not targets:
        sys.exit("wiz: no lights known yet - run 'wiz' first")
    name = args[0]
    for target in targets:
        if target.get("_synthetic"):
            target = _new_record(state, target["ip"])
            state["lights"].append(target)
        target["name"] = name
        print("  %s  renamed to '%s'" % (_record_prefix(target), name))
    save_state(state)


def _remember_ignored(state, key):
    if key and key not in state["ignored"]:
        state["ignored"].append(key)


def cmd_forget(state, targets):
    doomed = targets or list(state["lights"])
    seen = set()
    for target in doomed:
        identity = (str(target.get("id")), target.get("ip"))
        if identity in seen:
            continue
        seen.add(identity)
        actual = next(
            (light for light in state["lights"]
             if light.get("id") == target.get("id") and light.get("ip") == target.get("ip")),
            None,
        )
        if actual:
            key = actual.get("uid")
            if not key:
                old_ip = actual.get("ip") or actual.get("last_ip")
                key = "ip:" + str(old_ip) if old_ip else "id:" + str(actual["id"])
            _remember_ignored(state, key)
            state["lights"].remove(actual)
            print("  %s  forgotten" % _record_prefix(actual))
        elif target.get("ip"):
            _remember_ignored(state, "ip:" + target["ip"])
            print("  %s  forgotten" % target["ip"])
    save_state(state)


def cmd_add(state, ip):
    if not looks_like_ip(ip):
        sys.exit("wiz: '%s' does not look like an IP address" % ip)
    record = _record_by_ip(state["lights"], ip)
    if record is None:
        record = _new_record(state, ip)
        state["lights"].append(record)
    _remove_ignored_keys(state, ["ip:" + ip])
    save_state(state)
    print("added %s" % _record_prefix(record))
    return 0 if apply(record, None) else 1


def cmd_control(state, cmd, args, target):
    _validate_args(cmd, args)
    targets = resolve_targets(state, target)
    if not targets:
        print("no tracked lights; run 'wiz' or 'wiz find' to discover one")
        return 1
    params = build_params(cmd, args)
    print("%d light(s):" % len(targets))
    results = [apply(light, params) for light in targets]
    return 0 if all(results) else 1


# Optional audio-reactive shows. The core CLI never imports the audio stack;
# the extras live in wiz_live.py and are loaded only when one of these words
# is the first argument.
LIVE_COMMANDS = ("shows", "live", "visualize", "visualise", "caramelldansen",
                 "detect", "listen")


def dispatch_live(argv):
    """Hand a live command to the optional wiz_live module."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (here, os.path.join(CONF_DIR, "lib"),
                      os.path.dirname(os.path.abspath(os.path.expanduser(
                          os.environ.get("WIZ_LIVE_HOME", here))))):
        if candidate and candidate not in sys.path:
            sys.path.insert(0, candidate)
    try:
        import wiz_live
    except ImportError as exc:
        sys.exit(
            "wiz: '%s' needs the optional audio extras, and wiz_live.py was not "
            "found next to this script (%s)\n"
            "  install: pip install \"wizterm[live,recognize]\"\n"
            "  docs:    %s#live-shows" % (argv[0], exc, REPOSITORY_URL)
        )
    wiz_live.CORE = sys.modules[__name__]
    return wiz_live.main(argv)


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if argv and argv[0] in ("-V", "--version", "version"):
        print("wiz %s" % VERSION)
        return 0
    if argv and argv[0] in LIVE_COMMANDS:
        return dispatch_live(argv)

    state = load_state()
    if not argv:
        return cmd_status(state, auto_discover=True)

    argv, leading_target = normalize_leading_target(argv)
    cmd = argv[0]
    if cmd == "update":
        return cmd_update(argv[1:])
    if cmd == "find":
        if len(argv) == 1:
            return cmd_find(state)
        if len(argv) == 2 and argv[1] == "--include-forgotten":
            return cmd_find(state, include_forgotten=True)
        sys.exit("usage: wiz find [--include-forgotten]")

    if cmd == "add":
        if len(argv) != 2:
            sys.exit("usage: wiz add <ip>")
        return cmd_add(state, argv[1])

    if cmd == "list":
        if len(argv) != 1:
            sys.exit("usage: wiz list")
        return cmd_status(state)

    help_words = ("help", "list", "--help", "-h")
    if cmd in ("preset", "presets", "color") and (
            len(argv) == 1 or (len(argv) == 2 and argv[1] in help_words)):
        return cmd_preset_help()
    if cmd in ("ambience", "ambiance", "scenes", "scene") and (
            len(argv) == 1 or (len(argv) == 2 and argv[1] in help_words)):
        return cmd_ambience_help()
    if cmd == "rgb" and len(argv) == 2 and argv[1] in help_words:
        return cmd_rgb_help()

    args, target = split_target(cmd, argv[1:])
    if leading_target is not None:
        if target is not None:
            sys.exit("wiz: target specified twice")
        target = leading_target
    if cmd == "status":
        _validate_args(cmd, args)
        return cmd_status(state, target, auto_discover=target is None)
    if cmd == "rename":
        return cmd_rename(state, args, resolve_targets(state, target))
    if cmd == "forget":
        _validate_args(cmd, args)
        cmd_forget(state, resolve_targets(state, target) if target is not None else [])
        return 0

    if cmd.isdigit() or cmd in ("on", "off") or cmd in PRESETS \
            or cmd in ("temp", "preset", "color", "rgb", "ambience", "ambiance", "scene"):
        return cmd_control(state, cmd, args, target)
    sys.exit("wiz: unknown command '%s' (run 'wiz --help' for help)" % cmd)


if __name__ == "__main__":
    sys.exit(main())
