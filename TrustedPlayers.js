// Allowlist matching shared by Service.qml and Players.qml.
//
// Service.qml decides whether to inhibit idle from this; Players.qml draws the
// picker's checkboxes from this. They have to be the same code: if the picker
// ticked a box using different rules than the service matches with, the tick
// would mean nothing. Every function here is pure -- callers pass the data in.

var DEFAULT_PLAYERS = "spotify,spotifyd,vlc,mpv,celluloid,io.github.celluloid_player.celluloid,jellyfin,jellyfin-media-player,jellyfin-mpv,moonfin,youtube,youtube-music,firefox,chromium,chromium-browser,google-chrome,brave,brave-browser,plex,plexamp,mpvpaper,harmonoid,strawberry,musicplayer"

// Words that carry no vendor information. Dropped so "org.mpris.MediaPlayer2.vlc"
// reduces to "vlc" and "io.github.celluloid_player.celluloid" reduces to
// "celluloid" rather than needing a whole-string comparison.
var NOISE = {
  "org": true, "com": true, "net": true, "io": true, "de": true, "dev": true,
  "app": true, "apps": true, "co": true, "uk": true, "me": true,
  "github": true, "gitlab": true, "gnome": true, "kde": true, "qt": true,
  "player": true, "players": true, "media": true, "mediaplayer": true,
  "mediaplayer2": true, "mpris": true, "instance": true, "desktop": true,
  "application": true, "x": true, "the": true, "player2": true
}

var LIST_KEYS = ["trusted", "untrusted"]

function normalizedToken(value) {
  return String(value || "").toLowerCase().replace(/^org\.mpris\.mediaplayer2\./, "").replace(/\.instance[0-9]+$/, "").replace(/[^a-z0-9]/g, "")
}

// Splits on separators and camelCase boundaries, dropping tokens that carry no
// vendor information, so "org.jellyfin.JellyfinDesktop" yields "jellyfin".
function tokenSegments(raw) {
  var value = String(raw || "").slice(0, 512)
  if (!value) return []
  var spaced = value.replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
  var parts = spaced.split(/[^A-Za-z0-9]+/)
  var output = []
  for (var i = 0; i < parts.length; i++) {
    var segment = normalizedToken(parts[i])
    if (!segment || NOISE[segment] === true) continue
    if (output.indexOf(segment) === -1) output.push(segment)
  }
  return output
}

// A bare entry like "jellyfin" is a whole name and only matches whole tokens.
// An entry carrying separators is a reverse-DNS id or executable name, so it
// additionally matches on the informative fragments inside it.
function allowedTokens(entry) {
  var raw = String(entry || "").slice(0, 512)
  var tokens = []
  var whole = normalizedToken(raw)
  if (whole) tokens.push(whole)
  if (!/[.\-_ ]/.test(raw.trim())) return tokens
  var segments = tokenSegments(raw)
  for (var i = 0; i < segments.length; i++) {
    if (tokens.indexOf(segments[i]) === -1) tokens.push(segments[i])
  }
  return tokens
}

// Reverse-DNS ids and display names hide the vendor token inside decoration:
// "io.github.celluloid_player.celluloid" and "Jellyfin Desktop" both only
// expose it as a fragment, so a single whole-string comparison never matches
// a configured "celluloid" or "jellyfin".
function identityTokens(values) {
  var output = []
  function push(token) {
    if (token && output.indexOf(token) === -1) output.push(token)
  }
  for (var i = 0; i < values.length; i++) {
    var raw = String(values[i] || "").slice(0, 512)
    if (!raw) continue
    push(normalizedToken(raw))
    push(normalizedToken(raw.replace(/^org\.mpris\.MediaPlayer2\./i, "")))
    var segments = tokenSegments(raw)
    for (var s = 0; s < segments.length; s++) push(segments[s])
  }
  return output
}

function playerTokens(player) {
  if (!player) return []
  return identityTokens([player.dbusName, player.desktopEntry, player.identity])
}

// Flattens a comma-separated allowlist into the token set a lookup compares
// against. Callers cache this; it is the expensive half of matching.
function resolveEntries(list) {
  var entries = String(list || "").toLowerCase().split(",")
  var tokens = []
  for (var i = 0; i < entries.length; i++) {
    var allowed = allowedTokens(entries[i])
    for (var t = 0; t < allowed.length; t++) {
      if (tokens.indexOf(allowed[t]) === -1) tokens.push(allowed[t])
    }
  }
  return tokens
}

function matchesTokens(allowTokens, tokens) {
  for (var a = 0; a < allowTokens.length; a++) {
    for (var b = 0; b < tokens.length; b++) {
      if (allowTokens[a] === tokens[b]) return true
    }
  }
  return false
}

function isTrustedPlayer(player, allowTokens) {
  if (!player || player.isPlaying !== true) return false
  return matchesTokens(allowTokens, playerTokens(player))
}

// Whether a pickable candidate id -- a desktop entry id, or a bus name for a
// player with no desktop entry -- is currently trusted. Same rules as a live
// player, minus the "is playing" condition, so ticking this box is a promise
// the service will keep.
function isCandidateTrusted(candidateId, allowTokens) {
  if (!candidateId) return false
  return matchesTokens(allowTokens, identityTokens([candidateId]))
}

// (env var or built-ins) + additions - removals. Removals win, so an id that
// somehow lands in both lists resolves the same way for every reader.
function effective(baseList, selection) {
  var entries = String(baseList || "").split(",")
  var trusted = selection && Array.isArray(selection.trusted) ? selection.trusted : []
  var untrusted = selection && Array.isArray(selection.untrusted) ? selection.untrusted : []
  for (var i = 0; i < trusted.length; i++) {
    var added = String(trusted[i] || "").trim()
    if (added) entries.push(added)
  }
  var output = []
  var seen = {}
  for (var j = 0; j < entries.length; j++) {
    var entry = entries[j].trim().toLowerCase()
    if (!entry || seen[entry] === true) continue
    if (untrusted.indexOf(entry) !== -1) continue
    seen[entry] = true
    output.push(entry)
  }
  return output.join(",")
}

// Total on purpose: a malformed or half-written file yields empty sets, which
// degrades to the built-in allowlist. Throwing here would take the service down
// over a typo in a picker file.
function parseSelection(raw) {
  var empty = { trusted: [], untrusted: [] }
  var source = String(raw || "")
  if (!source || source.length > 65536) return empty
  var value
  try {
    value = JSON.parse(source)
  } catch (error) {
    return empty
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return empty

  var parsed = { trusted: [], untrusted: [] }
  for (var i = 0; i < LIST_KEYS.length; i++) {
    var key = LIST_KEYS[i]
    var items = value[key]
    if (!Array.isArray(items)) continue
    for (var n = 0; n < items.length && n < 256; n++) {
      var entry = typeof items[n] === "string" ? items[n].trim() : ""
      if (entry && entry.length <= 128 && /^[A-Za-z0-9][A-Za-z0-9._+-]*$/.test(entry) && parsed[key].indexOf(entry) === -1) {
        parsed[key].push(entry)
      }
    }
  }
  parsed.trusted = parsed.trusted.filter(function(entry) { return parsed.untrusted.indexOf(entry) === -1 })
  return parsed
}

// The stable id to store for a live MPRIS player: the desktop entry its author
// wrote, falling back to the bus-name suffix when the player declares none.
function candidateIdForPlayer(player) {
  if (!player) return ""
  var desktopEntry = String(player.desktopEntry || "").trim()
  if (desktopEntry) return desktopEntry
  var busName = String(player.dbusName || "").trim()
  var stripped = busName.replace(/^org\.mpris\.MediaPlayer2\./i, "").replace(/\.instance[0-9]+$/, "")
  if (stripped) return stripped
  return String(player.identity || "").trim()
}