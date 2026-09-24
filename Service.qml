pragma ComponentBehavior: Bound

import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Services.Mpris

// Media Idle Inhibit — while a trusted MPRIS player is Playing, keep the
// Omarchy screensaver/lock/sleep chain disarmed for a bounded lease. This
// mirrors how the packaged omarchy.media service watches MPRIS playback state,
// but instead of showing now-playing it flips the idle service's stay-awake
// flag so screensaver and display blank resume when the lease ends.
Item {
  id: root

  property var shell: null
  readonly property string pluginDir: {
    var path = String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "")
    return path.charAt(path.length - 1) === "/" ? path : path + "/"
  }

  readonly property var players: Mpris.players ? Mpris.players.values : []

  property bool idleDisabledByUs: false
  property bool wantIdleDisabled: false
  property bool actualIdleEnabled: true
  property bool actualStateKnown: false
  property bool pendingDisable: false
  property bool commandInFlight: false
  property string commandPhase: ""
  property int generation: 0
  property int commandGeneration: -1
  property int guardSequence: 0
  property int retryAttempt: 0
  property real leaseUntil: 0
  property string leasePlayerKey: ""
  property var playerLeases: []
  property bool disableAttempted: false
  property bool shuttingDown: false
  property bool cleanupIssued: false
  property string lastCommand: ""
  property string lastAck: ""
  property string actionOutput: ""
  property string statusOutput: ""
  property string commandErrorOutput: ""
  property string lastEvent: "starting"
  property string lastEventAt: ""

  readonly property string trustedPlayerList: {
    var configured = String(Quickshell.env("OMARCHY_MEDIA_IDLE_TRUSTED_PLAYERS") || "").slice(0, 4096)
    return configured || "spotify,spotifyd,vlc,mpv,celluloid,io.github.celluloid_player.celluloid,jellyfin,jellyfin-media-player,jellyfin-mpv,youtube,youtube-music,firefox,chromium,chromium-browser,google-chrome,brave,brave-browser,plex,plexamp,mpvpaper,harmonoid,strawberry,musicplayer"
  }

  readonly property int maxLeaseMs: configuredLeaseMs()

  function configuredLeaseMs() {
    var value = Number(Quickshell.env("OMARCHY_MEDIA_IDLE_MAX_MINUTES") || "30")
    if (!isFinite(value) || value <= 0) return 30 * 60 * 1000
    return Math.max(5 * 60 * 1000, Math.min(value * 60 * 1000, 2 * 60 * 60 * 1000))
  }

  function nowMs() {
    return Date.now()
  }

  function nowIso() {
    return new Date().toISOString()
  }

  function logEvent(event, details) {
    var suffix = details === undefined || details === null || details === "" ? "" : " " + String(details).slice(0, 256)
    root.lastEventAt = root.nowIso()
    root.lastEvent = String(event).slice(0, 64) + suffix
    console.log("omarchy media-idle " + root.lastEventAt + " " + root.lastEvent)
  }

  function normalizedToken(value) {
    return String(value || "").toLowerCase().replace(/^org\.mpris\.mediaplayer2\./, "").replace(/\.instance[0-9]+$/, "").replace(/[^a-z0-9]/g, "")
  }

  function playerKey(player) {
    if (!player) return ""
    return String(player.dbusName || player.desktopEntry || player.identity || "").slice(0, 512)
  }

  function playerTokens(player) {
    if (!player) return []
    var values = [player.dbusName, player.desktopEntry, player.identity]
    var output = []
    for (var i = 0; i < values.length; i++) {
      var raw = String(values[i] || "").slice(0, 512)
      var normalized = root.normalizedToken(raw)
      if (normalized) output.push(normalized)
      var shortName = root.normalizedToken(raw.replace(/^org\.mpris\.MediaPlayer2\./i, ""))
      if (shortName && output.indexOf(shortName) === -1) output.push(shortName)
    }
    return output
  }

  function trustedPlayer(player) {
    if (!player || player.isPlaying !== true) return false
    var allowed = String(root.trustedPlayerList || "").toLowerCase().split(",")
    var normalizedAllowed = []
    for (var i = 0; i < allowed.length; i++) {
      var token = root.normalizedToken(allowed[i])
      if (token) normalizedAllowed.push(token)
    }
    var actual = root.playerTokens(player)
    for (var a = 0; a < normalizedAllowed.length; a++) {
      for (var b = 0; b < actual.length; b++) {
        if (normalizedAllowed[a] === actual[b]) return true
      }
    }
    return false
  }

  function playerList() {
    var list = root.players
    return list && typeof list.length === "number" ? list : []
  }

  function anyPlayerPlaying() {
    var list = root.playerList()
    for (var i = 0; i < list.length; i++) {
      if (list[i] && list[i].isPlaying === true) return true
    }
    return false
  }

  function trustedPlayingKeys() {
    var list = root.playerList()
    var keys = []
    var seen = {}
    for (var i = 0; i < list.length; i++) {
      if (!root.trustedPlayer(list[i])) continue
      var key = root.playerKey(list[i]) || root.normalizedToken(list[i].identity)
      var identity = "key:" + key
      if (key && seen[identity] !== true) {
        seen[identity] = true
        keys.push(key)
      }
    }
    return keys
  }

  function trustedPlayingKey() {
    var keys = root.trustedPlayingKeys()
    return keys.length ? keys[0] : ""
  }

  function inhibitionRequested() {
    var current = root.trustedPlayingKeys()
    var currentSet = {}
    var retained = []
    var leases = Array.isArray(root.playerLeases) ? root.playerLeases : []
    var i
    for (i = 0; i < current.length; i++) currentSet["key:" + current[i]] = true
    for (i = 0; i < leases.length; i++) {
      if (leases[i] && currentSet["key:" + leases[i].key] === true) retained.push({ key: leases[i].key, until: Number(leases[i].until) })
    }
    var present = {}
    for (i = 0; i < retained.length; i++) present["key:" + retained[i].key] = true
    var now = root.nowMs()
    for (i = 0; i < current.length; i++) {
      if (present["key:" + current[i]] !== true) retained.push({ key: current[i], until: now + root.maxLeaseMs })
    }
    root.playerLeases = retained
    root.leasePlayerKey = ""
    root.leaseUntil = 0
    for (i = 0; i < retained.length; i++) {
      if (retained[i].until > root.leaseUntil) {
        root.leasePlayerKey = retained[i].key
        root.leaseUntil = retained[i].until
      }
      if (retained[i].until > now) return true
    }
    return false
  }

  function desiredText() {
    return root.wantIdleDisabled ? "playing" : "idle-allowed"
  }

  function scheduleRetry() {
    if (root.shuttingDown || retryTimer.running) return
    root.retryAttempt = Math.min(root.retryAttempt + 1, 6)
    retryTimer.interval = Math.min(30000, 1000 * Math.pow(2, root.retryAttempt - 1))
    retryTimer.restart()
  }

  function commandFailed(event) {
    if (root.shuttingDown) return
    if (root.commandPhase === "action") actionWatchdog.stop()
    else statusWatchdog.stop()
    root.commandInFlight = false
    root.commandPhase = ""
    var details = String(root.commandErrorOutput || "").trim().slice(0, 256)
    root.logEvent(event || "command-failed", details)
    root.commandErrorOutput = ""
    root.scheduleRetry()
  }

  function parseAck(text, wantDisable) {
    var value = String(text || "").trim()
    if (wantDisable && value === "disabled") return true
    if (!wantDisable && (value === "enabled" || value === "not-owned")) return true
    return false
  }

  function parseCleanupAck(text) {
    var value = String(text || "").trim()
    return value === "enabled" || value === "not-owned"
  }

  function parseStatus(text) {
    var source = String(text || "").trim()
    if (!source || source.length > 65536) return null
    try {
      var value = JSON.parse(source)
      if (!value || typeof value !== "object" || typeof value.enabled !== "boolean") return null
      return value
    } catch (error) {
      return null
    }
  }

  function startStatus(verify, generation) {
    if (root.shuttingDown || root.commandInFlight) return
    root.commandInFlight = true
    root.commandPhase = verify ? "verify" : "status"
    root.commandGeneration = generation === undefined ? root.generation : generation
    root.statusOutput = ""
    root.commandErrorOutput = ""
    statusProcess.command = ["/usr/bin/python3", "-I", root.pluginDir + "idle_control.py", "status"]
    statusWatchdog.interval = 5000
    statusWatchdog.restart()
    statusProcess.running = true
  }

  function refreshStatus() {
    if (root.shuttingDown || root.commandInFlight) return
    root.startStatus(false, root.generation)
  }

  function startDisableGuard() {
    var parentPid = Number(Quickshell.processId)
    if (!isFinite(parentPid) || parentPid <= 1) return
    root.guardSequence++
    var guardId = "qs-" + parentPid + "-" + Date.now().toString(36) + "-" + root.guardSequence + "-" + Math.floor(Math.random() * 0x100000000).toString(36)
    Quickshell.execDetached(["/usr/bin/python3", "-I", root.pluginDir + "idle_control.py", "guard", String(parentPid), guardId])
  }

  function beginCommand(wantDisable, cleanupOnly) {
    if (root.shuttingDown || root.commandInFlight) return
    root.generation++
    root.commandGeneration = root.generation
    root.commandInFlight = true
    root.commandPhase = "action"
    root.pendingDisable = wantDisable
    root.lastCommand = wantDisable ? "disable" : (cleanupOnly ? "cleanup" : "enable")
    root.actionOutput = ""
    root.commandErrorOutput = ""
    if (wantDisable) {
      root.disableAttempted = true
      root.startDisableGuard()
    }
    root.logEvent("idle-" + root.lastCommand, "playing=" + root.anyPlayerPlaying())
    idleIpc.command = root.lastCommand === "cleanup"
      ? ["/usr/bin/python3", "-I", root.pluginDir + "idle_control.py", "cleanup"]
      : ["/usr/bin/python3", "-I", root.pluginDir + "idle_control.py", "action", root.lastCommand]
    actionWatchdog.interval = 5000
    actionWatchdog.restart()
    idleIpc.running = true
  }

  function actionTimedOut() {
    if (root.shuttingDown || root.commandPhase !== "action" || !idleIpc.running) return
    root.generation++
    root.commandInFlight = false
    root.commandPhase = ""
    root.logEvent("action-timeout")
    idleIpc.running = false
    root.scheduleRetry()
  }

  function statusTimedOut() {
    if (root.shuttingDown || (root.commandPhase !== "status" && root.commandPhase !== "verify") || !statusProcess.running) return
    root.generation++
    root.commandInFlight = false
    root.commandPhase = ""
    root.logEvent("status-timeout")
    statusProcess.running = false
    root.scheduleRetry()
  }

  function reconcile() {
    if (root.shuttingDown) return
    root.wantIdleDisabled = root.inhibitionRequested()
    if (root.commandInFlight) return
    if (!root.actualStateKnown) {
      root.startStatus(false, root.generation)
      return
    }
    if (root.wantIdleDisabled) {
      if (root.actualIdleEnabled === false) return
      root.beginCommand(true)
      return
    }
    if (root.idleDisabledByUs) root.beginCommand(false)
    else if (root.disableAttempted) root.beginCommand(false, true)
  }

  function notePlayerChange() {
    root.reconcile()
  }

  function cleanup() {
    if (root.shuttingDown) return
    root.shuttingDown = true
    retryTimer.stop()
    leaseTimer.stop()
    statusTimer.stop()
    actionWatchdog.stop()
    statusWatchdog.stop()
    root.generation++
    root.commandInFlight = false
    root.commandPhase = ""
    var ownsDisable = root.idleDisabledByUs || root.disableAttempted
    if (ownsDisable && !root.cleanupIssued) {
      root.cleanupIssued = true
      root.logEvent("cleanup-requested")
      Quickshell.execDetached(["/usr/bin/python3", "-I", root.pluginDir + "idle_control.py", "cleanup"])
      if (idleIpc.running) idleIpc.running = false
      if (statusProcess.running) statusProcess.running = false
    }
  }

  Timer {
    id: retryTimer
    interval: 1000
    repeat: false
    onTriggered: root.reconcile()
  }

  Timer {
    id: leaseTimer
    interval: 15000
    repeat: true
    running: !root.shuttingDown
    onTriggered: root.reconcile()
  }

  Timer {
    id: statusTimer
    interval: 15000
    repeat: true
    running: !root.shuttingDown
    onTriggered: root.refreshStatus()
  }

  Timer {
    id: actionWatchdog
    interval: 5000
    repeat: false
    onTriggered: root.actionTimedOut()
  }

  Timer {
    id: statusWatchdog
    interval: 5000
    repeat: false
    onTriggered: root.statusTimedOut()
  }

  Process {
    id: idleIpc
    stdout: StdioCollector {
      id: actionCollector
      waitForEnd: true
      onStreamFinished: root.actionOutput = String(actionCollector.text || "")
    }
    stderr: StdioCollector {
      id: actionErrorCollector
      waitForEnd: true
      onStreamFinished: root.commandErrorOutput = String(actionErrorCollector.text || "").slice(0, 2048)
    }
    onExited: function(exitCode, exitStatus) {
      if (root.shuttingDown) return
      if (root.commandGeneration !== root.generation) {
        actionWatchdog.stop()
        root.commandInFlight = false
        root.commandPhase = ""
        root.reconcile()
        return
      }
      if (root.commandPhase !== "action") {
        root.commandFailed("unexpected-command-exit")
        return
      }
      actionWatchdog.stop()
      root.logEvent("action-exit", "code=" + exitCode + " status=" + exitStatus)
      var cleanupCommand = root.lastCommand === "cleanup"
      var acknowledged = cleanupCommand
        ? root.parseCleanupAck(root.actionOutput)
        : root.parseAck(root.actionOutput, root.pendingDisable)
      if (exitCode !== 0 || !acknowledged) {
        root.commandFailed("action-unverified")
        return
      }
      var output = String(root.actionOutput || "").trim()
      root.lastAck = output
      if (cleanupCommand) {
        root.idleDisabledByUs = false
        root.disableAttempted = false
        if (output === "enabled") {
          root.actualIdleEnabled = true
          root.actualStateKnown = true
        } else {
          root.actualStateKnown = false
        }
      } else if (!root.pendingDisable && output === "not-owned") {
        root.idleDisabledByUs = false
        root.disableAttempted = false
        root.actualStateKnown = false
      } else {
        root.actualIdleEnabled = !root.pendingDisable
        root.actualStateKnown = true
        root.idleDisabledByUs = root.pendingDisable
        if (!root.pendingDisable) root.disableAttempted = false
      }
      root.commandInFlight = false
      root.commandPhase = ""
      root.commandErrorOutput = ""
      root.retryAttempt = 0
      retryTimer.stop()
      root.reconcile()
    }
  }

  Process {
    id: statusProcess
    stdout: StdioCollector {
      id: statusCollector
      waitForEnd: true
      onStreamFinished: root.statusOutput = String(statusCollector.text || "")
    }
    stderr: StdioCollector {
      id: statusErrorCollector
      waitForEnd: true
      onStreamFinished: root.commandErrorOutput = String(statusErrorCollector.text || "").slice(0, 2048)
    }
    onExited: function(exitCode, exitStatus) {
      if (root.shuttingDown) return
      if (root.commandGeneration !== root.generation) {
        statusWatchdog.stop()
        root.commandInFlight = false
        root.commandPhase = ""
        root.reconcile()
        return
      }
      if (root.commandPhase !== "status" && root.commandPhase !== "verify") {
        root.commandFailed("unexpected-status-exit")
        return
      }
      statusWatchdog.stop()
      root.logEvent("status-exit", "code=" + exitCode + " status=" + exitStatus)
      var value = exitCode === 0 ? root.parseStatus(root.statusOutput) : null
      if (value === null) {
        root.commandFailed("status-unavailable")
        return
      }
      root.actualIdleEnabled = value.enabled
      root.actualStateKnown = true
      if (root.commandPhase === "verify") {
        if (value.enabled === root.pendingDisable) {
          root.commandFailed("status-mismatch")
          return
        }
        root.idleDisabledByUs = root.pendingDisable
        if (!root.pendingDisable) root.disableAttempted = false
      }
      root.commandInFlight = false
      root.commandPhase = ""
      root.commandErrorOutput = ""
      root.retryAttempt = 0
      retryTimer.stop()
      root.reconcile()
    }
  }

  onPlayersChanged: root.notePlayerChange()

  Instantiator {
    model: root.playerList()
    delegate: Connections {
      required property var modelData
      target: modelData
      function onIsPlayingChanged() { root.notePlayerChange() }
    }
  }

  Component.onCompleted: {
    root.logEvent("service-ready", "players=" + root.playerList().length)
    root.reconcile()
  }

  Component.onDestruction: root.cleanup()

  IpcHandler {
    target: "media-idle"

    function status(): string {
      return JSON.stringify({
        anyPlaying: root.anyPlayerPlaying(),
        trustedPlaying: root.trustedPlayingKey() !== "",
        players: root.playerList().length,
        idleDisabledByUs: root.idleDisabledByUs,
        wantIdleDisabled: root.wantIdleDisabled,
        actualIdleEnabled: root.actualIdleEnabled,
        actualStateKnown: root.actualStateKnown,
        pendingDisable: root.pendingDisable,
        commandInFlight: root.commandInFlight,
        commandPhase: root.commandPhase,
        generation: root.generation,
        retryAttempt: root.retryAttempt,
        failures: root.retryAttempt,
        leaseUntil: root.leaseUntil,
        lastCommand: root.lastCommand,
        lastAck: root.lastAck,
        lastEvent: root.lastEvent,
        lastEventAt: root.lastEventAt
      })
    }

    function reconcile(): string {
      root.reconcile()
      return root.commandInFlight ? "pending" : root.desiredText()
    }
  }
}
