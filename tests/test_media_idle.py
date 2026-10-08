import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

import idle_control


class MediaIdleContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).parents[1] / "Service.qml").read_text(encoding="utf-8")

    def test_shell_environment_uses_fixed_system_path(self):
        with tempfile.TemporaryDirectory() as runtime, fake_omarchy_path() as omarchy:
            os.chmod(runtime, 0o700)
            with runtime_environment(runtime):
                lock_fd = idle_control._open_lock(idle_control.lock_file_path(), exclusive=True, create=True)
                process = SimpleNamespace(pid=os.getpid(), returncode=0, wait=mock.Mock(), kill=mock.Mock())
                try:
                    with mock.patch.object(idle_control, "_shell_command", return_value="/usr/bin/omarchy-shell"), mock.patch.object(idle_control.subprocess, "Popen", return_value=process) as popen, mock.patch.object(idle_control, "_read_process_output", return_value=(0, b"{}", b"")), mock.patch.dict(os.environ, {"PYTHONPATH": "/tmp/untrusted"}):
                        code, stdout, stderr = idle_control._invoke_shell(["idle", "status"], lock_fd)
                finally:
                    idle_control._close_lock(lock_fd)
                self.assertEqual((code, stdout, stderr), (0, b"{}", b""))
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(environment["PATH"], idle_control.SYSTEM_PATH)
        self.assertEqual(environment["OMARCHY_PATH"], omarchy)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("LD_PRELOAD", environment)

    def test_shell_output_is_bounded_before_buffering(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 70000)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        code, stdout, stderr = idle_control._read_process_output(process, 2)
        self.assertEqual(code, 125)
        self.assertLessEqual(len(stdout), idle_control.MAX_OUTPUT)
        self.assertLessEqual(len(stderr), idle_control.MAX_OUTPUT)

        self.assertNotIn('"omarchy-shell", "-q"', self.source)
        self.assertIn('parseAck', self.source)
        self.assertIn('parseStatus', self.source)
        self.assertIn('"idle_control.py", "status"', self.source)
        self.assertIn('"idle_control.py", "action", root.lastCommand', self.source)

    def test_untrusted_players_are_bounded_and_cleanup_is_owned(self):
        self.assertIn('trustedPlayer', self.source)
        self.assertIn('maxLeaseMs', self.source)
        self.assertIn('Component.onDestruction', self.source)
        self.assertIn('idleDisabledByUs', self.source)
        self.assertIn('root.idleDisabledByUs || root.disableAttempted', self.source)
        self.assertIn('"idle_control.py", "cleanup"', self.source)
        self.assertIn('"idle_control.py", "guard", String(parentPid), guardId', self.source)

    def test_stale_commands_retry_and_per_command_watchdogs_are_guarded(self):
        self.assertIn('commandGeneration !== root.generation', self.source)
        self.assertIn('scheduleRetry', self.source)
        self.assertIn('Math.pow(2, root.retryAttempt - 1)', self.source)
        self.assertIn('id: actionWatchdog', self.source)
        self.assertIn('id: statusWatchdog', self.source)
        self.assertIn('statusWatchdog.interval = 5000', self.source)
        self.assertIn('actionWatchdog.interval = 5000', self.source)
        self.assertIn('root.actionTimedOut()', self.source)
        self.assertIn('root.statusTimedOut()', self.source)
        self.assertIn('exitCode !== 0 || !acknowledged', self.source)

    def test_desired_state_is_tracked_while_commands_are_in_flight(self):
        self.assertIn('root.wantIdleDisabled = root.inhibitionRequested()', self.source)
        self.assertIn('if (root.commandInFlight) return', self.source)
        self.assertIn('wantIdleDisabled: root.wantIdleDisabled', self.source)
        self.assertIn('failures: root.retryAttempt', self.source)

    def test_uncertain_disable_uses_owned_cleanup_and_multiplayer_leases(self):
        self.assertIn("root.beginCommand(false, true)", self.source)
        self.assertIn("parseCleanupAck", self.source)
        self.assertIn('"idle_control.py", "cleanup"', self.source)
        self.assertIn("trustedPlayingKeys", self.source)
        self.assertIn("property var playerLeases: []", self.source)
        self.assertIn("retained.push({ key: current[i], until: now + root.maxLeaseMs })", self.source)

    def test_guard_claim_waits_for_the_disable_lock(self):
        with tempfile.TemporaryDirectory() as runtime:
            os.chmod(runtime, 0o700)
            with runtime_environment(runtime):
                held = idle_control._open_lock(idle_control.lock_file_path(), exclusive=True, create=True)
                claimed = threading.Event()

                def claim():
                    idle_control._claim_guard("guard-new-1")
                    claimed.set()

                thread = threading.Thread(target=claim)
                thread.start()
                time.sleep(0.1)
                self.assertFalse(claimed.is_set())
                idle_control._close_lock(held)
                thread.join(timeout=3)
                self.assertTrue(claimed.is_set())
                self.assertFalse(thread.is_alive())
                self.assertTrue(idle_control._guard_is_current("guard-new-1"))

    def test_superseded_guard_cannot_clean_after_lock_acquisition(self):
        with tempfile.TemporaryDirectory() as runtime:
            os.chmod(runtime, 0o700)
            with runtime_environment(runtime):
                with mock.patch.object(idle_control, "_claim_guard"), mock.patch.object(idle_control, "_parent_alive", return_value=False), mock.patch.object(idle_control, "_guard_is_current", side_effect=(True, False)), mock.patch.object(idle_control, "_cleanup_locked") as cleanup:
                    idle_control.guard_main(os.getpid(), "guard-old-1")
                cleanup.assert_not_called()

    def test_cleanup_waits_for_inflight_disable_lock(self):
        with tempfile.TemporaryDirectory() as runtime:
            os.chmod(runtime, 0o700)
            with runtime_environment(runtime):
                held = idle_control._open_lock(idle_control.lock_file_path(), exclusive=True, create=True)
                acquired = threading.Event()
                finished = threading.Event()

                def cleanup():
                    lock_fd = idle_control._open_lock(idle_control.lock_file_path(), exclusive=True, create=True, wait_timeout=2.0)
                    try:
                        acquired.set()
                        with mock.patch.object(idle_control, "_cleanup_locked", return_value="enabled"):
                            idle_control._cleanup_locked(lock_fd)
                    finally:
                        idle_control._close_lock(lock_fd)
                        finished.set()

                thread = threading.Thread(target=cleanup)
                thread.start()
                time.sleep(0.1)
                self.assertFalse(acquired.is_set())
                idle_control._close_lock(held)
                thread.join(timeout=3)
                self.assertTrue(finished.is_set())
                self.assertFalse(thread.is_alive())

    def test_cleanup_retries_until_enable_is_verified(self):
        with tempfile.TemporaryDirectory() as runtime:
            os.chmod(runtime, 0o700)
            with runtime_environment(runtime):
                lock_fd = idle_control._open_lock(idle_control.lock_file_path(), exclusive=True, create=True)
                responses = [
                    (0, b'{"enabled":false}\n', b""),
                    (0, b"enabled\n", b""),
                    (0, b'{"enabled":false}\n', b""),
                    (0, b'{"enabled":false}\n', b""),
                    (0, b"enabled\n", b""),
                    (0, b'{"enabled":true}\n', b""),
                ]
                try:
                    with mock.patch.object(idle_control, "_invoke_shell", side_effect=responses) as invoke, mock.patch.object(idle_control.time, "sleep"):
                        self.assertEqual(idle_control._cleanup_locked(lock_fd), "enabled")
                    self.assertEqual(invoke.call_count, len(responses))
                finally:
                    idle_control._close_lock(lock_fd)

    def test_disable_intent_marker_is_cleared_only_after_enable_verification(self):
        with tempfile.TemporaryDirectory() as runtime:
            os.chmod(runtime, 0o700)
            with runtime_environment(runtime):
                with mock.patch.object(idle_control, "_perform_action", return_value="disabled"):
                    idle_control.action_main("disable")
                self.assertTrue(idle_control._is_owned())
                self.assertEqual(os.stat(idle_control._owned_path()).st_mode & 0o777, 0o600)
                with mock.patch.object(idle_control, "_perform_action", return_value="enabled"):
                    idle_control.action_main("enable")
                self.assertFalse(idle_control._is_owned())
                with mock.patch.object(idle_control, "_cleanup_locked") as cleanup:
                    idle_control.cleanup_main()
                cleanup.assert_not_called()

    def test_enable_action_refuses_unowned_idle_state(self):
        with tempfile.TemporaryDirectory() as runtime:
            os.chmod(runtime, 0o700)
            with runtime_environment(runtime), mock.patch.object(idle_control, "_perform_action") as perform, redirect_stdout(io.StringIO()) as output:
                idle_control.action_main("enable")
            self.assertEqual(output.getvalue().strip(), "not-owned")
            perform.assert_not_called()

    def test_lock_and_diagnostics_are_private_per_user_files(self):
        with tempfile.TemporaryDirectory() as runtime:
            os.chmod(runtime, 0o700)
            victim = os.path.join(runtime, "victim")
            with open(victim, "w", encoding="utf-8") as handle:
                handle.write("unchanged")
            with runtime_environment(runtime):
                os.symlink(victim, idle_control.lock_file_path())
                with self.assertRaises(idle_control.ControlError):
                    idle_control._open_lock(idle_control.lock_file_path(), exclusive=True, create=True)
                with open(victim, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "unchanged")
                with redirect_stderr(io.StringIO()):
                    idle_control._diagnostic("verified cleanup diagnostic")
                log_path = os.path.join(runtime, idle_control.LOG_NAME)
                self.assertEqual(os.stat(log_path).st_mode & 0o777, 0o600)
                with open(log_path, encoding="utf-8") as handle:
                    self.assertIn("verified cleanup diagnostic", handle.read())


@contextmanager
def fake_omarchy_path():
    # _command_environment validates OMARCHY_PATH before forwarding it, so the
    # test has to supply a directory that passes the same checks. macOS resolves
    # /tmp through a symlink, so realpath it to keep the normalization check happy.
    with tempfile.TemporaryDirectory() as root:
        omarchy = os.path.realpath(root)
        shell = Path(omarchy) / "shell"
        shell.mkdir(mode=0o700)
        (shell / "shell.qml").write_text("import Quickshell\n", encoding="utf-8")
        previous = os.environ.get("OMARCHY_PATH")
        os.environ["OMARCHY_PATH"] = omarchy
        try:
            yield omarchy
        finally:
            if previous is None:
                os.environ.pop("OMARCHY_PATH", None)
            else:
                os.environ["OMARCHY_PATH"] = previous


@contextmanager
def runtime_environment(path):
    old_value = os.environ.get("XDG_RUNTIME_DIR")
    os.environ["XDG_RUNTIME_DIR"] = path
    try:
        yield
    finally:
        if old_value is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = old_value


class PickerWiringTests(unittest.TestCase):
    """The picker and the service must agree, and must both be driven by the
    selection file rather than by an edit to a shell config."""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parents[1]
        cls.service = (root / "Service.qml").read_text(encoding="utf-8")
        cls.players = (root / "Players.qml").read_text(encoding="utf-8")
        cls.shared = (root / "TrustedPlayers.js").read_text(encoding="utf-8")
        cls.manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))

    def test_matching_is_shared_not_duplicated(self):
        # If either side carried its own copy, the picker could tick a box the
        # service then disagrees with.
        self.assertIn('import "TrustedPlayers.js" as TrustedPlayers', self.service)
        self.assertIn('import "TrustedPlayers.js" as TrustedPlayers', self.players)
        self.assertIn("TrustedPlayers.isTrustedPlayer(player, root.allowTokens)", self.service)

        self.assertNotIn("function tokenSegments", self.service)
        self.assertNotIn("function playerTokens", self.service)
        self.assertNotIn("function allowedTokens", self.service)
        self.assertIn("function tokenSegments", self.shared)
        self.assertIn("function playerTokens", self.shared)
        self.assertIn("function allowedTokens", self.shared)

    def test_both_sides_resolve_the_same_allowlist(self):
        for source in (self.service, self.players):
            self.assertIn("TrustedPlayers.effective(root.baseTrustedPlayerList, root.selection)", source)
            self.assertIn('Quickshell.env("OMARCHY_MEDIA_IDLE_TRUSTED_PLAYERS")', source)
            self.assertIn("TrustedPlayers.DEFAULT_PLAYERS", source)

    def test_service_watches_the_selection_file_for_changes(self):
        # A picker toggle that needed a shell restart would not be a picker.
        self.assertIn("applySelection", self.service)
        self.assertIn("watchChanges: true", self.service)
        self.assertIn("atomicWrites: true", self.service)
        self.assertIn("onFileChanged: reload()", self.service)
        self.assertIn("root.notePlayerChange()", self.service)
        self.assertIn("/omarchy/media-idle.json", self.service)

    def test_picker_reads_and_writes_through_the_helper(self):
        self.assertIn("player_select.py", self.players)
        self.assertIn('root.helper, "state"', self.players)
        self.assertIn('root.helper, "list"', self.players)
        self.assertIn('root.helper, "set", setProc.pendingId, setProc.pendingOn', self.players)
        # The picker must not invent its own resolution of the file.
        self.assertNotIn("FileView", self.players)

    def test_picker_takes_checkbox_state_from_the_shared_matcher(self):
        self.assertIn("TrustedPlayers.isCandidateTrusted", self.players)
        self.assertIn("TrustedPlayers.candidateIdForPlayer", self.players)

    def test_picker_implements_the_overlay_lifecycle(self):
        # The shell summons by id, reads `opened` for toggle state, and calls
        # these hooks; without them the overlay never appears.
        self.assertIn("property bool opened: false", self.players)
        self.assertIn("function open(payload)", self.players)
        self.assertIn("function close()", self.players)
        self.assertIn("PanelWindow", self.players)
        self.assertIn("WlrLayershell.keyboardFocus", self.players)

    def test_manifest_exposes_the_overlay_next_to_the_service(self):
        self.assertIn("overlay", self.manifest["kinds"])
        self.assertIn("service", self.manifest["kinds"])
        self.assertEqual(self.manifest["entryPoints"]["overlay"], "Players.qml")
        self.assertEqual(self.manifest["entryPoints"]["service"], "Service.qml")

    def test_selection_parsing_is_total_in_both_implementations(self):
        # Service.qml and player_select.py each parse this file. Neither may
        # throw on a malformed one: a typo must not take the service down.
        self.assertIn("function parseSelection", self.shared)
        self.assertIn("catch (error)", self.shared)
        self.assertIn("except (ValueError, UnicodeError)", Path(__file__).parents[1].joinpath("player_select.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
