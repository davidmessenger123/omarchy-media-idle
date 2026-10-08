import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

import player_select


class SelectionParsingTests(unittest.TestCase):
    def test_missing_or_blank_yields_empty_sets(self):
        for raw in ("", "   ", None):
            self.assertEqual(player_select.parse_selection(raw), {"trusted": [], "untrusted": []})

    def test_malformed_json_never_raises(self):
        for raw in ("{not json", "[1,2]", '"a string"', "42", "{", '{"trusted": '):
            self.assertEqual(player_select.parse_selection(raw), {"trusted": [], "untrusted": []})

    def test_non_list_values_are_ignored_rather_than_fatal(self):
        parsed = player_select.parse_selection('{"trusted": "moonfin", "untrusted": 3}')
        self.assertEqual(parsed, {"trusted": [], "untrusted": []})

    def test_only_usable_entries_survive(self):
        raw = json.dumps({
            "trusted": ["moonfin", "", "  ", "../escape", "a/b", "-leading", "x" * 129, 5, None, "vlc"],
            "untrusted": ["firefox"],
        })
        parsed = player_select.parse_selection(raw)
        self.assertEqual(parsed["trusted"], ["moonfin", "vlc"])
        self.assertEqual(parsed["untrusted"], ["firefox"])

    def test_oversized_file_is_refused_whole(self):
        # A long list under the byte ceiling is capped at the entry limit.
        raw = json.dumps({"trusted": ["entry%03d" % n for n in range(300)]})
        self.assertLess(len(raw), player_select.MAX_FILE_BYTES)
        self.assertEqual(len(player_select.parse_selection(raw)["trusted"]), 256)

        # A file past the byte ceiling is not parsed at all, so a truncated or
        # runaway file cannot smuggle entries in.
        huge = "x" * (player_select.MAX_FILE_BYTES + 1)
        self.assertEqual(player_select.parse_selection(huge), {"trusted": [], "untrusted": []})

    def test_entries_in_both_lists_resolve_to_untrusted(self):
        parsed = player_select.parse_selection('{"trusted": ["a", "b"], "untrusted": ["b"]}')
        self.assertEqual(parsed, {"trusted": ["a"], "untrusted": ["b"]})

    def test_duplicates_collapse(self):
        parsed = player_select.parse_selection('{"trusted": ["vlc", "vlc", "VLC"], "untrusted": []}')
        self.assertEqual(parsed["trusted"], ["vlc", "VLC"])


class SelectionStoreTests(unittest.TestCase):
    def test_read_missing_file_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                player_select.read_selection(os.path.join(directory, "nope.json")),
                {"trusted": [], "untrusted": []},
            )

    def test_read_refuses_symlink_and_non_regular_files(self):
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "real.json")
            link = os.path.join(directory, "link.json")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write('{"trusted": ["vlc"]}')

            os.symlink(target, link)
            self.assertEqual(player_select.read_selection(link), {"trusted": [], "untrusted": []})

            directory_path = os.path.join(directory, "adirectory.json")
            os.mkdir(directory_path)
            self.assertEqual(player_select.read_selection(directory_path), {"trusted": [], "untrusted": []})

    def test_write_is_private_and_round_trips(self):
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "config", "omarchy", "media-idle.json")
            player_select.write_selection(path, {"trusted": ["vlc"], "untrusted": ["firefox"]})

            info = os.stat(path)
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
            self.assertEqual(
                player_select.read_selection(path),
                {"trusted": ["vlc"], "untrusted": ["firefox"]},
            )

    def test_write_leaves_no_temporary_files_behind(self):
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "media-idle.json")
            for _ in range(5):
                player_select.write_selection(path, {"trusted": ["vlc"], "untrusted": []})
            self.assertEqual(sorted(os.listdir(home)), ["media-idle.json"])

    def test_write_refuses_to_follow_a_symlink(self):
        with tempfile.TemporaryDirectory() as home:
            victim = os.path.join(home, "victim.json")
            with open(victim, "w", encoding="utf-8") as handle:
                handle.write("original")
            link = os.path.join(home, "media-idle.json")
            os.symlink(victim, link)

            with self.assertRaises(player_select.SelectionError):
                player_select.write_selection(link, {"trusted": ["vlc"], "untrusted": []})

            with open(victim, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "original")

    def test_write_replaces_the_file_rather_than_truncating_in_place(self):
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "media-idle.json")
            player_select.write_selection(path, {"trusted": ["a"], "untrusted": []})
            first = os.stat(path).st_ino
            player_select.write_selection(path, {"trusted": ["b"], "untrusted": []})
            self.assertNotEqual(os.stat(path).st_ino, first)
            self.assertEqual(player_select.read_selection(path)["trusted"], ["b"])


class SetEntryTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._directory.name, "media-idle.json")

    def tearDown(self):
        self._directory.cleanup()

    def test_turning_on_adds_to_trusted(self):
        result = player_select.set_entry(self.path, "org.moonfin.linux", "on")
        self.assertEqual(result, {"trusted": ["org.moonfin.linux"], "untrusted": []})

    def test_turning_off_adds_to_untrusted(self):
        player_select.set_entry(self.path, "firefox", "off")
        self.assertEqual(player_select.read_selection(self.path), {"trusted": [], "untrusted": ["firefox"]})

    def test_a_repeated_toggle_never_lands_in_both_lists(self):
        for _ in range(3):
            player_select.set_entry(self.path, "vlc", "off")
            player_select.set_entry(self.path, "vlc", "on")
        selection = player_select.read_selection(self.path)
        self.assertEqual(selection, {"trusted": ["vlc"], "untrusted": []})

    def test_toggling_a_default_off_then_on_leaves_it_trusted(self):
        player_select.set_entry(self.path, "firefox", "off")
        player_select.set_entry(self.path, "firefox", "on")
        self.assertEqual(player_select.read_selection(self.path), {"trusted": ["firefox"], "untrusted": []})

    def test_other_entries_survive_a_toggle(self):
        player_select.set_entry(self.path, "vlc", "on")
        player_select.set_entry(self.path, "mpv", "on")
        player_select.set_entry(self.path, "vlc", "off")
        self.assertEqual(player_select.read_selection(self.path), {"trusted": ["mpv"], "untrusted": ["vlc"]})

    def test_unusable_ids_are_rejected(self):
        for bad in ("", "  ", "../escape", "a/b", "-x", "x" * 200, None, 5):
            with self.assertRaises(player_select.SelectionError):
                player_select.set_entry(self.path, bad, "on")
        self.assertEqual(player_select.read_selection(self.path), {"trusted": [], "untrusted": []})

    def test_unusable_switch_is_rejected_without_writing(self):
        with self.assertRaises(player_select.SelectionError):
            player_select.set_entry(self.path, "vlc", "maybe")
        self.assertFalse(os.path.exists(self.path))

    def test_a_corrupt_file_is_replaced_rather_than_appended_to(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        player_select.set_entry(self.path, "vlc", "on")
        self.assertEqual(player_select.read_selection(self.path), {"trusted": ["vlc"], "untrusted": []})


class DesktopScanTests(unittest.TestCase):
    def _apps(self, root):
        directory = os.path.join(root, "applications")
        os.makedirs(directory, exist_ok=True)
        return directory

    def _write(self, directory, name, body):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def _scan(self, data_home, data_dirs=None):
        environment = {
            "XDG_DATA_HOME": data_home,
            "XDG_DATA_DIRS": data_dirs or os.path.join(data_home, "..", "nowhere"),
            "HOME": os.path.join(data_home, "home"),
        }
        with mock.patch.dict(os.environ, environment):
            return player_select.scan_installed()

    def test_finds_players_and_names_them(self):
        with tempfile.TemporaryDirectory() as root:
            apps = self._apps(root)
            self._write(apps, "org.moonfin.linux.desktop", "[Desktop Entry]\nType=Application\nName=Moonfin\nCategories=AudioVideo;Video;\n")
            self.assertEqual(self._scan(root), [{"id": "org.moonfin.linux", "name": "Moonfin"}])

    def test_editor_and_capture_tools_are_left_out(self):
        with tempfile.TemporaryDirectory() as root:
            apps = self._apps(root)
            self._write(apps, "kdenlive.desktop", "[Desktop Entry]\nType=Application\nName=Kdenlive\nCategories=Qt;KDE;AudioVideo;AudioVideoEditing;\n")
            self._write(apps, "obs.desktop", "[Desktop Entry]\nType=Application\nName=OBS\nCategories=AudioVideo;Recorder;\n")
            self._write(apps, "qv4l2.desktop", "[Desktop Entry]\nType=Application\nName=qv4l2\nCategories=AudioVideo;\n")
            self.assertEqual(self._scan(root), [])

    def test_hidden_and_non_application_entries_are_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            apps = self._apps(root)
            self._write(apps, "hidden.desktop", "[Desktop Entry]\nType=Application\nName=Hidden\nNoDisplay=true\nCategories=AudioVideo;Player;\n")
            self._write(apps, "nohidden.desktop", "[Desktop Entry]\nType=Application\nName=NoHidden\nHidden=true\nCategories=AudioVideo;Player;\n")
            self._write(apps, "link.desktop", "[Desktop Entry]\nType=Link\nName=Link\nCategories=AudioVideo;Player;\n")
            self._write(apps, "othergroup.desktop", "[Desktop Action X]\nName=Action\nCategories=AudioVideo;Player;\n")
            self.assertEqual(self._scan(root), [])

    def test_name_falls_back_to_the_desktop_id(self):
        with tempfile.TemporaryDirectory() as root:
            apps = self._apps(root)
            self._write(apps, "anon.desktop", "[Desktop Entry]\nType=Application\nCategories=AudioVideo;Player;\n")
            self.assertEqual(self._scan(root), [{"id": "anon", "name": "anon"}])

    def test_categories_may_appear_before_the_name(self):
        with tempfile.TemporaryDirectory() as root:
            apps = self._apps(root)
            self._write(apps, "reordered.desktop", "[Desktop Entry]\nCategories=AudioVideo;Player;\nName=Reordered\n")
            self.assertEqual(self._scan(root), [{"id": "reordered", "name": "Reordered"}])

    def test_results_are_sorted_by_name_case_insensitively(self):
        with tempfile.TemporaryDirectory() as root:
            apps = self._apps(root)
            for name in ("zeta", "alpha", "Mid"):
                self._write(apps, name + ".desktop", "[Desktop Entry]\nType=Application\nName=" + name + "\nCategories=AudioVideo;Player;\n")
            self.assertEqual([c["name"] for c in self._scan(root)], ["alpha", "Mid", "zeta"])

    def test_a_user_desktop_file_overrides_the_system_one(self):
        with tempfile.TemporaryDirectory() as root:
            user = self._apps(os.path.join(root, "user"))
            system = self._apps(os.path.join(root, "system"))
            self._write(user, "dup.desktop", "[Desktop Entry]\nType=Application\nName=User Copy\nCategories=AudioVideo;Player;\n")
            self._write(system, "dup.desktop", "[Desktop Entry]\nType=Application\nName=System Copy\nCategories=AudioVideo;Player;\n")
            self.assertEqual(self._scan(os.path.join(root, "user"), os.path.join(root, "system")), [{"id": "dup", "name": "User Copy"}])

    def test_a_non_player_system_copy_does_not_hide_the_user_one(self):
        # The skip is recorded against the id only when it actually parsed, so a
        # system file that is not a player must not block the user's entry.
        with tempfile.TemporaryDirectory() as root:
            user = self._apps(os.path.join(root, "user"))
            system = self._apps(os.path.join(root, "system"))
            self._write(user, "dup.desktop", "[Desktop Entry]\nType=Application\nName=User Copy\nCategories=AudioVideo;Player;\n")
            self._write(system, "dup.desktop", "[Desktop Entry]\nType=Application\nName=System Copy\nCategories=AudioVideo;Recorder;\n")
            self.assertEqual(self._scan(os.path.join(root, "user"), os.path.join(root, "system")), [{"id": "dup", "name": "User Copy"}])

    def test_missing_directories_are_not_an_error(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(self._scan(root), [])


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.config = os.path.join(self._directory.name, "media-idle.json")

    def tearDown(self):
        self._directory.cleanup()

    def _run(self, *args):
        environment = dict(os.environ, OMARCHY_MEDIA_IDLE_CONFIG=self.config)
        return subprocess.run(
            [sys.executable, "-I", str(Path(player_select.__file__)), *args],
            capture_output=True, text=True, env=environment,
        )

    def test_state_on_a_missing_file_is_empty_json(self):
        result = self._run("state")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {"trusted": [], "untrusted": []})

    def test_set_round_trips_through_the_command_line(self):
        self.assertEqual(self._run("set", "moonfin", "on").returncode, 0)
        self.assertEqual(json.loads(self._run("state").stdout), {"trusted": ["moonfin"], "untrusted": []})

    def test_list_emits_candidates(self):
        result = self._run("list")
        self.assertEqual(result.returncode, 0)
        self.assertIn("candidates", json.loads(result.stdout))

    def test_unknown_arguments_exit_two_without_touching_the_file(self):
        for args in ((), ("nonsense",), ("set", "moonfin"), ("set", "moonfin", "on", "extra"), ("state", "extra")):
            self.assertEqual(self._run(*args).returncode, 2, args)
        self.assertFalse(os.path.exists(self.config))

    def test_bad_id_exits_one_with_a_diagnostic(self):
        result = self._run("set", "../escape", "on")
        self.assertEqual(result.returncode, 1)
        self.assertIn("omarchy-media-idle:", result.stderr)
        self.assertFalse(os.path.exists(self.config))

    def test_relative_override_is_refused(self):
        environment = dict(os.environ, OMARCHY_MEDIA_IDLE_CONFIG="relative.json")
        result = subprocess.run(
            [sys.executable, "-I", str(Path(player_select.__file__)), "state"],
            capture_output=True, text=True, env=environment,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("absolute", result.stderr)


if __name__ == "__main__":
    unittest.main()