import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from tempfile import TemporaryDirectory
from unittest.mock import patch

import wiz


def _ahead_of_current():
    """A release version strictly newer than the one under test."""
    parts = wiz.VERSION.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


AHEAD_VERSION = _ahead_of_current()


class WizRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.cache_path = os.path.join(self.tmp.name, "lights.json")
        self.cache_patch = patch.object(wiz, "CACHE_FILE", self.cache_path)
        self.cache_patch.start()

    def tearDown(self):
        self.cache_patch.stop()
        self.tmp.cleanup()

    def test_migrates_legacy_ip_cache_to_numbered_registry(self):
        with open(self.cache_path, "w") as handle:
            json.dump(
                {
                    "lights": [
                        {"ip": "192.0.2.51", "name": "desk"},
                        {"ip": "192.0.2.50", "name": None},
                    ]
                },
                handle,
            )

        state = wiz.load_state()

        self.assertEqual([light["id"] for light in state["lights"]], ["1", "2"])
        self.assertEqual(state["lights"][0]["ip"], "192.0.2.50")
        self.assertEqual(state["lights"][1]["name"], "desk")
        self.assertEqual(state["next_id"], 3)

    def test_registration_response_extracts_mac_uid(self):
        packet = json.dumps(
            {"method": "registration", "result": {"mac": "AA:BB:CC:DD:EE:FF"}}
        ).encode("utf-8")

        self.assertEqual(
            wiz.parse_discovery_packet(packet),
            {"uid": "mac:aabbccddeeff"},
        )

    def test_system_config_mac_backfills_missing_discovery_uid(self):
        record = {"ip": "192.0.2.50", "uid": None}
        with patch.object(
            wiz,
            "get_system_config",
            return_value={"mac": "44:4F:8E:B5:F9:0E", "moduleName": "ESP25_SHRGB_01"},
        ):
            self.assertIs(wiz.enrich_discovery_uid(record), record)
        self.assertEqual(record["uid"], "mac:444f8eb5f90e")
        self.assertEqual(record["kind"], "rgb")

    def test_system_config_backfill_does_not_replace_existing_uid(self):
        record = {
            "ip": "192.0.2.50",
            "uid": "mac:111111111111",
            "kind": "rgb",
        }
        with patch.object(wiz, "get_system_config") as get_config:
            wiz.enrich_discovery_uid(record)
        get_config.assert_not_called()
        self.assertEqual(record["uid"], "mac:111111111111")

    def test_module_kind_labels_cover_rgb_and_tunable_white(self):
        self.assertEqual(wiz.classify_module("ESP25_SHRGB_01"), "rgb")
        self.assertEqual(wiz.classify_module("ESP24_SHTWW_01"), "tunable-white")
        self.assertEqual(wiz.display_device_kind({"kind": "rgb"}), "RGB")
        self.assertEqual(wiz.display_device_kind({"kind": "tunable-white"}), "tunable white")

    def test_mac_keeps_same_id_when_ip_changes(self):
        state = wiz.empty_state()
        first = wiz.merge_discovered(
            state,
            [{"ip": "192.0.2.50", "mac": "AA:BB:CC:DD:EE:FF"}],
        )[0]
        second = wiz.merge_discovered(
            state,
            [{"ip": "192.0.2.99", "mac": "aabbccddeeff"}],
        )[0]

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["ip"], "192.0.2.99")
        self.assertEqual(second["uid"], "mac:aabbccddeeff")
        self.assertEqual(len(state["lights"]), 1)

    def test_different_mac_at_reused_ip_does_not_inherit_old_identity(self):
        state = wiz.empty_state()
        old = wiz.merge_discovered(
            state,
            [{"ip": "192.0.2.50", "mac": "AA:AA:AA:AA:AA:AA"}],
        )[0]
        old["name"] = "desk"
        new = wiz.merge_discovered(
            state,
            [{"ip": "192.0.2.50", "mac": "BB:BB:BB:BB:BB:BB"}],
        )[0]

        self.assertNotEqual(new["id"], old["id"])
        self.assertEqual(old["uid"], "mac:aaaaaaaaaaaa")
        self.assertEqual(old["name"], "desk")
        self.assertIsNone(old["ip"])
        self.assertEqual(new["uid"], "mac:bbbbbbbbbbbb")
        self.assertIsNone(new["name"])
        self.assertEqual(new["ip"], "192.0.2.50")
        self.assertEqual(len(state["lights"]), 2)

    def test_offline_conflict_record_round_trips_without_string_none(self):
        state = wiz.empty_state()
        old = wiz.merge_discovered(
            state,
            [{"ip": "192.0.2.50", "mac": "AA:AA:AA:AA:AA:AA"}],
        )[0]
        old["name"] = "desk"
        wiz.merge_discovered(
            state,
            [{"ip": "192.0.2.50", "mac": "BB:BB:BB:BB:BB:BB"}],
        )
        wiz.save_state(state)

        loaded = wiz.load_state()
        old_loaded = next(
            light for light in loaded["lights"]
            if light["uid"] == "mac:aaaaaaaaaaaa"
        )
        self.assertIsNone(old_loaded["ip"])
        self.assertEqual(old_loaded["last_ip"], "192.0.2.50")

    def test_offline_record_never_sends_command_to_stale_ip(self):
        record = {
            "id": "1",
            "uid": "mac:aaaaaaaaaaaa",
            "ip": None,
            "last_ip": "192.0.2.50",
            "name": "desk",
        }
        with patch.object(wiz, "get_pilot") as get_pilot:
            output = io.StringIO()
            with redirect_stdout(output):
                result = wiz.apply(record, None)

        self.assertFalse(result)
        get_pilot.assert_not_called()
        self.assertIn("no current IP", output.getvalue())

    def test_resolves_numeric_id_name_and_ip(self):
        state = {
            "version": 2,
            "next_id": 3,
            "ignored": [],
            "lights": [
                {"id": "1", "uid": "mac:111111111111", "ip": "192.0.2.50", "name": "desk"},
                {"id": "2", "uid": "mac:222222222222", "ip": "192.0.2.51", "name": "bedroom"},
            ],
        }

        _, target = wiz.split_target("on", ["@2"])
        by_id = wiz.resolve_targets(state, target)
        _, target = wiz.split_target("on", ["desk"])
        by_name = wiz.resolve_targets(state, target)
        _, target = wiz.split_target("on", ["192.0.2.51"])
        by_ip = wiz.resolve_targets(state, target)
        _, brightness_target = wiz.split_target("40", ["desk"])

        self.assertEqual(by_id[0]["id"], "2")
        self.assertEqual(by_name[0]["id"], "1")
        self.assertEqual(by_ip[0]["id"], "2")
        self.assertEqual(brightness_target, "desk")

    def test_record_prefix_keeps_columns_aligned_for_multi_digit_ids(self):
        short_id = wiz._record_prefix({"id": "2", "name": "desk", "ip": "192.0.2.50"})
        long_id = wiz._record_prefix({"id": "10", "name": "lamp", "ip": "192.0.2.51"})
        self.assertEqual(short_id.index("desk"), long_id.index("lamp"))
        self.assertEqual(short_id.index("192.0.2.50"), long_id.index("192.0.2.51"))

    def test_target_first_syntax_normalizes_to_legacy_shape(self):
        self.assertEqual(
            wiz.normalize_leading_target(["desk", "ambience", "romance"]),
            (["ambience", "romance"], "desk"),
        )
        self.assertEqual(
            wiz.normalize_leading_target(["@3", "40"]),
            (["40"], "3"),
        )
        self.assertEqual(
            wiz.normalize_leading_target(["ambience", "romance", "@desk"]),
            (["ambience", "romance", "@desk"], None),
        )

    def test_main_accepts_target_first_syntax(self):
        state = {
            "version": 2,
            "next_id": 2,
            "ignored": [],
            "lights": [
                {"id": "1", "uid": "mac:111111111111", "ip": "192.0.2.50", "name": "desk"},
            ],
        }
        with patch.object(wiz, "load_state", return_value=state):
            with patch.object(wiz, "cmd_control", return_value=0) as control:
                with patch.object(sys, "argv", ["wiz", "desk", "ambience", "romance"]):
                    result = wiz.main()
        self.assertEqual(result, 0)
        control.assert_called_once_with(state, "ambience", ["romance"], "desk")

    def test_target_first_rejects_duplicate_trailing_target(self):
        state = {
            "version": 2,
            "next_id": 2,
            "ignored": [],
            "lights": [],
        }
        with patch.object(wiz, "load_state", return_value=state):
            with patch.object(sys, "argv", ["wiz", "desk", "ambience", "romance", "@3"]):
                with self.assertRaises(SystemExit):
                    wiz.main()

    def test_empty_explicit_target_is_rejected(self):
        state = {
            "version": 2,
            "next_id": 1,
            "ignored": [],
            "lights": [],
        }

        with self.assertRaises(SystemExit):
            wiz.split_target("off", ["@"])
        with self.assertRaises(SystemExit):
            wiz.resolve_targets(state, " ")

    def test_quoted_empty_forget_target_is_rejected_without_deleting(self):
        state = {
            "version": 2,
            "next_id": 2,
            "ignored": [],
            "lights": [
                {"id": "1", "uid": "mac:111111111111", "ip": "192.0.2.50", "name": "desk"},
            ],
        }
        wiz.save_state(state)

        with patch.object(sys, "argv", ["wiz", "forget", ""]):
            with self.assertRaises(SystemExit):
                wiz.main()

        saved = wiz.load_state()
        self.assertEqual(len(saved["lights"]), 1)
        self.assertEqual(saved["lights"][0]["name"], "desk")

    def test_forget_by_id_persists_ignored_uid(self):
        state = {
            "version": 2,
            "next_id": 2,
            "ignored": [],
            "lights": [
                {"id": "1", "uid": "mac:111111111111", "ip": "192.0.2.50", "name": "desk"},
            ],
        }
        wiz.save_state(state)

        args, target = wiz.split_target("forget", ["@1"])
        self.assertEqual(args, [])
        wiz.cmd_forget(state, wiz.resolve_targets(state, target))
        saved = wiz.load_state()

        self.assertEqual(saved["lights"], [])
        self.assertEqual(saved["ignored"], ["mac:111111111111"])

    def test_normal_discovery_skips_forgotten_device_until_re_adopted(self):
        state = {
            "version": 2,
            "next_id": 2,
            "ignored": ["mac:111111111111"],
            "lights": [],
        }
        discovered = [{"ip": "192.0.2.50", "mac": "11:11:11:11:11:11"}]

        tracked = wiz.merge_discovered(state, discovered)
        self.assertEqual(tracked, [])
        self.assertEqual(state["lights"], [])

        tracked = wiz.merge_discovered(state, discovered, include_ignored=True)
        self.assertEqual([light["id"] for light in tracked], ["2"])
        self.assertEqual(state["ignored"], [])

    def test_bare_status_refreshes_discovery_before_listing(self):
        response = {"ip": "192.0.2.50", "mac": "AA:BB:CC:DD:EE:FF"}
        with patch.object(wiz, "discover", return_value=[response]) as discover:
            with patch.object(wiz, "get_pilot", return_value={"state": True, "dimming": 80}):
                with patch.object(sys, "argv", ["wiz"]):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        result = wiz.main()

        discover.assert_called_once_with()
        self.assertEqual(result, 0)
        self.assertRegex(output.getvalue(), r"\[\s*1\]\s+")
        self.assertIn("192.0.2.50", output.getvalue())

    def test_rgb_accepts_hash_hex_example(self):
        self.assertEqual(
            wiz.build_params("rgb", ["#ff8800"]),
            {"r": 255, "g": 136, "b": 0, "state": True},
        )

    def test_named_color_preset_builds_rgb_params(self):
        self.assertEqual(
            wiz.build_params("preset", ["orange"]),
            {"r": 255, "g": 136, "b": 0, "state": True},
        )
        self.assertEqual(
            wiz.build_params("color", ["blue"]),
            {"r": 0, "g": 102, "b": 255, "state": True},
        )

    def test_named_ambience_builds_scene_params(self):
        self.assertEqual(
            wiz.build_params("ambience", ["Ocean"]),
            {"sceneId": 1, "state": True},
        )
        self.assertEqual(
            wiz.build_params("scene", ["35"]),
            {"sceneId": 35, "state": True},
        )

    def test_preset_help_lists_default_and_color_presets(self):
        with patch.object(sys, "argv", ["wiz", "preset"]):
            output = io.StringIO()
            with redirect_stdout(output):
                result = wiz.main()

        self.assertEqual(result, 0)
        self.assertIn("night", output.getvalue())
        self.assertIn("orange", output.getvalue())
        self.assertIn("#ff8800", output.getvalue())

    def test_ambience_help_lists_ids_and_names(self):
        with patch.object(sys, "argv", ["wiz", "ambience"]):
            output = io.StringIO()
            with redirect_stdout(output):
                result = wiz.main()

        self.assertEqual(result, 0)
        self.assertIn("1", output.getvalue())
        self.assertIn("Ocean", output.getvalue())
        self.assertIn("35", output.getvalue())
        self.assertIn("Alarm", output.getvalue())
        self.assertIn("1000", output.getvalue())
        self.assertIn("Rhythm", output.getvalue())
        self.assertIn("256", output.getvalue())
        self.assertIn("Custom Mode 1", output.getvalue())

    def test_update_writes_cli_and_hermes_skill(self):
        remote_source = 'VERSION = "%s"\n' % AHEAD_VERSION
        remote_project = '[project]\nversion = "%s"\n' % AHEAD_VERSION
        remote_skill = "---\nname: wiz-lan-control\nversion: 1.4.0\n---\nupdated\n"
        with TemporaryDirectory() as tmp:
            cli_path = os.path.join(tmp, "wiz")
            skill_path = os.path.join(tmp, "SKILL.md")

            def fetch(url):
                if url.endswith("/wiz.py"):
                    return remote_source
                if url.endswith("/pyproject.toml"):
                    return remote_project
                if url.endswith("/skills/wiz/SKILL.md"):
                    return remote_skill
                raise AssertionError(url)

            with patch.object(wiz, "_fetch_url", side_effect=fetch):
                with patch.object(wiz, "_update_targets", return_value=[cli_path]):
                    with patch.object(wiz, "_hermes_skill_path", return_value=skill_path):
                        result = wiz.cmd_update(["--ref", "release-test"])

            self.assertEqual(result, 0)
            with open(cli_path) as handle:
                self.assertEqual(handle.read(), remote_source)
            with open(skill_path) as handle:
                self.assertEqual(handle.read(), remote_skill)

    def test_update_syncs_selected_non_hermes_harnesses(self):
        remote_source = 'VERSION = "%s"\n' % AHEAD_VERSION
        remote_project = '[project]\nversion = "%s"\n' % AHEAD_VERSION
        remote_skill = "---\nname: wiz-lan-control\nversion: 1.5.0\n---\nportable update\n"
        with TemporaryDirectory() as tmp:
            cli_path = os.path.join(tmp, "wiz")
            codex_path = os.path.join(tmp, "agents", "SKILL.md")
            claude_path = os.path.join(tmp, "claude", "SKILL.md")

            def fetch(url):
                if url.endswith("/wiz.py"):
                    return remote_source
                if url.endswith("/pyproject.toml"):
                    return remote_project
                if url.endswith("/skills/wiz/SKILL.md"):
                    return remote_skill
                raise AssertionError(url)

            with patch.object(wiz, "_fetch_url", side_effect=fetch):
                with patch.object(wiz, "_update_targets", return_value=[cli_path]):
                    with patch.object(wiz, "_skill_paths", return_value=[codex_path, claude_path]) as paths:
                        result = wiz.cmd_update([
                            "--ref", "release-test", "--harness", "codex,claude",
                        ])

            self.assertEqual(result, 0)
            paths.assert_called_once_with(("codex", "claude"))
            with open(cli_path) as handle:
                self.assertEqual(handle.read(), remote_source)
            with open(codex_path) as handle:
                self.assertEqual(handle.read(), remote_skill)
            with open(claude_path) as handle:
                self.assertEqual(handle.read(), remote_skill)

    def test_version_comparison_preserves_prerelease_order(self):
        alpha = wiz._version_tuple("0.7.0-alpha")
        stable = wiz._version_tuple("0.7.0")
        alpha_one = wiz._version_tuple("0.7.0-alpha.1")
        self.assertTrue(alpha is not None and stable is not None and alpha < stable)
        self.assertTrue(alpha is not None and alpha_one is not None and alpha < alpha_one)
        self.assertIsNone(wiz._version_tuple("0.7.0-alpha..1"))
        self.assertIsNone(wiz._version_tuple("0.7.0-01"))

    def test_arbitrary_executable_is_not_recognized_as_wiz(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "wiz")
            with open(path, "w") as handle:
                handle.write("#!/bin/sh\necho unrelated\n")
            os.chmod(path, 0o755)
            self.assertFalse(wiz._is_wiz_script(path))

    def test_skill_path_override_cannot_escape_hermes_home(self):
        with TemporaryDirectory() as tmp:
            hermes_home = os.path.join(tmp, "hermes")
            outside = os.path.join(tmp, "outside", "SKILL.md")
            with patch.dict(os.environ, {
                "HERMES_HOME": hermes_home,
                "WIZ_HERMES_SKILL_PATH": outside,
            }, clear=False):
                with self.assertRaises(ValueError):
                    wiz._hermes_skill_path()

    def test_symlinked_update_parent_is_rejected(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with TemporaryDirectory() as tmp:
            real_dir = os.path.join(tmp, "real")
            link_dir = os.path.join(tmp, "link")
            os.makedirs(real_dir)
            try:
                os.symlink(real_dir, link_dir)
            except OSError as exc:
                self.skipTest("symlink creation unavailable: %s" % exc)
            with self.assertRaises(OSError):
                wiz._atomic_write_update(os.path.join(link_dir, "SKILL.md"), "content\n")

    def test_transaction_rolls_back_when_later_replace_fails(self):
        with TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "first")
            second = os.path.join(tmp, "second")
            with open(first, "w") as handle:
                handle.write("old first")
            with open(second, "w") as handle:
                handle.write("old second")
            real_replace = os.replace
            calls = {"count": 0}

            def flaky_replace(source, destination):
                calls["count"] += 1
                if calls["count"] == 4:
                    raise OSError("injected replace failure")
                return real_replace(source, destination)

            with patch.object(wiz.os, "replace", side_effect=flaky_replace):
                with self.assertRaises(OSError):
                    wiz._transactional_write_updates([
                        (first, "new first", False),
                        (second, "new second", False),
                    ])
            with open(first) as handle:
                self.assertEqual(handle.read(), "old first")
            with open(second) as handle:
                self.assertEqual(handle.read(), "old second")

    def test_force_does_not_downgrade_newer_skill(self):
        remote_source = 'VERSION = "%s"\n' % AHEAD_VERSION
        remote_project = '[project]\nversion = "%s"\n' % AHEAD_VERSION
        remote_skill = "---\nname: wiz-lan-control\nversion: 1.0.0\n---\nold\n"
        with TemporaryDirectory() as tmp:
            cli_path = os.path.join(tmp, "wiz")
            skill_path = os.path.join(tmp, "SKILL.md")
            with open(skill_path, "w") as handle:
                handle.write("---\nversion: 1.5.0\n---\nnew\n")

            def fetch(url):
                if url.endswith("/wiz.py"):
                    return remote_source
                if url.endswith("/pyproject.toml"):
                    return remote_project
                return remote_skill

            with patch.object(wiz, "_fetch_url", side_effect=fetch):
                with patch.object(wiz, "_update_targets", return_value=[cli_path]):
                    with patch.object(wiz, "_skill_paths", return_value=[skill_path]):
                        result = wiz.cmd_update(["--force", "--ref", "release-test"])
            self.assertEqual(result, 0)
            with open(skill_path) as handle:
                self.assertIn("version: 1.5.0", handle.read())

    def test_newer_legacy_source_without_version_is_rejected(self):
        remote_source = "print('legacy wiz source')\n"
        remote_project = '[project]\nversion = "%s"\n' % AHEAD_VERSION
        remote_skill = "---\nname: wiz-lan-control\nversion: 1.5.0\n---\n"
        responses = {
            "wiz.py": remote_source,
            "pyproject.toml": remote_project,
            "skills/wiz/SKILL.md": remote_skill,
        }

        def fetch(url):
            return next(value for suffix, value in responses.items() if url.endswith("/" + suffix))

        with patch.object(wiz, "_fetch_url", side_effect=fetch):
            with patch.object(wiz, "_update_targets") as targets:
                result = wiz.cmd_update(["--ref", "release-test"])
        self.assertNotEqual(result, 0)
        targets.assert_not_called()

    def test_hermes_profile_fallback_requires_explicit_home(self):
        with TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "active_profile"), "w") as handle:
                handle.write("coder\n")
            with patch.object(wiz, "_default_hermes_home", return_value=tmp):
                with patch.dict(os.environ, {"HERMES_HOME": ""}, clear=False):
                    with self.assertRaises(ValueError):
                        wiz._hermes_home()

    def test_legacy_hermes_skill_path_is_not_left_stale(self):
        with TemporaryDirectory() as tmp:
            legacy = os.path.join(tmp, "skills", "smart-home", "wiz", "SKILL.md")
            os.makedirs(os.path.dirname(legacy))
            with open(legacy, "w") as handle:
                handle.write("---\nname: wiz\nversion: 1.3.0\n---\nold\n")
            with patch.dict(os.environ, {
                "HERMES_HOME": tmp,
                "WIZ_HERMES_SKILL_PATH": "",
            }, clear=False):
                self.assertEqual(wiz._hermes_skill_path(), legacy)
                self.assertIn(legacy, wiz._skill_paths(("hermes",)))

    def test_harness_parser_accepts_all_and_rejects_unknown(self):
        self.assertEqual(wiz._parse_harnesses("all"), wiz.UPDATE_HARNESSES)
        self.assertEqual(wiz._parse_harnesses("codex,claude,codex"), ("codex", "claude"))
        with self.assertRaises(ValueError):
            wiz._parse_harnesses("codex,unknown")

    def test_update_check_does_not_write_targets(self):
        remote_source = 'VERSION = "%s"\n' % AHEAD_VERSION
        remote_project = '[project]\nversion = "%s"\n' % AHEAD_VERSION
        remote_skill = "---\nname: wiz-lan-control\nversion: 1.4.0\n---\n"
        responses = {
            "wiz.py": remote_source,
            "pyproject.toml": remote_project,
            "skills/wiz/SKILL.md": remote_skill,
        }

        def fetch(url):
            return next(value for suffix, value in responses.items() if url.endswith("/" + suffix))

        with patch.object(wiz, "_fetch_url", side_effect=fetch):
            with patch.object(wiz, "_update_targets") as targets:
                result = wiz.cmd_update(["--check", "--ref", "release-test"])

        self.assertEqual(result, 0)
        targets.assert_not_called()

    def test_update_ref_rejects_url_injection(self):
        with patch.object(wiz, "_fetch_url") as fetch:
            result = wiz.cmd_update(["--ref", "../private"])

        self.assertNotEqual(result, 0)
        fetch.assert_not_called()

    def test_update_does_not_downgrade_cli(self):
        remote_source = 'VERSION = "0.4.0"\n'
        remote_project = '[project]\nversion = "0.4.0"\n'
        remote_skill = "---\nname: wiz-lan-control\nversion: 1.0.0\n---\n"
        responses = {
            "wiz.py": remote_source,
            "pyproject.toml": remote_project,
            "skills/wiz/SKILL.md": remote_skill,
        }

        def fetch(url):
            return next(value for suffix, value in responses.items() if url.endswith("/" + suffix))

        with patch.object(wiz, "_fetch_url", side_effect=fetch):
            with patch.object(wiz, "_update_targets") as targets:
                with patch.object(wiz, "_hermes_skill_path") as skill_path:
                    result = wiz.cmd_update(["--ref", "release-test"])

        self.assertEqual(result, 0)
        targets.assert_not_called()
        skill_path.assert_not_called()

    def test_update_uses_project_version_for_legacy_source(self):
        remote_source = "print('legacy wiz source')\n"
        remote_project = '[project]\nversion = "0.4.0"\n'
        remote_skill = "---\nname: wiz-lan-control\nversion: 1.0.0\n---\n"
        responses = {
            "wiz.py": remote_source,
            "pyproject.toml": remote_project,
            "skills/wiz/SKILL.md": remote_skill,
        }

        def fetch(url):
            return next(value for suffix, value in responses.items() if url.endswith("/" + suffix))

        with patch.object(wiz, "_fetch_url", side_effect=fetch):
            with patch.object(wiz, "_update_targets") as targets:
                result = wiz.cmd_update(["--ref", "release-test"])

        self.assertEqual(result, 0)
        targets.assert_not_called()

    def test_version_is_exposed_by_cli(self):
        with patch.object(sys, "argv", ["wiz", "--version"]):
            output = io.StringIO()
            with redirect_stdout(output):
                result = wiz.main()

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue().strip(), "wiz %s" % wiz.VERSION)


if __name__ == "__main__":
    unittest.main()
