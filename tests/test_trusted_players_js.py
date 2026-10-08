import json
import shutil
import subprocess
import unittest
from pathlib import Path

# The allowlist matcher decides which apps are allowed to suppress your
# screensaver, so it is worth testing for behaviour rather than for the
# presence of a string. TrustedPlayers.js is plain JS with no Qt dependency,
# which lets node run it directly. node ships with the GitHub runners and on
# most desktops; where it is missing these skip rather than fail, so the Python
# suite stays runnable on its own.
NODE = shutil.which("node")
MATCHER = Path(__file__).parents[1] / "TrustedPlayers.js"

DRIVER = r"""
const fs = require('fs')
const api = new Function(
  fs.readFileSync(process.argv[1], 'utf8') +
  '; return {isTrustedPlayer, resolveEntries, playerTokens, tokenSegments,' +
  ' identityTokens, effective, parseSelection, candidateIdForPlayer}'
)()
const cases = JSON.parse(process.argv[2])
console.log(JSON.stringify(cases.map(function(c) {
  return api.isTrustedPlayer(
    {dbusName: c.bus || '', desktopEntry: c.de || '', identity: c.id || '', isPlaying: c.playing !== false},
    api.resolveEntries(c.list)
  )
})))
"""


@unittest.skipUnless(NODE, "node is required to exercise TrustedPlayers.js")
class MatcherBehaviourTests(unittest.TestCase):
    def decide(self, cases):
        result = subprocess.run(
            [NODE, "-e", DRIVER, str(MATCHER), json.dumps(cases)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_a_listed_app_is_trusted(self):
        cases = [
            {"list": "spotify", "bus": "org.mpris.MediaPlayer2.spotify", "de": "spotify", "id": "Spotify"},
            {"list": "vlc", "bus": "org.mpris.MediaPlayer2.vlc", "de": "vlc", "id": "VLC media player"},
            {"list": "celluloid", "bus": "org.mpris.MediaPlayer2.io.github.celluloid_player.celluloid",
             "de": "io.github.celluloid_player.celluloid", "id": "Celluloid"},
            {"list": "jellyfin", "bus": "org.mpris.MediaPlayer2.jellyfin",
             "de": "org.jellyfin.JellyfinDesktop", "id": "Jellyfin Desktop"},
            {"list": "moonfin", "bus": "org.mpris.MediaPlayer2.moonfin", "de": "org.moonfin.linux", "id": "Moonfin"},
            {"list": "brave", "bus": "org.mpris.MediaPlayer2.brave.instance799689", "de": "brave", "id": "Brave"},
        ]
        self.assertEqual(self.decide(cases), [True] * len(cases))

    def test_an_unlisted_app_is_ignored(self):
        cases = [
            {"list": "vlc", "bus": "org.mpris.MediaPlayer2.rhythmbox", "de": "rhythmbox", "id": "Rhythmbox"},
            {"list": "vlc,mpv,celluloid", "bus": "org.mpris.MediaPlayer2.rhythmbox", "de": "rhythmbox", "id": "Rhythmbox"},
        ]
        self.assertEqual(self.decide(cases), [False] * len(cases))

    def test_a_display_name_does_not_inherit_trust_from_a_substring(self):
        # "NotVLC" camelCase-splits to the token "vlc", which used to let an app
        # nobody ticked hold the screen awake. Identity is a human display name,
        # so it is split on separators only.
        cases = [
            {"list": "vlc", "bus": "org.mpris.MediaPlayer2.notvlc", "de": "notvlc", "id": "NotVLC"},
            {"list": "jellyfin", "bus": "org.mpris.MediaPlayer2.notjellyfin", "de": "notjellyfin", "id": "NotJellyfin"},
        ]
        self.assertEqual(self.decide(cases), [False, False])

    def test_a_bare_entry_does_not_match_a_containing_app(self):
        cases = [
            {"list": "vlc", "bus": "org.mpris.MediaPlayer2.vlcx", "de": "vlcx", "id": "vlcx"},
            {"list": "vlc", "bus": "org.mpris.MediaPlayer2.myvlcplayer", "de": "myvlcplayer", "id": "myvlcplayer"},
        ]
        self.assertEqual(self.decide(cases), [False, False])

    def test_camel_case_still_splits_on_identifiers(self):
        # The camelCase rule exists for machine-written ids; dropping it for
        # those would break real apps, so it is kept for everything but the
        # display name.
        cases = [
            {"list": "jellyfin", "bus": "org.mpris.MediaPlayer2.jellyfin",
             "de": "org.jellyfin.JellyfinDesktop", "id": "Anything At All"},
            {"list": "kodi", "bus": "org.mpris.MediaPlayer2.kodi", "de": "org.kodi.kodi", "id": "Kodi"},
        ]
        self.assertEqual(self.decide(cases), [True, True])

    def test_a_paused_player_is_never_trusted(self):
        cases = [
            {"list": "spotify", "bus": "org.mpris.MediaPlayer2.spotify", "de": "spotify",
             "id": "Spotify", "playing": False},
        ]
        self.assertEqual(self.decide(cases), [False])

    def test_the_env_var_style_list_still_matches_its_own_entries(self):
        cases = [
            {"list": "spotify,spotifyd,vlc,mpv,celluloid,io.github.celluloid_player.celluloid,"
                     "jellyfin,jellyfin-media-player,jellyfin-mpv,moonfin,firefox,brave,cliamp",
             "bus": "org.mpris.MediaPlayer2.moonfin", "de": "org.moonfin.linux", "id": "Moonfin"},
            {"list": "spotify,spotifyd,vlc,mpv,celluloid,io.github.celluloid_player.celluloid,"
                     "jellyfin,jellyfin-media-player,jellyfin-mpv,moonfin,firefox,brave,cliamp",
             "bus": "org.mpris.MediaPlayer2.cliamp", "de": "cliamp", "id": "Cliamp"},
        ]
        self.assertEqual(self.decide(cases), [True, True])


@unittest.skipUnless(NODE, "node is required to exercise TrustedPlayers.js")
class ResolutionTests(unittest.TestCase):
    def _call(self, expression):
        script = (
            "const fs = require('fs');"
            "const api = new Function(fs.readFileSync(process.argv[1], 'utf8') + "
            "'; return {effective, parseSelection, candidateIdForPlayer, playerTokens}')();"
            "console.log(JSON.stringify(" + expression + "));"
        )
        result = subprocess.run([NODE, "-e", script, str(MATCHER)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_additions_and_removals_resolve_over_the_base(self):
        expression = (
            "api.effective('spotify,vlc,firefox,brave',"
            "{trusted:['kodi'],untrusted:['firefox']})"
        )
        self.assertEqual(
            self._call(expression).split(","),
            ["spotify", "vlc", "brave", "kodi"],
        )

    def test_removals_win_when_an_entry_is_in_both_lists(self):
        self.assertEqual(
            self._call("api.effective('vlc', {trusted:['vlc'],untrusted:['vlc']})"), ""
        )

    def test_an_empty_selection_leaves_the_base_alone(self):
        self.assertEqual(
            self._call("api.effective('vlc,mpv', {trusted:[],untrusted:[]})"), "vlc,mpv"
        )

    def test_selection_parsing_is_total(self):
        for raw in ["{not json", "[1,2]", '"str"', "null", "", '{"trusted":"x"}']:
            self.assertEqual(
                self._call("api.parseSelection(" + json.dumps(raw) + ")"),
                {"trusted": [], "untrusted": []},
                raw,
            )

    def test_selection_parsing_drops_unusable_entries(self):
        raw = json.dumps({"trusted": ["moonfin", "../escape", "a/b", "", 7], "untrusted": ["firefox"]})
        self.assertEqual(
            self._call("api.parseSelection(" + json.dumps(raw) + ")"),
            {"trusted": ["moonfin"], "untrusted": ["firefox"]},
        )

    def test_candidate_id_prefers_the_desktop_entry(self):
        self.assertEqual(
            self._call("api.candidateIdForPlayer({dbusName:'org.mpris.MediaPlayer2.moonfin',"
                       "desktopEntry:'org.moonfin.linux',identity:'Moonfin'})"),
            "org.moonfin.linux",
        )

    def test_candidate_id_falls_back_to_the_bus_name(self):
        self.assertEqual(
            self._call("api.candidateIdForPlayer({dbusName:'org.mpris.MediaPlayer2.foo.instance7',"
                       "desktopEntry:'',identity:'Foo'})"),
            "foo",
        )


if __name__ == "__main__":
    unittest.main()