import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Services.Mpris

// Media Idle Inhibit — while any MPRIS player is Playing, keep the Omarchy
// screensaver/lock/sleep chain disarmed. This mirrors how the packaged
// omarchy.media service watches MPRIS playback state, but instead of showing
// now-playing it flips the idle service's stay-awake flag so screensaver and
// display blank only run when nothing is actually playing.
Item {
  id: root

  // Injected by the shell's generic service loader.
  property var shell: null

  readonly property var players: Mpris.players ? Mpris.players.values : []

  // Last state successfully applied to the idle service.
  property bool idleDisabledByUs: false
  // Desired idle state derived from MPRIS playback.
  property bool wantIdleDisabled: false
  // True while an IPC toggle is in flight; we re-reconcile when it exits.
  property bool commandInFlight: false
  // State carried by the in-flight command (committed on a clean exit).
  property bool pendingDisable: false
  property int failures: 0
  property string lastCommand: ""
  property string lastEvent: "starting"
  property string lastEventAt: ""

  function nowIso() {
    return new Date().toISOString()
  }

  function logEvent(event, details) {
    var suffix = details === undefined || details === null || details === "" ? "" : " " + String(details)
    root.lastEventAt = nowIso()
    root.lastEvent = event + suffix
    console.log("omarchy media-idle " + root.lastEventAt + " " + root.lastEvent)
  }

  function anyPlayerPlaying() {
    var list = root.players
    for (var i = 0; i < list.length; i++) {
      if (list[i] && list[i].isPlaying) return true
    }
    return false
  }

  function scheduleRetry() {
    retryTimer.interval = Math.min(30000, 500 * Math.max(1, root.failures))
    retryTimer.restart()
  }

  // Desired idle state: disabled (stay awake) while anything plays. Compare
  // against what was successfully applied; if they differ, send one IPC toggle.
  // State changes during a toggle's flight are reconciled once it exits.
  function reconcile() {
    var playing = root.anyPlayerPlaying()
    root.wantIdleDisabled = playing

    if (root.commandInFlight) return
    if (root.wantIdleDisabled === root.idleDisabledByUs) return

    root.commandInFlight = true
    root.pendingDisable = root.wantIdleDisabled
    root.lastCommand = root.pendingDisable ? "disable" : "enable"
    logEvent("idle-" + root.lastCommand, "playing=" + playing)

    idleIpc.command = ["omarchy-shell", "-q", "idle", root.lastCommand]
    idleIpc.running = true
    commandWatchdog.restart()
  }

  Process {
    id: idleIpc
    onExited: function(exitCode, exitStatus) {
      commandWatchdog.stop()
      root.commandInFlight = false
      root.logEvent("ipc-exit", "code=" + exitCode + " status=" + exitStatus)
      if (exitCode !== 0) {
        // The idle service may not be ready yet; back off and retry. Do not
        // commit idleDisabledByUs, so the next reconcile re-issues the toggle.
        root.failures += 1
        root.scheduleRetry()
        return
      }
      root.failures = 0
      root.idleDisabledByUs = root.pendingDisable
      root.reconcile()
    }
  }

  // A hung omarchy-shell invocation must not wedge the reconcile loop forever.
  Timer {
    id: commandWatchdog
    interval: 5000
    repeat: false
    onTriggered: {
      if (!root.commandInFlight) return
      root.logEvent("ipc-timeout", root.lastCommand)
      root.commandInFlight = false
      idleIpc.running = false
      root.failures += 1
      root.scheduleRetry()
    }
  }

  Timer {
    id: retryTimer
    repeat: false
    onTriggered: root.reconcile()
  }

  // Mpris.players is a bindable singleton property; rooting it through
  // `players` fires onPlayersChanged when the set of players changes.
  onPlayersChanged: root.reconcile()

  Instantiator {
    model: root.players
    delegate: Connections {
      required property var modelData
      target: modelData
      function onIsPlayingChanged() { root.reconcile() }
    }
  }

  Component.onCompleted: {
    root.logEvent("service-ready", "players=" + root.players.length)
    root.reconcile()
  }

  IpcHandler {
    target: "media-idle"

    function status(): string {
      return JSON.stringify({
        anyPlaying: root.anyPlayerPlaying(),
        players: root.players.length,
        idleDisabledByUs: root.idleDisabledByUs,
        wantIdleDisabled: root.wantIdleDisabled,
        commandInFlight: root.commandInFlight,
        failures: root.failures,
        lastCommand: root.lastCommand,
        lastEvent: root.lastEvent,
        lastEventAt: root.lastEventAt
      })
    }

    function reconcile(): string {
      root.reconcile()
      return root.anyPlayerPlaying() ? "playing" : "idle-allowed"
    }
  }
}