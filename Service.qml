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

  // Effective state we last commanded the idle service into.
  property bool idleDisabledByUs: false
  // True while an IPC toggle is in flight; we re-reconcile when it exits.
  property bool commandInFlight: false
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

  // Desired idle state: disabled (stay awake) while anything plays. Compare
  // against what we last issued; if they differ, send one IPC toggle. State
  // changes during a toggle's flight are reconciled once it exits.
  function reconcile() {
    if (root.commandInFlight) return
    var playing = root.anyPlayerPlaying()
    var wantDisable = playing
    var wantCommand = wantDisable ? "disable" : "enable"

    if (wantDisable === root.idleDisabledByUs) return

    root.idleDisabledByUs = wantDisable
    root.commandInFlight = true
    root.lastCommand = wantCommand
    logEvent("idle-" + wantCommand, "playing=" + playing)

    idleIpc.command = ["bash", "-lc", "omarchy-shell -q idle " + wantCommand]
    idleIpc.running = true
  }

  Process {
    id: idleIpc
    onExited: function(exitCode, exitStatus) {
      root.commandInFlight = false
      root.logEvent("ipc-exit", "code=" + exitCode + " status=" + exitStatus)
      root.reconcile()
    }
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
        commandInFlight: root.commandInFlight,
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