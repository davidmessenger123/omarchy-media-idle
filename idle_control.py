#!/usr/bin/python3 -I

import ctypes
import fcntl
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time

LOCK_NAME = "omarchy-media-idle.lock"
LOG_NAME = "omarchy-media-idle.log"
OWNED_NAME = "omarchy-media-idle.owned"
GUARD_NAME = "omarchy-media-idle.guard"
COMMAND_TIMEOUT = 4.0
CLEANUP_LOCK_TIMEOUT = 15.0
CLEANUP_ATTEMPTS = 4
MAX_OUTPUT = 65536
SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/share/omarchy/bin"
GUARD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")


class ControlError(RuntimeError):
    pass


def _flags(value):
    return value | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def runtime_directory():
    path = os.environ.get("XDG_RUNTIME_DIR", "")
    if not isinstance(path, str) or not path or not os.path.isabs(path) or path == os.path.sep:
        raise ControlError("XDG_RUNTIME_DIR is unavailable")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ControlError("XDG_RUNTIME_DIR is invalid")
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ControlError("XDG_RUNTIME_DIR is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ControlError("XDG_RUNTIME_DIR is not private")
    return path


def lock_file_path():
    return os.path.join(runtime_directory(), LOCK_NAME)


def _diagnostic(message):
    text = str(message).replace("\n", " ")[:2048]
    print("omarchy-media-idle: " + text, file=sys.stderr, flush=True)
    try:
        directory = runtime_directory()
        path = os.path.join(directory, LOG_NAME)
        fd = os.open(path, _flags(os.O_WRONLY | os.O_CREAT | os.O_APPEND), 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
                raise ControlError("diagnostic log has unsafe metadata")
            os.fchmod(fd, 0o600)
            payload = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + text + "\n").encode("utf-8", "replace")
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short diagnostic write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
    except (ControlError, OSError):
        pass


def _open_lock(path, exclusive, create, wait_timeout=0.0):
    flags = _flags(os.O_RDWR | (os.O_CREAT if create else 0))
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ControlError("cannot open Media Idle lock") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > 4096):
            raise ControlError("Media Idle lock has unsafe metadata")
        os.fchmod(fd, 0o600)
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not wait_timeout:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            return fd
        deadline = time.monotonic() + wait_timeout
        while True:
            try:
                fcntl.flock(fd, operation | fcntl.LOCK_NB)
                return fd
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ControlError("timed out waiting for Media Idle lock") from exc
                time.sleep(0.05)
    except Exception:
        os.close(fd)
        raise


def _close_lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _owned_path():
    return os.path.join(runtime_directory(), OWNED_NAME)


def _set_owned(owned):
    path = _owned_path()
    if not owned:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_size > 32 or stat.S_IMODE(info.st_mode) & 0o077):
            raise ControlError("ownership marker has unsafe metadata")
        os.unlink(path)
        directory_fd = os.open(runtime_directory(), _flags(os.O_RDONLY | os.O_DIRECTORY))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return
    fd = os.open(path, _flags(os.O_WRONLY | os.O_CREAT), 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            raise ControlError("ownership marker has unsafe metadata")
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        payload = b"owned\n"
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ControlError("short ownership marker write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _is_owned():
    path = _owned_path()
    try:
        fd = os.open(path, _flags(os.O_RDONLY | os.O_NONBLOCK))
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ControlError("cannot read ownership marker safely") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_size > 32 or stat.S_IMODE(info.st_mode) & 0o077):
            raise ControlError("ownership marker has unsafe metadata")
        raw = os.read(fd, 33)
        return raw.strip() == b"owned"
    finally:
        os.close(fd)


def _guard_path():
    return os.path.join(runtime_directory(), GUARD_NAME)


def _claim_guard(guard_id):
    if not isinstance(guard_id, str) or not GUARD_ID_RE.fullmatch(guard_id):
        raise ControlError("guard identifier is invalid")
    path = _guard_path()
    lock_fd = _open_lock(lock_file_path(), exclusive=True, create=True, wait_timeout=CLEANUP_LOCK_TIMEOUT)
    try:
        fd = os.open(path, _flags(os.O_WRONLY | os.O_CREAT), 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
                raise ControlError("guard marker has unsafe metadata")
            os.fchmod(fd, 0o600)
            os.ftruncate(fd, 0)
            payload = (guard_id + "\n").encode("ascii")
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise ControlError("short guard marker write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        _close_lock(lock_fd)


def _guard_is_current(guard_id):
    path = _guard_path()
    try:
        fd = os.open(path, _flags(os.O_RDONLY | os.O_NONBLOCK))
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_size > 256 or stat.S_IMODE(info.st_mode) & 0o077):
            raise ControlError("guard marker has unsafe metadata")
        return os.read(fd, 257).decode("ascii", "strict").strip() == guard_id
    finally:
        os.close(fd)


def _process_identity(pid):
    try:
        with open("/proc/{0}/stat".format(pid), encoding="ascii") as handle:
            raw = handle.read()
    except (OSError, UnicodeError):
        return None
    end = raw.rfind(")")
    if end < 0:
        return None
    fields = raw[end + 1:].strip().split()
    return fields[19] if len(fields) > 19 else None


def _parent_alive(pid, identity):
    current = _process_identity(pid)
    return current is not None and identity is not None and current == identity


def _shell_command():
    resolved = shutil.which("omarchy-shell", path=SYSTEM_PATH)
    if not resolved:
        raise ControlError("omarchy-shell is unavailable")
    try:
        path = os.path.realpath(resolved)
        info = os.stat(path)
    except OSError as exc:
        raise ControlError("omarchy-shell cannot be verified") from exc
    if (not path.startswith("/usr/") and not path.startswith("/opt/")
            or not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise ControlError("omarchy-shell is not a trusted system executable")
    return path


def _child_setup(parent_pid):
    def setup():
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
                os._exit(127)
            if os.getppid() != parent_pid:
                os.kill(os.getpid(), signal.SIGTERM)
        except BaseException:
            os._exit(127)
    return setup


def _omarchy_path():
    # omarchy-shell loads its QML from $OMARCHY_PATH/shell, so the variable has
    # to reach it or every call fails with "OMARCHY_PATH is not set". It also
    # picks the payload quickshell evaluates, so validate rather than forward
    # blindly.
    value = os.environ.get("OMARCHY_PATH", "")
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        raise ControlError("OMARCHY_PATH is not set")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ControlError("OMARCHY_PATH is invalid")
    resolved = os.path.realpath(value)
    if os.path.normpath(value) != value or resolved != value:
        raise ControlError("OMARCHY_PATH is not normalized")
    try:
        info = os.stat(value)
    except OSError as exc:
        raise ControlError("OMARCHY_PATH is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()):
        raise ControlError("OMARCHY_PATH is not a trusted directory")
    if not os.path.isfile(os.path.join(value, "shell", "shell.qml")):
        raise ControlError("OMARCHY_PATH has no shell configuration")
    return value


def _command_environment():
    names = (
        "HOME",
        "USER",
        "LOGNAME",
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "DBUS_SESSION_BUS_ADDRESS",
        "WAYLAND_DISPLAY",
        "DISPLAY",
        "OMARCHY_SHELL_IPC_TIMEOUT",
    )
    environment = {name: os.environ[name] for name in names if name in os.environ}
    environment.update({
        "PATH": SYSTEM_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "OMARCHY_PATH": _omarchy_path(),
    })
    return environment


def _kill_process_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass


def _read_process_output(process, timeout):
    selector = selectors.DefaultSelector()
    buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
    for stream in buffers:
        selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + max(0.1, min(float(timeout), 120.0))
    overflow = False
    timed_out = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _kill_process_group(process)
                break
            for key, _ in selector.select(min(0.25, remaining)):
                target = buffers[key.fileobj]
                chunk = os.read(key.fileobj.fileno(), min(65536, MAX_OUTPUT - len(target) + 1))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target.extend(chunk)
                if len(target) > MAX_OUTPUT:
                    overflow = True
                    _kill_process_group(process)
                    break
            if overflow:
                break
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
    if timed_out:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.wait()
        return 124, bytes(buffers[process.stdout][:MAX_OUTPUT]), bytes(buffers[process.stderr][:MAX_OUTPUT])
    if overflow:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.wait()
        return 125, bytes(buffers[process.stdout][:MAX_OUTPUT]), bytes(buffers[process.stderr][:MAX_OUTPUT])
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        process.wait()
    return process.returncode, bytes(buffers[process.stdout][:MAX_OUTPUT]), bytes(buffers[process.stderr][:MAX_OUTPUT])


def _invoke_shell(arguments, lock_fd, timeout=COMMAND_TIMEOUT):
    command = [_shell_command()] + list(arguments)
    process = None
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_command_environment(),
            pass_fds=(lock_fd,),
            preexec_fn=_child_setup(os.getpid()),
            close_fds=True,
            start_new_session=True,
        )
        return _read_process_output(process, timeout)
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        if process is not None:
            _kill_process_group(process)
        return 126, b"", b""


def _decode(raw):
    try:
        return bytes(raw).decode("utf-8", "strict")
    except UnicodeError as exc:
        raise ControlError("omarchy-shell returned invalid UTF-8") from exc


def _reject_constant(token):
    raise ValueError(token)


def _status_enabled(lock_fd):
    code, stdout, stderr = _invoke_shell(["idle", "status"], lock_fd)
    diagnostic = _decode(stderr).strip()[:512]
    if code != 0:
        raise ControlError("idle status failed" + (": " + diagnostic if diagnostic else ""))
    try:
        value = json.loads(_decode(stdout), parse_constant=_reject_constant)
    except (UnicodeError, ValueError) as exc:
        raise ControlError("idle status returned malformed JSON") from exc
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        raise ControlError("idle status has an invalid shape")
    return value["enabled"]


def _perform_action(action, lock_fd):
    if action not in ("disable", "enable"):
        raise ControlError("unsupported idle action")
    expected_enabled = action == "enable"
    expected_ack = "enabled" if expected_enabled else "disabled"
    code, stdout, stderr = _invoke_shell(["idle", action], lock_fd)
    output = _decode(stdout).strip()
    diagnostic = _decode(stderr).strip()[:512]
    acknowledged = code == 0 and output == expected_ack
    status_enabled = _status_enabled(lock_fd)
    if acknowledged and status_enabled == expected_enabled:
        return expected_ack
    details = []
    if not acknowledged:
        details.append("action code=" + str(code) + " output=" + output[:128])
    if status_enabled != expected_enabled:
        details.append("status enabled=" + str(status_enabled).lower())
    if diagnostic:
        details.append("stderr=" + diagnostic)
    raise ControlError("idle action was not verified: " + "; ".join(details))


def _cleanup_locked(lock_fd):
    last_error = None
    for attempt in range(CLEANUP_ATTEMPTS):
        try:
            if _status_enabled(lock_fd):
                return "enabled"
            _perform_action("enable", lock_fd)
            return "enabled"
        except (ControlError, UnicodeError, ValueError) as exc:
            last_error = exc
            _diagnostic("cleanup enable attempt {0} failed: {1}".format(attempt + 1, exc))
            if attempt + 1 < CLEANUP_ATTEMPTS:
                time.sleep(min(1.0, 0.1 * (attempt + 1)))
    raise ControlError("cleanup could not verify idle enable: {0}".format(last_error))


def action_main(action):
    fd = _open_lock(lock_file_path(), exclusive=True, create=True)
    try:
        if action == "enable" and not _is_owned():
            result = "not-owned"
        else:
            if action == "disable":
                _set_owned(True)
            try:
                result = _perform_action(action, fd)
            except Exception:
                if action == "disable":
                    try:
                        if _status_enabled(fd):
                            _set_owned(False)
                    except (ControlError, OSError, ValueError):
                        pass
                raise
            if action == "enable":
                _set_owned(False)
    finally:
        _close_lock(fd)
    print(result)


def status_main():
    fd = _open_lock(lock_file_path(), exclusive=False, create=True)
    try:
        enabled = _status_enabled(fd)
    finally:
        _close_lock(fd)
    print(json.dumps({"enabled": enabled}, separators=(",", ":")))


def cleanup_main():
    fd = _open_lock(lock_file_path(), exclusive=True, create=True, wait_timeout=CLEANUP_LOCK_TIMEOUT)
    try:
        if not _is_owned():
            result = "not-owned"
        else:
            result = _cleanup_locked(fd)
            _set_owned(False)
    finally:
        _close_lock(fd)
    print(result)


def guard_main(parent_pid, guard_id):
    identity = _process_identity(parent_pid)
    if identity is None:
        raise ControlError("guard parent is unavailable")
    _claim_guard(guard_id)
    started = time.monotonic()
    while _parent_alive(parent_pid, identity) and _guard_is_current(guard_id):
        if _is_owned():
            started = time.monotonic()
        elif time.monotonic() - started >= 2.0:
            return
        time.sleep(0.1)
    if _parent_alive(parent_pid, identity) or not _guard_is_current(guard_id):
        return
    fd = _open_lock(lock_file_path(), exclusive=True, create=True, wait_timeout=CLEANUP_LOCK_TIMEOUT)
    try:
        if not _guard_is_current(guard_id):
            return
        if _is_owned():
            _cleanup_locked(fd)
            _set_owned(False)
    finally:
        _close_lock(fd)


def main(argv=None):
    values = sys.argv[1:] if argv is None else list(argv)
    if len(values) == 2 and values[0] == "action" and values[1] in ("disable", "enable"):
        operation = lambda: action_main(values[1])
    elif values == ["status"]:
        operation = status_main
    elif values == ["cleanup"]:
        operation = cleanup_main
    elif len(values) == 3 and values[0] == "guard" and values[1].isdigit() and int(values[1]) > 1:
        operation = lambda: guard_main(int(values[1]), values[2])
    else:
        print("usage: idle_control.py action disable|enable | status | cleanup | guard PID ID", file=sys.stderr)
        return 2
    try:
        operation()
        return 0
    except (ControlError, OSError, ValueError, subprocess.SubprocessError) as exc:
        _diagnostic(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
