// Prodigy Rewritten — multiplayer relay (socket.io 1.x, Node).
//
// Speaks the exact protocol the bundled js/socket.io-1.3.5.js client expects:
//   handshake query:  userId, worldId, userToken, zone
//   client -> server: "message"  ({ action, data?, target?, ... })
//                     "switchZone"  (zoneName string)
//   server -> client: "message"        (same shape; target set = direct delivery)
//                     "playerJoined"  (userID string)
//                     "playerLeft"    (userID string)
//                     "playerList"    ([userID, ...])
//
// Also serves the REST surface the game calls (paths are bare; the client is
// pointed here via PRODIGY_RELAY_URL):
//   GET  /status                        health check (Render healthCheckPath)
//   GET  /v1/worlds                     [{ id, full }] — world picker, full = 0-100
//   POST /matchmaking-api/begin         { userID, level, score, playerData, token }
//   POST /matchmaking-api/end           { userID, token }
//   GET  /leaderboard-api/pvp/:min/:max ?page&limit&player_score&userID
//
// Auth: userToken must be a Firebase ID token whose decoded uid matches
// userId. Set FIREBASE_SERVICE_ACCOUNT to a service-account JSON string in
// the environment (Render: env var, see README.md). Without it the relay
// refuses to start, unless RELAY_ALLOW_UNVERIFIED=1 is set (local dev only —
// the relay will then trust any userId and should never face the internet).
//
// Targeted messages whose recipient is online are delivered over that
// recipient's socket(s), wherever they are on the relay. If the recipient is
// offline, the message is persisted to Firestore  mail/{targetUid}/items/{id}
// and the client loads it into its mailbox on next login (owner-only rules).

var PORT = Number(process.env.PORT) || 3100;
var WORLD_COUNT = Number(process.env.WORLD_COUNT) || 20;
var WORLD_CAPACITY = Number(process.env.WORLD_CAPACITY) || 100;
var MATCH_WINDOW = Number(process.env.MATCH_WINDOW) || 500;   // score delta for matchmaking
var MATCH_QUEUE_TTL = Number(process.env.MATCH_QUEUE_TTL) || 90000; // queue entries expire
var LEADERBOARD_SIZE = 200;      // how many entries we keep cached
var LEADERBOARD_TTL = 5 * 60 * 1000;
var MSG_RATE_LIMIT = 30;         // socket messages per second per connection
var MAX_MSG_BYTES = 16 * 1024;

var ALLOW_UNVERIFIED = process.env.RELAY_ALLOW_UNVERIFIED === "1";
var SA_JSON = process.env.FIREBASE_SERVICE_ACCOUNT;

// ---------- firebase-admin (optional for local dev) -------------------------
var adminApp = null;   // firebase-admin/app instance
var adminAuth = null;  // firebase-admin/auth
var adminDb = null;    // firebase-admin/firestore
try {
	if (SA_JSON) {
		var fbApp = require("firebase-admin/app");
		var fbAuth = require("firebase-admin/auth");
		var fbFirestore = require("firebase-admin/firestore");
		adminApp = fbApp.initializeApp({
			credential: fbApp.cert(JSON.parse(SA_JSON))
		});
		adminAuth = fbAuth.getAuth(adminApp);
		adminDb = fbFirestore.getFirestore(adminApp);
		console.log("[relay] firebase-admin initialised (project: " + (adminApp.options.projectId || "unknown") + ")");
	} else if (!ALLOW_UNVERIFIED) {
		console.error("[relay] FATAL: FIREBASE_SERVICE_ACCOUNT is not set.\n" +
			"Set it to a service-account JSON string (see README.md),\n" +
			"or RELAY_ALLOW_UNVERIFIED=1 for local, unauthenticated dev.");
		process.exit(1);
	} else {
		console.warn("[relay] WARNING: running WITHOUT token verification (RELAY_ALLOW_UNVERIFIED=1). Local dev only!");
	}
} catch (e) {
	console.error("[relay] FATAL: could not initialise firebase-admin: " + e);
	process.exit(1);
}

function verifyToken(token, callback) {
	if (ALLOW_UNVERIFIED) return callback(null, null);
	adminAuth.verifyIdToken(String(token || "")).then(function (decoded) {
		callback(null, decoded);
	}, function (err) {
		callback(err || new Error("bad token"));
	});
}

// ---------- express app -----------------------------------------------------
var express = require("express");
var cors = require("cors");
var http = require("http");

var app = express();
app.use(cors({ origin: true }));
app.use(express.urlencoded({ extended: true }));
app.use(express.json({ limit: "256kb" }));

app.get("/status", function (req, res) {
	res.json({ ok: true, uptime: Math.round(process.uptime()), players: playerListByIdCount() });
});

app.get("/v1/worlds", function (req, res) {
	var worlds = [];
	for (var id = 1; id <= WORLD_COUNT; id++) {
		var n = worldCount(id);
		worlds.push({ id: id, full: Math.max(0, Math.min(100, Math.round((n * 100) / WORLD_CAPACITY))) });
	}
	res.json(worlds);
});

// ---------- matchmaking queue (in-memory) -----------------------------------
var queue = new Map(); // uid -> { uid, userID, level, score, playerData, socketId, addedAt }

function queueSweep() {
	var now = Date.now();
	queue.forEach(function (entry, uid) {
		if (now - entry.addedAt > MATCH_QUEUE_TTL) queue.delete(uid);
	});
}

function coerceBool(v) {
	return (v === true || v === "true" || v === 1 || v === "1" || v === "on");
}

function normalizePlayerData(pd) {
	if (!pd || typeof pd !== "object") return null;
	return {
		userID: pd.userID,
		equipment: pd.equipment || {},
		appearance: pd.appearance || {},
		data: pd.data || {},
		isMember: coerceBool(pd.isMember)
	};
}

function tryMatch(entry) {
	var best = null, bestDelta = Infinity;
	queue.forEach(function (other, uid) {
		if (uid === entry.uid || other.socketId === entry.socketId) return;
		var delta = Math.abs((other.score || 0) - (entry.score || 0));
		if (delta <= MATCH_WINDOW && delta < bestDelta) { best = other; bestDelta = delta; }
	});
	if (!best) return null;
	queue.delete(best.uid);
	queue.delete(entry.uid);
	return [best, entry];
}

function handleTokenAndBody(req, res, done) {
	var body = req.body || {};
	var token = body.token || body.userToken || (req.headers.authorization || "").replace(/^Bearer\s+/i, "");
	var userID = String(body.userID || body.userId || "");
	if (!userID) return res.status(400).json({ error: "missing userID" });
	verifyToken(token, function (err, decoded) {
		if (err) return res.status(403).json({ error: "invalid token" });
		if (!ALLOW_UNVERIFIED && decoded && decoded.uid !== userID) return res.status(403).json({ error: "token does not match userID" });
		done(body, userID);
	});
}

app.post("/matchmaking-api/begin", function (req, res) {
	handleTokenAndBody(req, res, function (body, userID) {
		queueSweep();
		var playerData = normalizePlayerData(body.playerData);
		var socketId = socketIdOf(userID);
		if (!socketId) return res.status(409).json({ error: "not connected to the multiplayer relay; rejoin a world first" });
		var entry = {
			uid: userID,
			userID: userID,
			level: Number(body.level) || 1,
			score: Number(body.score) || 0,
			playerData: playerData,
			socketId: socketId,
			addedAt: Date.now()
		};
		var match = tryMatch(entry);
		if (!match) {
			queue.set(userID, entry);
			return res.json({ ok: true, queued: true });
		}
		var p1 = match[0], p2 = match[1];
		var msg = {
			action: "arena",
			data: {
				playerA: { id: p1.userID, data: p1.playerData },
				playerB: { id: p2.userID, data: p2.playerData }
			}
		};
		emitToUser(p1.userID, msg);
		emitToUser(p2.userID, msg);
		res.json({ ok: true, matched: true });
	});
});

app.post("/matchmaking-api/end", function (req, res) {
	handleTokenAndBody(req, res, function (body, userID) {
		queue.delete(userID);
		res.json({ ok: true });
	});
});

// ---------- PvP leaderboard (cached from Firestore) -------------------------
var leaderboardCache = [];
var leaderboardCacheAt = 0;
var leaderboardRefreshInFlight = false;

function refreshLeaderboard(done) {
	if (!adminDb) return done && done();
	if (leaderboardRefreshInFlight) return done && done();
	leaderboardRefreshInFlight = true;
	var finish = function () { leaderboardRefreshInFlight = false; done && done(); };
	adminDb.collection("publicPlayers")
		.orderBy("arenaScore", "desc")
		.limit(LEADERBOARD_SIZE)
		.get()
		.then(function (snap) {
			var entries = [];
			snap.forEach(function (doc) {
				var d = doc.data() || {};
				entries.push({
					userID: doc.id,
					appearance: d.appearance || {},
					equipment: d.equipment || {},
					data: d.data || {},
					isMember: d.isMember ? 1 : 0,
					arenaScore: Number(d.arenaScore) || 0
				});
			});
			// stable tie-break: keep insertion order (earlier mirror wins the tie)
			entries.sort(function (a, b) { return b.arenaScore - a.arenaScore; });
			leaderboardCache = entries;
			leaderboardCacheAt = Date.now();
			finish();
		}, function (e) {
			console.error("[relay] leaderboard refresh failed: " + e);
			finish();
		});
}

app.get("/leaderboard-api/pvp/:min/:max", function (req, res) {
	var min = Number(req.params.min);
	var max = Number(req.params.max);
	if (!isFinite(min)) min = 0;
	if (!isFinite(max)) max = Number.MAX_SAFE_INTEGER;
	var page = Math.max(0, Number(req.query.page) || 0);
	var limit = Math.max(1, Math.min(30, Number(req.query.limit) || 30));

	var serve = function () {
		var rows = leaderboardCache.filter(function (e) { return e.arenaScore >= min && e.arenaScore <= max; });
		var out = [];
		for (var i = page * limit; i < Math.min(rows.length, (page + 1) * limit); i++) {
			var e = rows[i];
			out.push({
				place: i + 1,
				userID: e.userID,
				appearance: e.appearance,
				equipment: e.equipment,
				data: e.data,
				isMember: e.isMember
			});
		}
		res.json(out);
	};

	if (Date.now() - leaderboardCacheAt > LEADERBOARD_TTL) {
		refreshLeaderboard(serve);
	} else {
		serve();
	}
});

// The retail client also asks for a class leaderboard:
//   GET /leaderboard-api/class/<classIDs>?sort=data.level&limit=30
// It renders whatever comes back as a plain list of wizards (no places), and
// treats an empty array as "there was an error loading the leaderboard". This
// fork has no classes, so serve the top players by level from the same
// publicPlayers cache — the list is real player data instead of an error, and
// it costs no extra Firestore reads.
app.get("/leaderboard-api/class/:classIDs", function (req, res) {
	var sort = String(req.query.sort || "data.level");
	var limit = Math.max(1, Math.min(30, Number(req.query.limit) || 30));

	var serve = function () {
		var rows = leaderboardCache.slice();
		try {
			rows.sort(function (a, b) {
				var av = sort.split(".").reduce(function (o, k) {
					return o && typeof o === "object" ? o[k] : undefined;
				}, a);
				var bv = sort.split(".").reduce(function (o, k) {
					return o && typeof o === "object" ? o[k] : undefined;
				}, b);
				return (Number(bv) || 0) - (Number(av) || 0);
			});
		} catch (e) {
			console.error("[relay] class leaderboard sort failed: " + e);
		}
		res.json(rows.slice(0, limit).map(function (e) {
			return {
				userID: e.userID,
				appearance: e.appearance,
				equipment: e.equipment,
				data: e.data,
				isMember: e.isMember
			};
		}));
	};

	if (Date.now() - leaderboardCacheAt > LEADERBOARD_TTL) {
		refreshLeaderboard(serve);
	} else {
		serve();
	}
});

// ---------- http + socket.io ------------------------------------------------
var server = http.createServer(app);
var io = require("socket.io")(server);

// uid -> Set<socket>; socketId -> uid; worldId -> Set<socket>
var uidSockets = new Map();
var socketUid = new Map();
var worldSockets = new Map(); // worldId -> Set<socket>
var socketZone = new Map();   // socket -> zoneRoom

function addUidSocket(uid, socket) {
	if (!uidSockets.has(uid)) uidSockets.set(uid, new Set());
	uidSockets.get(uid).add(socket);
	socketUid.set(socket.id, uid);
}

function removeUidSocket(uid, socket) {
	var set = uidSockets.get(uid);
	if (set) {
		set.delete(socket);
		if (set.size === 0) uidSockets.delete(uid);
	}
	socketUid.delete(socket.id);
}

function addWorldSocket(worldId, socket) {
	if (!worldSockets.has(worldId)) worldSockets.set(worldId, new Set());
	worldSockets.get(worldId).add(socket);
}

function removeWorldSocket(worldId, socket) {
	var set = worldSockets.get(worldId);
	if (set) {
		set.delete(socket);
		if (set.size === 0) worldSockets.delete(worldId);
	}
}

function worldCount(worldId) {
	var set = worldSockets.get(worldId);
	return set ? set.size : 0;
}

function playerListByIdCount() {
	var n = 0;
	uidSockets.forEach(function (set) { n += set.size; });
	return n;
}

function socketIdOf(uid) {
	var set = uidSockets.get(uid);
	if (!set) return null;
	var first = set.values().next();
	return first.done ? null : first.value.id;
}

function emitToUser(uid, msg) {
	var set = uidSockets.get(uid);
	if (!set) return false;
	set.forEach(function (socket) { socket.emit("message", msg); });
	return true;
}

function zoneRoom(worldId, zone) {
	return "world:" + worldId + ":zone:" + zone;
}

function bytesOf(value) {
	try { return JSON.stringify(value).length; } catch (e) { return MAX_MSG_BYTES + 1; }
}

io.on("connection", function (socket) {
	var q = socket.handshake.query || {};
	var userId = String(q.userId || "");
	var worldId = Number(q.worldId) || 1;
	var userToken = String(q.userToken || "");
	if (!isFinite(worldId) || worldId < 1 || worldId > WORLD_COUNT) worldId = 1;

	verifyToken(userToken, function (err, decoded) {
		if (err || !userId || (!ALLOW_UNVERIFIED && decoded && decoded.uid !== userId)) {
			console.log("[relay] refused connection for userId=" + userId + (err ? " (" + (err.code || err.message || err) + ")" : " (uid mismatch)"));
			socket.emit("error", { code: 403, message: "invalid credentials" });
			socket.disconnect(true);
			return;
		}
		bindSocket(socket, userId, worldId);
	});
});

function bindSocket(socket, uid, worldId) {
	// one live session per account: a fresh login supersedes the old socket
	var existing = uidSockets.get(uid);
	if (existing) {
		existing.forEach(function (old) {
			try { old.emit("error", { code: 409, message: "signed in elsewhere" }); old.disconnect(true); } catch (e) {}
		});
		uidSockets.delete(uid);
	}

	addUidSocket(uid, socket);
	addWorldSocket(worldId, socket);
	socket.join("world:" + worldId);
	console.log("[relay] +uid=" + uid + " world=" + worldId + " (world pop: " + worldCount(worldId) + ")");

	var msgWindow = { count: 0, resetAt: Date.now() + 1000 };

	socket.on("switchZone", function (zoneName) {
		if (typeof zoneName !== "string") return;
		zoneName = zoneName.slice(0, 100).replace(/[\x00-\x1f\x7f]/g, "");
		if (!zoneName) return;
		var oldRoom = socketZone.get(socket);
		var newRoom = zoneRoom(worldId, zoneName);
		if (oldRoom === newRoom) return;
		if (oldRoom) {
			socket.leave(oldRoom);
			socket.to(oldRoom).emit("playerLeft", uid);
		}
		socket.join(newRoom);
		socketZone.set(socket, newRoom);
		var peers = [];
		var room = io.sockets.adapter.rooms[newRoom];
		var sids = room && room.sockets ? room.sockets : (room || {});
		for (var sid in sids) {
			if (!Object.prototype.hasOwnProperty.call(sids, sid)) continue;
			var otherUid = socketUid.get(sid);
			if (otherUid && otherUid !== uid && peers.indexOf(otherUid) === -1) peers.push(otherUid);
		}
		socket.emit("playerList", peers);
		socket.to(newRoom).emit("playerJoined", uid);
	});

	socket.on("message", function (msg) {
		// rate limit + size guard
		var now = Date.now();
		if (now > msgWindow.resetAt) { msgWindow.count = 0; msgWindow.resetAt = now + 1000; }
		if (++msgWindow.count > MSG_RATE_LIMIT) return;
		if (!msg || typeof msg !== "object" || typeof msg.action !== "string") return;
		if (bytesOf(msg) > MAX_MSG_BYTES) return;

		// sender identity is authoritative — clients may not spoof it
		if (msg.data && typeof msg.data === "object") msg.data.userID = uid;

		var target = msg.target != null ? String(msg.target) : null;
		if (target) {
			if (emitToUser(target, msg)) return true;
			// offline recipient: persist to their mailbox for delivery on next login
			var offlineMsg = JSON.parse(JSON.stringify(msg));
			offlineMsg.time = 7 * 24 * 3600 * 1000; // mail must outlive the 60s live challenge window
			persistMail(target, uid, offlineMsg);
			return true;
		}
		var room = socketZone.get(socket);
		if (room) socket.to(room).emit("message", msg);
	});

	socket.on("disconnect", function () {
		queue.delete(uid);
		var oldRoom = socketZone.get(socket);
		if (oldRoom) socket.to(oldRoom).emit("playerLeft", uid);
		socketZone.delete(socket);
		removeUidSocket(uid, socket);
		removeWorldSocket(worldId, socket);
		console.log("[relay] -uid=" + uid + " world=" + worldId + " (world pop: " + worldCount(worldId) + ")");
	});
}

function persistMail(targetUid, fromUid, msg) {
	if (!adminDb) {
		console.log("[relay] mail for offline uid=" + targetUid + " (admin disabled — would persist): " + JSON.stringify(msg).slice(0, 400));
		return;
	}
	adminDb.collection("mail").doc(targetUid).collection("items").add({
		payload: msg,
		from: fromUid,
		createdAt: Date.now()
	}).then(function () {
		console.log("[relay] mail persisted for offline uid=" + targetUid + " from=" + fromUid);
	}, function (e) {
		console.error("[relay] mail persist failed for uid=" + targetUid + ": " + e);
	});
}

server.listen(PORT, function () {
	console.log("[relay] listening on port " + PORT +
		" | worlds: 1-" + WORLD_COUNT + " (capacity " + WORLD_CAPACITY + ")" +
		" | auth: " + (ALLOW_UNVERIFIED ? "DISABLED (dev only!)" : "Firebase ID token"));
	// warm the leaderboard cache in the background
	refreshLeaderboard();
	setInterval(refreshLeaderboard, LEADERBOARD_TTL).unref();
});

process.on("SIGTERM", function () {
	console.log("[relay] SIGTERM received, shutting down");
	io.close();
	server.close(function () { process.exit(0); });
	setTimeout(function () { process.exit(0); }, 5000).unref();
});
process.on("SIGINT", function () {
	io.close();
	server.close(function () { process.exit(0); });
	setTimeout(function () { process.exit(0); }, 5000).unref();
});
