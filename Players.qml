pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import Quickshell.Services.Mpris
import qs.Commons
import "TrustedPlayers.js" as TrustedPlayers

// Picker for the trusted-player allowlist.
//
// Lists every app that could plausibly inhibit idle -- the MPRIS players on
// the bus right now, unioned with the installed apps that advertise themselves
// as media players -- and lets each one be trusted or not. Ticking writes to
// media-idle.json, which Service.qml watches, so a change takes effect without
// restarting the shell.
//
// Checkbox state is computed with the same TrustedPlayers matching the service
// uses, so a tick here is a promise the service keeps.
Item {
  id: root

  readonly property string pluginDir: {
    var path = String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "")
    return path.charAt(path.length - 1) === "/" ? path : path + "/"
  }
  readonly property string helper: root.pluginDir + "player_select.py"

  // Same precedence as Service.qml: the env var replaces the built-in list.
  readonly property string baseTrustedPlayerList: {
    var configured = String(Quickshell.env("OMARCHY_MEDIA_IDLE_TRUSTED_PLAYERS") || "").slice(0, 4096)
    return configured || TrustedPlayers.DEFAULT_PLAYERS
  }
  readonly property var baseAllowTokens: TrustedPlayers.resolveEntries(root.baseTrustedPlayerList)
  readonly property var effectiveAllowTokens: TrustedPlayers.resolveEntries(TrustedPlayers.effective(root.baseTrustedPlayerList, root.selection))

  property bool opened: false
  property var selection: ({ trusted: [], untrusted: [] })
  property var installed: []
  property string filterText: ""
  property int currentIndex: 0
  property bool loaded: false
  property string error: ""

  readonly property var players: Mpris.players ? Mpris.players.values : []

  // --------------------------------------------------------------- the rows
  //
  // Two sources, merged on the stable candidate id: what is on the bus now,
  // and what is installed but not running. Installed-only rows are how you
  // trust an app before you have launched it once.
  readonly property var allRows: {
    var seen = {}
    var rows = []
    var installedList = root.installed
    var live = root.playersList()

    function push(id, name, playing, builtin) {
      if (!id || seen[id] === true) return
      seen[id] = true
      rows.push({ id: id, name: name || id, playing: playing, builtin: builtin })
    }

    for (var i = 0; i < live.length; i++) {
      var player = live[i]
      if (!player) continue
      var id = TrustedPlayers.candidateIdForPlayer(player)
      if (!id) continue
      var name = String(player.identity || "").trim() || id
      push(id, name, player.isPlaying === true, TrustedPlayers.isCandidateTrusted(id, root.baseAllowTokens))
    }

    // A live player with no desktop entry still deserves its real name on the
    // row, so installed rows never overwrite a name that came from MPRIS.
    for (var n = 0; n < installedList.length; n++) {
      var entry = installedList[n]
      if (!entry || !entry.id || seen[entry.id] === true) continue
      seen[entry.id] = true
      rows.push({
        id: entry.id,
        name: String(entry.name || entry.id),
        playing: false,
        builtin: TrustedPlayers.isCandidateTrusted(entry.id, root.baseAllowTokens)
      })
    }

    rows.sort(function(a, b) {
      if (a.playing !== b.playing) return a.playing ? -1 : 1
      var byName = a.name.toLowerCase().localeCompare(b.name.toLowerCase())
      if (byName !== 0) return byName
      return a.id.localeCompare(b.id)
    })
    return rows
  }

  readonly property var rows: {
    var needle = String(root.filterText || "").trim().toLowerCase()
    if (!needle) return root.allRows
    var filtered = []
    for (var i = 0; i < root.allRows.length; i++) {
      var row = root.allRows[i]
      if (row.name.toLowerCase().indexOf(needle) !== -1 || row.id.toLowerCase().indexOf(needle) !== -1) {
        filtered.push(row)
      }
    }
    return filtered
  }

  readonly property int trustedCount: {
    var count = 0
    for (var i = 0; i < root.rows.length; i++) {
      if (TrustedPlayers.isCandidateTrusted(root.rows[i].id, root.effectiveAllowTokens)) count++
    }
    return count
  }

  function playersList() {
    return root.players && typeof root.players.length === "number" ? root.players : []
  }

  function isTrusted(id) {
    return TrustedPlayers.isCandidateTrusted(id, root.effectiveAllowTokens)
  }

  function isAdded(id) {
    return root.selection.trusted.indexOf(id) !== -1
  }

  function isRemoved(id) {
    return root.selection.untrusted.indexOf(id) !== -1
  }

  // ------------------------------------------------------------- refreshing
  //
  // Re-read on open rather than trusting what this instance last wrote. The
  // service, a terminal, or a second picker could all have changed the file.
  function refresh() {
    root.error = ""
    root.loaded = false
    stateProc.running = true
    listProc.running = true
  }

  function parseState(text) {
    return TrustedPlayers.parseSelection(String(text || ""))
  }

  function parseCandidates(text) {
    var source = String(text || "").trim()
    if (!source || source.length > 1048576) return []
    try {
      var value = JSON.parse(source)
      if (!value || !Array.isArray(value.candidates)) return []
      return value.candidates
    } catch (error) {
      return []
    }
  }

  // ----------------------------------------------------------------- toggling
  function toggleCurrent() {
    if (root.currentIndex < 0 || root.currentIndex >= root.rows.length) return
    var row = root.rows[root.currentIndex]
    if (!row) return
    // Flipping through the file rather than mutating in memory keeps the
    // checkbox honest if the write is refused.
    root.setEntry(row.id, !root.isTrusted(row.id))
  }

  function setEntry(id, on) {
    if (setProc.running) return
    setProc.pendingId = id
    setProc.pendingOn = on ? "on" : "off"
    setProc.running = true
  }

  function toggleIndex(index) {
    if (index < 0 || index >= root.rows.length) return
    root.currentIndex = index
    root.toggleCurrent()
  }

  function moveSelection(delta) {
    if (root.rows.length === 0) return
    var next = root.currentIndex + delta
    if (next < 0) next = 0
    if (next > root.rows.length - 1) next = root.rows.length - 1
    if (next === root.currentIndex) return
    root.currentIndex = next
    listView.positionViewAtIndex(next, ListView.Contain)
  }

  function dismiss() {
    root.opened = false
  }

  function clearFilter() {
    root.filterText = ""
    root.currentIndex = 0
  }

  // --------------------------------------------------------------- lifecycle
  function open(payload) {
    root.opened = true
    root.clearFilter()
    root.refresh()
    // The layer surface only becomes keyboard-interactive a turn after the
    // shell flips it visible, so focus is claimed once that has happened.
    Qt.callLater(function() { if (root.opened) keyCatcher.forceActiveFocus() })
  }

  function close() {
    root.opened = false
  }

  Process {
    id: stateProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.selection = root.parseState(text)
        root.loaded = true
      }
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.error = String(text || "").trim().slice(0, 200)
    }
    command: ["/usr/bin/python3", "-I", root.helper, "state"]
    running: false
  }

  Process {
    id: listProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.installed = root.parseCandidates(text)
    }
    command: ["/usr/bin/python3", "-I", root.helper, "list"]
    running: false
  }

  Process {
    id: setProc
    property string pendingId: ""
    property string pendingOn: "on"
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        // Take the authoritative result rather than the value we asked for.
        if (setProc.exitCode === 0) {
          root.selection = root.parseState(text)
          root.error = ""
        }
        // Re-read so a change made elsewhere while the picker was open shows up.
        stateProc.running = true
      }
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.error = String(text || "").trim().slice(0, 200)
    }
    command: ["/usr/bin/python3", "-I", root.helper, "set", setProc.pendingId, setProc.pendingOn]
    running: false
  }

  PanelWindow {
    id: panel

    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "davidjm-media-idle"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: root.opened ? WlrKeyboardFocus.Exclusive : WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore

    Rectangle {
      anchors.fill: parent
      color: Color.menu.scrim
    }

    MouseArea {
      anchors.fill: parent
      onClicked: root.dismiss()
    }

    // Every keystroke is handled here rather than by the filter field. This is
    // the shape the stock overlays use (omarchy.emojis, omarchy.clipboard,
    // omarchy.image-picker): a focused scope with Keys.BeforeItem is reliably
    // the active-focus item on a layer surface, while a TextField competing
    // with an enclosing focus scope is not.
    FocusScope {
      id: keyCatcher
      anchors.fill: parent
      focus: true
      Keys.priority: Keys.BeforeItem
      Keys.onPressed: function(event) {
        if (event.key === Qt.Key_Escape) {
          root.dismiss()
          return
        }
        if (event.key === Qt.Key_Down) {
          root.moveSelection(1)
          return
        }
        if (event.key === Qt.Key_Up) {
          root.moveSelection(-1)
          return
        }
        if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
          root.toggleCurrent()
          return
        }
        if (event.key === Qt.Key_Space) {
          root.toggleCurrent()
          return
        }
        if (event.key === Qt.Key_Backspace) {
          root.filterText = String(root.filterText || "").slice(0, -1)
          root.currentIndex = 0
          return
        }
        // Anything that arrives as printable text feeds the filter. Modifier
        // combinations are left alone so a stray Ctrl+C does not type a "c".
        if (event.text && event.text.length > 0 && !(event.modifiers & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier))) {
          root.filterText = String(root.filterText || "") + event.text
          root.currentIndex = 0
        }
      }
    }

    FocusScope {
      id: card
      width: 760
      height: 620
      anchors.centerIn: parent

      Rectangle {
        anchors.fill: parent
        radius: Style.space(12)
        color: Color.menu.background
        border.width: 1
        border.color: Color.menu.border

        ColumnLayout {
          anchors.fill: parent
          anchors.margins: Style.space(20)
          spacing: Style.space(12)

          ColumnLayout {
            Layout.fillWidth: true
            spacing: Style.space(2)

            Text {
              text: "Trusted media players"
              color: Color.menu.text
              font.pixelSize: Style.font.title
              font.weight: Font.Bold
              Layout.fillWidth: true
            }

            Text {
              text: root.loaded
                    ? root.trustedCount + " of " + root.rows.length + " shown can hold the screen awake"
                    : "Looking for media players…"
              color: Color.muted
              font.pixelSize: Style.font.caption
              Layout.fillWidth: true
            }
          }

          TextField {
            id: filter
            // Display only. keyCatcher owns the keyboard, so this must not
            // hold focus or it will take the keys the shortcuts need.
            readOnly: true
            Layout.fillWidth: true
            Layout.preferredHeight: Style.space(38)
            placeholderText: "Filter players"
            placeholderTextColor: Color.muted
            color: Color.menu.text
            font.pixelSize: Style.font.body
            selectByMouse: true
            background: Rectangle {
              radius: Style.space(6)
              color: "transparent"
              border.width: 1
              border.color: Color.menu.border
            }
            text: root.filterText
            onTextChanged: {
              root.currentIndex = 0
              listView.positionViewAtBeginning()
            }
          }

          Text {
            visible: root.error !== ""
            text: root.error
            color: Color.urgent
            wrapMode: Text.WordWrap
            font.pixelSize: Style.font.caption
            Layout.fillWidth: true
          }

          Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: "transparent"
            border.width: 1
            border.color: Qt.alpha(Color.menu.border, 0.5)
            radius: Style.space(6)

            ListView {
              id: listView
              anchors.fill: parent
              anchors.margins: Style.space(4)
              clip: true
              model: root.rows
              currentIndex: root.currentIndex
              boundsBehavior: Flickable.StopAtBounds
              highlightMoveDuration: 120
              ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

              delegate: Item {
                id: rowItem
                required property var modelData
                required property int index

                width: listView.width
                height: Style.space(44)

                readonly property bool trusted: root.isTrusted(rowItem.modelData.id)

                Rectangle {
                  anchors.fill: parent
                  anchors.leftMargin: Style.space(4)
                  anchors.rightMargin: Style.space(4)
                  radius: Style.space(5)
                  color: listView.currentIndex === rowItem.index ? Color.menu.selectedBackground : "transparent"
                }

                RowLayout {
                  anchors.fill: parent
                  anchors.leftMargin: Style.space(12)
                  anchors.rightMargin: Style.space(12)
                  spacing: Style.space(12)

                  Text {
                    text: rowItem.trusted ? "✓" : "☐"
                    color: rowItem.trusted ? Color.accent : Color.muted
                    font.pixelSize: Style.font.title
                    Layout.preferredWidth: Style.space(18)
                  }

                  ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 0

                    Text {
                      text: rowItem.modelData.name
                      color: Color.menu.text
                      elide: Text.ElideRight
                      font.pixelSize: Style.font.body
                      Layout.fillWidth: true
                    }

                    Text {
                      text: rowItem.modelData.id
                      color: Color.muted
                      elide: Text.ElideRight
                      font.pixelSize: Style.font.caption
                      Layout.fillWidth: true
                    }
                  }

                  Text {
                    visible: rowItem.modelData.playing
                    text: "playing"
                    color: Color.accent
                    font.pixelSize: Style.font.caption
                  }

                  Text {
                    visible: rowItem.trusted && root.isAdded(rowItem.modelData.id)
                    text: "added"
                    color: Color.accent
                    font.pixelSize: Style.font.caption
                  }

                  Text {
                    visible: !rowItem.trusted && root.isRemoved(rowItem.modelData.id)
                    text: "removed"
                    color: Color.muted
                    font.pixelSize: Style.font.caption
                  }

                  Text {
                    visible: rowItem.trusted && !root.isAdded(rowItem.modelData.id) && !root.isRemoved(rowItem.modelData.id)
                    text: "default"
                    color: Color.muted
                    font.pixelSize: Style.font.caption
                  }
                }

                MouseArea {
                  anchors.fill: parent
                  onClicked: root.toggleIndex(rowItem.index)
                }
              }
            }

            Text {
              anchors.centerIn: parent
              visible: listView.count === 0
              text: root.loaded ? "No media players found" : "Looking for media players…"
              color: Color.muted
              font.pixelSize: Style.font.body
            }
          }

          Text {
            text: setProc.running
                  ? "Saving…"
                  : "Type to filter · Up/Down to move · Space or Enter to toggle · Esc to close"
            color: Color.muted
            font.pixelSize: Style.font.caption
            Layout.fillWidth: true
          }
        }
      }
    }

    // Focus is claimed in open() via Qt.callLater, once the panel is interactive.
    }
}