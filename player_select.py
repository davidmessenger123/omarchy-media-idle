#!/usr/bin/python3 -I

"""Trusted-player selection store for Media Idle Inhibit.

Owns ~/.config/omarchy/media-idle.json, the file behind the picker overlay. It
holds two disjoint sets: "trusted" adds apps to the allowlist, "untrusted"
removes them, so a built-in default can be turned off without editing the
built-in list. idle_control.py stays the only thing that touches idle state;
this only reads and writes the allowlist selection.

Commands:
  state              print the current selection as JSON
  list               print installed media-player candidates as JSON
  set <id> <on|off>  move one entry and print the resulting selection

Reads are deliberately total: a missing, unreadable, or malformed file yields
empty sets rather than an error, so a corrupted file degrades to the built-in
allowlist instead of disabling the plugin or trusting everything.
"""

import json
import os
import re
import stat
import sys
import tempfile

CONFIG_ENV = "OMARCHY_MEDIA_IDLE_CONFIG"
MAX_FILE_BYTES = 65536
MAX_ENTRIES = 256
LIST_KEYS = ("trusted", "untrusted")

# The allowlist is matched token by token, so an entry only needs to carry the
# desktop id a user can read off a .desktop file or a bus name. Anything that
# cannot be one of those is a mistake or an attempt to smuggle a separator.
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")

# Categories that mean "this is something that plays media". AudioVideo alone
# would sweep in every screen recorder and screenshot tool.
REQUIRED_CATEGORY = "AudioVideo"
MEDIA_CATEGORIES = ("Player", "Video", "Audio")


class SelectionError(RuntimeError):
    pass


def config_path():
    override = os.environ.get(CONFIG_ENV, "")
    if override:
        if not os.path.isabs(override):
            raise SelectionError(CONFIG_ENV + " must be an absolute path")
        return os.path.realpath(override)
    home = os.environ.get("HOME", "")
    if not home or not os.path.isabs(home):
        raise SelectionError("HOME is unavailable")
    return os.path.join(home, ".config", "omarchy", "media-idle.json")


def _empty():
    return {key: [] for key in LIST_KEYS}


def _normalise_entry(value):
    if not isinstance(value, str):
        return ""
    entry = value.strip()
    if not entry or len(entry) > 128 or not ID_RE.fullmatch(entry):
        return ""
    return entry


def parse_selection(raw):
    """Turn file contents into two disjoint, ordered, deduplicated lists."""
    if not isinstance(raw, str) or not raw.strip():
        return _empty()
    if len(raw.encode("utf-8", "replace")) > MAX_FILE_BYTES:
        return _empty()
    try:
        value = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, UnicodeError):
        return _empty()
    if not isinstance(value, dict):
        return _empty()

    selection = _empty()
    for key in LIST_KEYS:
        items = value.get(key, [])
        if not isinstance(items, list):
            continue
        for item in items[:MAX_ENTRIES]:
            entry = _normalise_entry(item)
            if entry and entry not in selection[key]:
                selection[key].append(entry)

    # Untrusted wins: an entry in both lists is resolved the same way whichever
    # set a future reader forgets to check.
    selection["trusted"] = [e for e in selection["trusted"] if e not in selection["untrusted"]]
    return selection


def _reject_constant(token):
    raise ValueError(token)


def read_selection(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return _empty()
    except OSError:
        return _empty()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_size > MAX_FILE_BYTES:
        return _empty()
    try:
        with open(path, encoding="utf-8") as handle:
            return parse_selection(handle.read(MAX_FILE_BYTES + 1))
    except (OSError, UnicodeError):
        return _empty()


def _fsync_directory(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_selection(path, selection):
    """Replace the selection atomically so a crash cannot leave it truncated."""
    payload = json.dumps(selection, indent=2, sort_keys=True) + "\n"
    if len(payload.encode("utf-8")) > MAX_FILE_BYTES:
        raise SelectionError("selection is too large to write")
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise SelectionError("cannot create the Media Idle config directory") from exc
    try:
        # Refuse to write through a symlink: the overlay replaced the file it
        # was shown, and following a link would silently retarget the write.
        if os.path.islink(path):
            raise SelectionError("Media Idle config is a symlink")
        handle, temporary = tempfile.mkstemp(prefix=".media-idle.", dir=directory)
    except OSError as exc:
        raise SelectionError("cannot create a temporary Media Idle config") from exc
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as exc:
        raise SelectionError("cannot write the Media Idle config") from exc
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    _fsync_directory(directory)


def set_entry(path, entry, on):
    entry = _normalise_entry(entry)
    if not entry:
        raise SelectionError("player id is not a usable allowlist entry")
    if on not in (True, False, "on", "off", "true", "false"):
        raise SelectionError("expected 'on' or 'off'")
    enabled = on in (True, "on", "true")

    selection = read_selection(path)
    for key in LIST_KEYS:
        selection[key] = [existing for existing in selection[key] if existing != entry]
    target = "trusted" if enabled else "untrusted"
    selection[target].append(entry)
    selection[target].sort()
    for key in LIST_KEYS:
        selection[key] = sorted(set(selection[key]))
    write_selection(path, selection)
    return selection


def _parse_desktop(path, desktop_id):
    """Read the handful of keys the picker shows out of a .desktop file."""
    name = ""
    categories = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line != "[Desktop Entry]":
                break
            key, separator, value = line.partition("=")
            if not separator:
                continue
            key = key.strip()
            value = value.strip()
            if key == "Name" and not name:
                name = value
            elif key == "Categories":
                categories = [item.strip() for item in value.split(";")]
            elif key in ("NoDisplay", "Hidden") and value.lower() == "true":
                return None
            elif key == "Type" and value != "Application":
                return None
    if REQUIRED_CATEGORY not in categories:
        return None
    if not any(category in MEDIA_CATEGORIES for category in categories):
        return None
    return {"id": desktop_id, "name": name or desktop_id}


def scan_installed():
    """Every installed app that advertises itself as a media player."""
    directories = []
    data_home = os.environ.get("XDG_DATA_HOME") or ""
    if data_home and os.path.isabs(data_home):
        directories.append(os.path.join(data_home, "applications"))
    home = os.environ.get("HOME", "")
    if home and os.path.isabs(home):
        directories.append(os.path.join(home, ".local", "share", "applications"))
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    for entry in data_dirs.split(":"):
        if entry and os.path.isabs(entry):
            directories.append(os.path.join(entry, "applications"))

    candidates = {}
    for directory in directories:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for filename in names:
            if not filename.endswith(".desktop"):
                continue
            desktop_id = filename[: -len(".desktop")]
            if desktop_id in candidates:
                continue
            if not ID_RE.fullmatch(desktop_id):
                continue
            try:
                parsed = _parse_desktop(os.path.join(directory, filename), desktop_id)
            except OSError:
                continue
            if parsed is not None:
                candidates[desktop_id] = parsed

    ordered = sorted(candidates.values(), key=lambda item: (item["name"].lower(), item["id"]))
    return ordered[:MAX_ENTRIES]


def _emit(value):
    print(json.dumps(value, separators=(",", ":")))


def main(argv=None):
    values = sys.argv[1:] if argv is None else list(argv)
    command = values[0] if values else ""
    try:
        if command == "state" and len(values) == 1:
            _emit(read_selection(config_path()))
        elif command == "list" and len(values) == 1:
            _emit({"candidates": scan_installed()})
        elif command == "set" and len(values) == 3:
            _emit(set_entry(config_path(), values[1], values[2].lower()))
        else:
            print("usage: player_select.py state | list | set ID on|off", file=sys.stderr)
            return 2
    except SelectionError as exc:
        print("omarchy-media-idle: " + str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print("omarchy-media-idle: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())