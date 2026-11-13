# Changes — old-prodigy-revived (meowgoober)

This file documents every code-level change made to `oldprodigy/1-30-0` (and the relay in `server/`),
with before/after snippets and notes for porting to `oldprodigy/1-39-0`.

## Commits (meowgoober)

| Commit | Description |
|---|---|
| c1883dd | Create h (zip) |
| 13a6d6e | ywe (zips) |
| 8e52285 | add assets |
| b3d34b8 | get all assets from main url |
| 9f84c22 | add offline mode |
| dc382e7 | logo |
| b33e840 | make memberships free |
| 053883e | things — big initial drop (online layer + server) |
| eab7d97 | server |
| 7a07f6a | add two server domains |
| cd7a25d | more server things |
| 01d9b1d | Revert Toonigy changes from README update through relay upload |
| cefcd17 | no filters |
| acc0584 | anyone can see messages now |
| 163caff | fix some things upp!!!! |

## Key code changes

### 1. `index.html` — age gate + qi-stub load
Before: no age gate; game.min.js loaded alone.
After:
```html
<script src="./js/qi-stub.js"></script>
<script src="./js/game.min.js"></script>
<!-- 13+ checkbox + legal bar -->
```

### 2. `js/qi-stub.js` (new) — QI widget shim fixing attacks clearing battle screen.

### 3. `js/firebase-config.js` (new) — relay URL resolution + Firebase web config.
```js
window.PRODIGY_RELAY_URL = "https://old-prodigy-relay.onrender.com/";
window.FIREBASE_CONFIG = { apiKey, authDomain, projectId, ... };
```

### 4. `js/firebase-core.js` (new) — full online layer:
- Firebase auth (Email/Password + username handles)
- Firestore saves: `saves/{uid}`, public mirror `publicPlayers/{uid}`
- Dedupe by signature; `resetCloudDedupe()`
- `enableOnlineMultiplayer(game)` sets `net.loggedIn = true` and stubs legacy endpoints
- `patchGameClasses()`, `patchCharData()` (publicPlayers mirror for getCharData)
- Mail load/delete, account deletion
- Free-text chat + slur filter (allow profanity, block slurs)
- Arena matchmaking self-battle fix (`messageRecieved` uses other player's id)
- `player.userID = FirebaseCore.user.uid` in `refreshNetworkCredentials` (fixes Go To House)

### 5. `js/game.min.js` — ApiClient relay override, NavBar Stylist+Chat buttons, Stylist member flags → `!1` (free), membership toggle.

### 6. `server/index.js` — relay (socket.io 1.7.4, Firebase token auth, worlds, presence, mail, matchmaking, PvP leaderboard, `/leaderboard-api/class/:sort`).

## Porting to 1-39-0 (done)

The 1-39-0 bundle is a different build, so the patches were hand-ported to its
own structures rather than copied from 1-30-0:

1. Copied `js/qi-stub.js`, `js/firebase-config.js`, `js/firebase-core.js` into
   `oldprodigy/1-39-0/js/` and added `js/local-save.js` (1-39-0 has no
   `LocalSave`, but firebase-core depends on it for offline saves/migration).
2. `index.html`: qi-stub before `game.min.js`; Firebase SDK, local-save,
   firebase-config and firebase-core after it.
3. `js/game.min.js`:
   - `ApiClient` URL override covers `root`, `multiplayer`, `leaderboard`,
     `matchmaking`, `account`, `mailer`, `events` (1-39-0 has extra mailer and
     events endpoints).
   - HUD Stylist button: 1-39-0's HUD is `Prodigy.HUD.Menu` +
     `Prodigy.HUD.MenuModel` (not `Prodigy.Menu.MenuBar`), so the button is a
     `MenuModel` descriptor added by `addStylist()`.
   - Membership toggle already existed in Settings > Other; it now also appears
     in Settings > Network, shows the signed-in email, and persists through
     `LocalSave.saveToBrowser()`.
   - Stylist membership flags `M_STYLE` / `M_HAIR` / `M_EYES` set to `!1`.
4. `js/firebase-core.js`:
   - `patchGameClasses()` targets `Prodigy.CharSelect` (1-39-0) as well as
     `Prodigy.Menu.CharSelect` (1-30-0).
   - `patchLoginState()` wires the 1-39-0 `Login` state (`usernameField`,
     `passwordField`, `error`, `progressBox`) to `FirebaseCore.auth` /
     `handleAuth`, and adds `setView` / `offlineModeLoad` / sign-up support.
   - `worldChosen()` falls back to the `house-suburbs` zone because 1-39-0 has
     no `Intro` state.
   - `enableOnlineMultiplayer()` sets `net.loggedIn = true` and stubs the legacy
     3-second `updateCharacter` check-in, so presence/chat/PVP stay enabled.

## Note
Version-separated worlds/saves require threading a `version` field through the
socket handshake and Firestore paths; not yet implemented. Apply per-version by
namespacing `saves/{version}_{uid}` and filtering worlds by version on the relay.
