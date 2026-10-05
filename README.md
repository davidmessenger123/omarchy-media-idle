# Media Idle Inhibit

An [Omarchy](https://omarchy.org/) shell plugin that keeps the screensaver,
auto-lock, and display sleep from kicking in while a trusted MPRIS media player
is playing.

Music counts. A Spotify track, a Jellyfin video, or a YouTube tab in a browser
will all hold the screen awake — the plugin only looks at *whether* a player is
playing, never at *what* it is playing.

## How it works

Omarchy's idle service exposes an IPC surface (`omarchy-shell idle ...`) backed
by a persistent "stay awake" flag. This plugin watches MPRIS players and flips
that flag while media is playing, so the screensaver and display sleep stay
disarmed and auto-lock never starts.

It never trusts the IPC call on faith. Every change is:

1. applied through `idle_control.py`, and
2. confirmed twice: the command must exit `0` **and** print the expected
   acknowledgement (`disabled` / `enabled`), and a follow-up
   `omarchy-shell idle status` must report the expected state.

Anything else is treated as a failure and retried with exponential backoff.

## Requirements

- Omarchy with `omarchy-shell` on `PATH` and an `idle` IPC target
  (`omarchy-shell idle status` must work).
- `OMARCHY_PATH` exported in your session environment. `idle_control.py`
  validates it — absolute, normalized, trusted owner, containing
  `shell/shell.qml` — because `omarchy-shell` resolves the QML payload it
  evaluates from it. Without it every call fails with
  `OMARCHY_PATH is not set` and the plugin silently retries forever.
- `python3` at `/usr/bin/python3`.

## Install

```bash
omarchy plugin add https://github.com/davidmessenger123/omarchy-media-idle.git --enable
```

This clones into `~/.config/omarchy/plugins/davidjm.media-idle` (the `id` comes
from `manifest.json`), validates the manifest, and enables it. From then on
`omarchy plugin update davidjm.media-idle` keeps it current.

Add `--yes` to skip the confirmation prompts when running non-interactively.

To install it without enabling immediately, drop `--enable` and run
`omarchy plugin enable davidjm.media-idle` later.

> Plugins run as unsandboxed code inside your long-lived `omarchy-shell`
> process. Review the source before enabling it.

## Uninstall

```bash
omarchy plugin remove davidjm.media-idle
```

Disabling the plugin runs a cleanup pass for any inhibition it owns, so the
screensaver is not left disarmed.

## Configuration

Both settings are environment variables read from the `omarchy-shell` process.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OMARCHY_MEDIA_IDLE_TRUSTED_PLAYERS` | see `Service.qml` | Comma-separated allowlist of players allowed to inhibit idle. |
| `OMARCHY_MEDIA_IDLE_MAX_MINUTES` | `30` | Lease length in minutes, clamped to `5`–`120`. |

Set them in `~/.config/hypr/hyprland.lua`, because the shell inherits
Hyprland's session environment:

```lua
hl.env("OMARCHY_MEDIA_IDLE_TRUSTED_PLAYERS", "spotify,vlc,mpv,jellyfin,cliamp")
hl.env("OMARCHY_MEDIA_IDLE_MAX_MINUTES", "20")
```

Then `omarchy restart shell`. Editing the file alone changes nothing until the
shell picks up the new environment.

### The lease

Inhibition is granted per player as a time-boxed **lease**, renewed for as long
as that player keeps playing. Stopping or pausing does **not** release it
immediately — the lease runs out first, so idle can stay inhibited for up to
`OMARCHY_MEDIA_IDLE_MAX_MINUTES` after playback ends. Lower it if that window
is too long for you.

## Adding a player

Only players on the allowlist can inhibit idle. An app that is not listed is
simply ignored — playback continues normally, the screen just still blanks.

### 1. Find the player's MPRIS identity

```bash
busctl --user list | grep -i mpris
```

```
org.mpris.MediaPlayer2.brave.instance799689   799689   brave      davidjm :1.2691
org.mpris.MediaPlayer2.cliamp                 868113   cliamp     davidjm :1.3046
```

Use the bus name — that is what the plugin matches on. Check whether it is
actually playing:

```bash
busctl --user introspect org.mpris.MediaPlayer2.cliamp \
  /org/mpris/MediaPlayer2 org.mpris.MediaPlayer2.Player | grep PlaybackStatus
```

If nothing appears, the app has no MPRIS interface and cannot be trusted by
this plugin at all.

### 2. Append it to the allowlist

Add the identity to `OMARCHY_MEDIA_IDLE_TRUSTED_PLAYERS` and restart the shell:

```lua
# defaults + your player
hl.env("OMARCHY_MEDIA_IDLE_TRUSTED_PLAYERS", "<defaults>,cliamp")
```

```bash
omarchy restart shell
```

The variable **replaces** the built-in list; it is not merged. Copy the default
string out of `Service.qml` (`trustedPlayerList`) if you want to keep the
defaults.

### 3. Confirm it matched

```bash
omarchy-shell media-idle status
```

```json
{"anyPlaying":true,"trustedPlaying":true,"idleDisabledByUs":true,
 "actualIdleEnabled":false,"wantIdleDisabled":true,"leaseUntil":0,
 "lastEvent":"action-exit code=0 status=0"}
```

- `anyPlaying` — some MPRIS player is playing.
- `trustedPlaying` — one of them is on the allowlist.
- `wantIdleDisabled` — the plugin wants idle suppressed.
- `idleDisabledByUs` — it actually did it, verified.
- `actualIdleEnabled: false` — the idle service agrees.

If `anyPlaying` is `true` but `trustedPlaying` is `false`, the identity you
configured does not match the player. Run `omarchy-shell media-idle reconcile`
to re-evaluate immediately instead of waiting for the 15s timer.

### How matching works

Entries are lowercased and compared against three properties of each player:
`dbusName`, `desktopEntry`, and `identity`. Two rules apply:

- **Bare entries** (`spotify`) match a whole token only. This is deliberate — a
  short entry cannot grant trust to an unrelated app that merely contains the
  fragment.
- **Entries containing a separator** (`io.github.celluloid_player.celluloid`,
  `jellyfin-media-player`) also match on the informative fragments inside
  them. The id is split on separators and camelCase boundaries, and noise words
  such as `org`, `github`, `player`, `media`, `desktop` are discarded, so
  `org.jellyfin.JellyfinDesktop` yields the token `jellyfin`.

| Configured entry | Matches |
| --- | --- |
| `spotify` | `org.mpris.MediaPlayer2.spotify` |
| `cliamp` | `org.mpris.MediaPlayer2.cliamp` |
| `celluloid` | `io.github.celluloid_player.celluloid`, `Celluloid` |
| `jellyfin` | `org.jellyfin.JellyfinDesktop`, `Jellyfin Desktop` |
| `brave` | `org.mpris.MediaPlayer2.brave.instance799689` |

The `org.mpris.MediaPlayer2.` prefix and trailing `.instanceNNNN` suffixes are
stripped before matching, so you never need to copy those.

## Ownership and cleanup

Omarchy's stay-awake flag is global and shared with the manual stay-awake
indicator, so the plugin is careful about ownership:

- It records an ownership marker (`$XDG_RUNTIME_DIR/omarchy-media-idle.owned`)
  before disabling idle, and **only re-enables idle it disabled itself**. If the
  marker is absent — it never disabled idle, or something removed it — the
  enable path reports `not-owned` and leaves the flag alone rather than
  clobbering someone else's stay-awake state.
- Every operation is serialised through an exclusive `flock` on
  `$XDG_RUNTIME_DIR/omarchy-media-idle.lock`.
- A detached watchdog process guards the disable: if `omarchy-shell` dies
  without cleaning up, the guard notices and restores idle rather than leaving
  the screen permanently awake.
- On unload the plugin runs a cleanup pass that re-enables idle and retries up
  to four times before giving up.

Runtime state lives in `$XDG_RUNTIME_DIR` and disappears at logout:

```
omarchy-media-idle.lock    serialisation lock
omarchy-media-idle.owned   ownership marker
omarchy-media-idle.guard   watchdog claim
omarchy-media-idle.log     diagnostics
```

## Troubleshooting

**Screen still blanks during playback.** Check `omarchy-shell media-idle status`
— if `trustedPlaying` is `false`, the player is not on the allowlist. If it is
`true` but `idleDisabledByUs` is `false`, look at `lastEvent` and the log.

**Everything reports a failure and retries.** Usually `OMARCHY_PATH`. Verify
with `echo $OMARCHY_PATH` in a session terminal and confirm
`omarchy-shell idle status` works there.

```bash
tail -f "$XDG_RUNTIME_DIR/omarchy-media-idle.log"
```

**Nothing happens at all.**

```bash
omarchy plugin list | grep media-idle     # installed and enabled?
omarchy-shell media-idle status           # is the service loaded?
```

**Recovery.** If idle is stuck disabled and you cannot wait for a lease to
expire:

```bash
omarchy-shell idle enable
```

Remove a stale ownership marker only if you are sure no shell is running the
plugin, otherwise the guard may fight you:

```bash
rm -f "$XDG_RUNTIME_DIR/omarchy-media-idle.owned"
```

## Development

```bash
python3 -m py_compile idle_control.py
python3 -m unittest discover -s tests
```

`tests/test_media_idle.py` covers the fixed child environment and command
timeouts, output bounds, per-player leases, stale-command retries and watchdogs,
guard claim ordering, cleanup retry-until-verified, and the rule that idle is
never re-enabled without ownership.

Layout:

- `Service.qml` — the Quickshell service: MPRIS watching, leases, IPC handler.
- `idle_control.py` — privileged-free helper that performs and verifies the
  `omarchy-shell idle` calls, owns the lock/marker/guard files, and is the only
  thing that mutates idle state.
- `manifest.json` — plugin manifest (`kinds: ["service"]`).

## License

MIT