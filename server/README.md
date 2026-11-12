# Multiplayer relay — deploy & operations

Node service implementing the multiplayer relay for the fork of Prodigy 1.30.0
in this repo. It speaks socket.io **1.x** (the game bundles `socket.io-1.3.5.js`)
and serves the small REST surface the game needs. It verifies Firebase ID
tokens, so only signed-in players can connect or call the API.

## What it implements

| Surface | Endpoint / event | Purpose |
|---|---|---|
| REST | `GET /status` | Render health check; uptime + connected player count |
| REST | `GET /v1/worlds` | world list `[{id, full}]` for the in-game picker; `full` is 0–100 vs `WORLD_CAPACITY` |
| REST | `POST /matchmaking-api/begin` | arena queue `{userID, level, score, playerData, token}`; matched players both receive a socket message `{action:"arena", data:{playerA, playerB}}` |
| REST | `POST /matchmaking-api/end` | leave the arena queue |
| REST | `GET /leaderboard-api/pvp/:min/:max?page&limit&userID` | top arena entries `[{place, userID, appearance, equipment, data, isMember}]`; 5-minute cache over the `publicPlayers` Firestore mirror |
| socket.io 1.x | handshake query `userId, worldId, userToken, zone` | `userToken` must be a Firebase ID token for the same uid |
| socket.io | `switchZone` → `playerList`, `playerJoined`, `playerLeft` | presence in zone rooms |
| socket.io | `message` (broadcast or `target`) | zone broadcast (`move`/`change`/`chat`/`emit`) and targeted delivery (PVP battle sync, challenges); offline `target` is persisted to `mail/{uid}/items/{id}` |

Firestore collections used by the relay (server-side, via Admin SDK):
`publicPlayers/{uid}` (read for leaderboard), `mail/{uid}/items/{id}` (write
when the recipient is offline). The client also reads `publicPlayers`
directly for `getCharData` — see the `publicPlayers` rule below.

## Deploy to Render (free tier)

1. Push this repo to GitHub (already the origin).
2. Render dashboard → **New → Blueprint** → pick this repo. `render.yaml`
   creates `old-prodigy-relay` (rootDir `server`, health check `/status`).
3. Set the `FIREBASE_SERVICE_ACCOUNT` env var (Environment → Add):
   - Firebase console → Project settings → **Service accounts** →
     **Generate new private key**.
   - Copy the **entire JSON file** as the value (Render env vars are
     single-line; the JSON has no newlines problem if pasted as-is —
     paste it verbatim including braces).
4. Deploy. The service URL is `https://old-prodigy-relay.onrender.com`
   (or with a suffix if the name is taken — check the dashboard).
5. Put that URL (with trailing slash) in
   `oldprodigy/1-30-0/js/firebase-config.js` as `window.PRODIGY_RELAY_URL`.
6. Publish the Firestore rules below (Firestore console → Rules).
7. Free-tier caveats: the service **sleeps after ~15 min idle** and takes
   ~1 minute to wake. The first connection/world-list fetch after idle can
   time out or hang; retrying after the wake succeeds. Websockets drop on
   spin-down (players get disconnected; they can re-enter the world picker).
   Player data needs nobody online — persistence is Firestore.

### Firestore rules (merge into your existing ruleset)

```
match /publicPlayers/{uid} {
  allow read: if true;                       // needed for getCharData + other players' avatars
  allow write: if request.auth != null && request.auth.uid == uid;
}
match /mail/{uid}/items/{mailId} {
  allow read: if request.auth != null && request.auth.uid == uid;
  allow delete: if request.auth != null && request.auth.uid == uid;
  allow create, update: if false;            // only the relay (Admin SDK) writes mail
}
```

Keep the existing rules for `players/`, `saves/`, `usernames/` unchanged.

## Environment variables

| Var | Required | Default | Meaning |
|---|---|---|---|
| `PORT` | no | `3100` | Render sets this automatically |
| `FIREBASE_SERVICE_ACCOUNT` | **yes** (prod) | — | service-account JSON; without it the relay exits (unless dev mode below) |
| `RELAY_ALLOW_UNVERIFIED` | no | unset | `1` = skip token verification. **Local dev only**, never on the internet |
| `WORLD_COUNT` | no | `20` | ids served by `/v1/worlds` (must be ≤ the world NAMES the client knows) |
| `WORLD_CAPACITY` | no | `100` | players per world; only affects the "full" percentage shown in the picker |
| `MATCH_WINDOW` | no | `500` | arena matchmaking score delta |
| `MATCH_QUEUE_TTL` | no | `90000` | queue entry lifetime (ms) |

## Local development

```
cd server
npm install
RELAY_ALLOW_UNVERIFIED=1 npm start
```

Point the game at `http://127.0.0.1:3100/` via `PRODIGY_RELAY_URL` in
`js/firebase-config.js`. In this mode the relay trusts any `userId` —
never expose it.

## Account / matchmaking integrity notes

- A fresh socket for an account **supersedes** the previous one (same
  account in two tabs → the older tab is disconnected).
- `data.userID` on any message is always overwritten with the authenticated
  uid — the client cannot impersonate another sender.
- Matchmaking pairs the two closest waiting players within `MATCH_WINDOW`
  arena points; entries expire after 90 s; the client re-queues every 60 s
  while searching.
- The arena leaderboard is a 5-minute in-memory cache of the top 200
  `publicPlayers` by `arenaScore`; no per-request Firestore reads.

## What the relay does NOT do

- No writes to `saves/` or `players/` — saves stay client → Firestore,
  owner-locked by rules.
- No chat moderation beyond size/rate limits.
- No cross-world PVP; matches and challenges route within account reach
  (targeted messages deliver to the recipient's live sockets anywhere on
  the relay) and unreachable recipients get the message as mail.
