#!/usr/bin/env python3
"""
RIVALS Multiplayer Server
Pure Python — no external dependencies needed.
Serves the game HTML and handles real-time WebSocket multiplayer.

Run:  python rivals_server.py
Then open:  http://localhost:7373
Deploy to Railway: set start command to 'python rivals_server.py'
"""
import os, sys, json, threading, time, hashlib, base64, struct, socket
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import defaultdict

PORT  = int(os.environ.get("PORT", 7373))
HOST  = "0.0.0.0"

# ──────────────────────────────────────────────
# Read the game HTML from disk (same folder)
# ──────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Try multiple filename variants
for _fn in ["rivals.html","rivals_game.html","rivals__14_.html","game.html"]:
    _candidate = os.path.join(SCRIPT_DIR, _fn)
    if os.path.exists(_candidate):
        HTML_PATH = _candidate
        break
else:
    HTML_PATH = os.path.join(SCRIPT_DIR, "rivals.html")

def load_html():
    if os.path.exists(HTML_PATH):
        with open(HTML_PATH, "rb") as f:
            return f.read()
    return b"<h1>rivals.html not found next to server</h1>"

# ──────────────────────────────────────────────
# Minimal RFC-6455 WebSocket helpers
# ──────────────────────────────────────────────
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

def ws_handshake(conn, key):
    accept = base64.b64encode(
        hashlib.sha1((key + WS_MAGIC).encode()).digest()
    ).decode()
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    )
    conn.sendall(resp.encode())

def ws_recv(conn):
    """Read one WebSocket frame; return text payload or None on close."""
    try:
        hdr = b""
        while len(hdr) < 2:
            chunk = conn.recv(2 - len(hdr))
            if not chunk:
                return None
            hdr += chunk
        fin  = (hdr[0] & 0x80) != 0
        opcode = hdr[0] & 0x0F
        masked = (hdr[1] & 0x80) != 0
        plen  = hdr[1] & 0x7F
        if plen == 126:
            plen = struct.unpack(">H", conn.recv(2))[0]
        elif plen == 127:
            plen = struct.unpack(">Q", conn.recv(8))[0]
        mask  = conn.recv(4) if masked else b"\x00\x00\x00\x00"
        data  = b""
        while len(data) < plen:
            chunk = conn.recv(plen - len(data))
            if not chunk:
                return None
            data += chunk
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode == 8:   # close
            return None
        if opcode == 1:   # text
            return data.decode("utf-8", errors="replace")
        return ""         # ping/pong/binary → ignore
    except Exception:
        return None

def ws_send(conn, text):
    """Send a WebSocket text frame."""
    try:
        payload = text.encode("utf-8")
        n = len(payload)
        if n < 126:
            hdr = bytes([0x81, n])
        elif n < 65536:
            hdr = struct.pack(">BBH", 0x81, 126, n)
        else:
            hdr = struct.pack(">BBQ", 0x81, 127, n)
        conn.sendall(hdr + payload)
        return True
    except Exception:
        return False

# ──────────────────────────────────────────────
# Game state
# ──────────────────────────────────────────────
lock     = threading.Lock()
players  = {}    # pid -> player dict
next_pid = 1

# ── Room / Lobby system ───────────────────────
# rooms[code] = {
#   "code": str, "host_pid": int,
#   "state": "lobby" | "ingame" | "ended",
#   "players": set of pids,
#   "kill_goal": int,          # first to X kills wins
#   "round_timer": float,      # epoch when round ends (0 = no timer)
# }
rooms    = {}    # code -> room dict
pid_room = {}    # pid -> room code

KILL_GOAL    = 10    # first to 10 kills wins a round
ROUND_SECS   = 180   # 3-minute hard cap per round

import random, string
def _gen_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if code not in rooms:
            return code

def make_player(pid, name, team, skin):
    x = 100 if team == "red" else 1300
    y = 400
    return dict(
        pid=pid, name=name[:12], team=team, skin=skin,
        x=x, y=y, angle=0.0,
        hp=3, maxHp=3, dead=False, respTimer=0,
        kills=0, deaths=0, score=0,
        # ── Persistent across rounds (saved on round end) ──
        coins=0,
        owned_skins=[],
        active_skins={},
        # ── Per-session state ──────────────────────────────
        gun="pistol", ammo=12,
        reloading=False,
        vx=0, vy=0,
        shield=0,
        sword_cd=0.0,
    )

def broadcast(msg_dict, exclude=None, room_code=None):
    """Broadcast to all clients, optionally filtered to a room."""
    text = json.dumps(msg_dict)
    dead_pids = []
    for pid, info in list(clients.items()):
        if pid == exclude:
            continue
        if room_code is not None and pid_room.get(pid) != room_code:
            continue
        if not ws_send(info["conn"], text):
            dead_pids.append(pid)
    for pid in dead_pids:
        remove_client(pid)

clients = {}   # pid -> {conn, thread}

def _player_save_data(p):
    """Return only the data that persists between rounds."""
    return {
        "coins":       p["coins"],
        "owned_skins": p["owned_skins"],
        "active_skins":p["active_skins"],
    }

def end_round(room_code, winner_pid):
    """Called when someone hits the kill goal. Save coins, reset round, send everyone to menu."""
    with lock:
        room = rooms.get(room_code)
        if not room or room["state"] != "ingame":
            return
        room["state"] = "ended"

        winner = players.get(winner_pid)
        results = []
        saved   = {}   # pid -> saved persistent data

        for pid in list(room["players"]):
            p = players.get(pid)
            if not p:
                continue
            saved[pid] = _player_save_data(p)
            results.append({
                "pid":    pid,
                "name":   p["name"],
                "team":   p["team"],
                "kills":  p["kills"],
                "deaths": p["deaths"],
                "score":  p["score"],
                "coins":  p["coins"],
            })

        results.sort(key=lambda r: r["score"], reverse=True)

    # Tell everyone: round over, here are the results, return to menu
    broadcast({
        "type":      "roundEnd",
        "winner_pid": winner_pid,
        "winner_name": winner["name"] if winner else "?",
        "results":   results,
        # Each player's client should read their own pid's coins from results
    }, room_code=room_code)

    print(f"[SERVER] Round ended in room {room_code} — winner: {winner['name'] if winner else '?'}")

    # After a short delay reset the room back to lobby so players can rematch
    def _reset_room():
        time.sleep(8)   # show scoreboard for 8 s
        with lock:
            room = rooms.get(room_code)
            if not room:
                return
            room["state"] = "lobby"
            room["round_timer"] = 0
            for pid in list(room["players"]):
                p = players.get(pid)
                sv = saved.get(pid, {})
                if p:
                    # Reset combat stats but KEEP coins / skins
                    p.update(dict(
                        x=100 if p["team"]=="red" else 1300, y=400,
                        angle=0.0, hp=3, maxHp=3, dead=False, respTimer=0,
                        kills=0, deaths=0, score=0,
                        gun="pistol", ammo=12, reloading=False,
                        vx=0, vy=0, shield=0, sword_cd=0.0,
                        # Restore persistent data
                        coins=sv.get("coins", p["coins"]),
                        owned_skins=sv.get("owned_skins", p["owned_skins"]),
                        active_skins=sv.get("active_skins", p["active_skins"]),
                    ))
        broadcast({"type": "returnToLobby", "room": room_code}, room_code=room_code)
        print(f"[SERVER] Room {room_code} reset to lobby")

    threading.Thread(target=_reset_room, daemon=True).start()

def remove_client(pid):
    with lock:
        clients.pop(pid, None)
        p = players.pop(pid, None)
        code = pid_room.pop(pid, None)
        if code and code in rooms:
            rooms[code]["players"].discard(pid)
            if not rooms[code]["players"]:
                rooms.pop(code, None)
    if p:
        if code:
            broadcast({"type": "playerLeft", "pid": pid}, room_code=code)
        print(f"[SERVER] {p['name']} disconnected")

# ──────────────────────────────────────────────
# Shop prices
# ──────────────────────────────────────────────
SHOP = {
    # ── Weapons ──────────────────────────────────────────────────────────
    "smg":     {"price": 15,  "name": "SMG",           "category": "weapon"},
    "shotgun": {"price": 20,  "name": "SHOTGUN",        "category": "weapon"},
    "assault": {"price": 25,  "name": "ASSAULT RIFLE",  "category": "weapon"},
    "sniper":  {"price": 35,  "name": "SNIPER RIFLE",   "category": "weapon"},
    "sword":   {"price": 10,  "name": "SWORD",          "category": "weapon"},

    # ── Weapon Skins ─────────────────────────────────────────────────────
    # Pistol skins
    "skin_pistol_golden":    {"price": 20, "name": "Golden Pistol",     "category": "skin", "weapon": "pistol",  "color": "#FFD700"},
    "skin_pistol_ice":       {"price": 20, "name": "Ice Pistol",        "category": "skin", "weapon": "pistol",  "color": "#A0E8FF"},
    "skin_pistol_inferno":   {"price": 25, "name": "Inferno Pistol",    "category": "skin", "weapon": "pistol",  "color": "#FF4500"},
    # SMG skins
    "skin_smg_golden":       {"price": 25, "name": "Golden SMG",        "category": "skin", "weapon": "smg",     "color": "#FFD700"},
    "skin_smg_neon":         {"price": 25, "name": "Neon SMG",          "category": "skin", "weapon": "smg",     "color": "#39FF14"},
    "skin_smg_void":         {"price": 30, "name": "Void SMG",          "category": "skin", "weapon": "smg",     "color": "#6A0DAD"},
    # Shotgun skins
    "skin_shotgun_golden":   {"price": 30, "name": "Golden Shotgun",    "category": "skin", "weapon": "shotgun", "color": "#FFD700"},
    "skin_shotgun_rusty":    {"price": 20, "name": "Rusty Shotgun",     "category": "skin", "weapon": "shotgun", "color": "#8B4513"},
    "skin_shotgun_ice":      {"price": 30, "name": "Ice Shotgun",       "category": "skin", "weapon": "shotgun", "color": "#A0E8FF"},
    # Assault skins
    "skin_assault_golden":   {"price": 35, "name": "Golden Assault",    "category": "skin", "weapon": "assault", "color": "#FFD700"},
    "skin_assault_camo":     {"price": 30, "name": "Camo Assault",      "category": "skin", "weapon": "assault", "color": "#4B5320"},
    "skin_assault_chrome":   {"price": 40, "name": "Chrome Assault",    "category": "skin", "weapon": "assault", "color": "#C0C0C0"},
    # Sniper skins
    "skin_sniper_golden":    {"price": 50, "name": "Golden Sniper",     "category": "skin", "weapon": "sniper",  "color": "#FFD700"},
    "skin_sniper_void":      {"price": 45, "name": "Void Sniper",       "category": "skin", "weapon": "sniper",  "color": "#6A0DAD"},
    "skin_sniper_dragon":    {"price": 60, "name": "Dragon Sniper",     "category": "skin", "weapon": "sniper",  "color": "#FF6B00"},
    # Sword skins
    "skin_sword_golden":     {"price": 30, "name": "Golden Sword",      "category": "skin", "weapon": "sword",   "color": "#FFD700"},
    "skin_sword_shadow":     {"price": 35, "name": "Shadow Blade",      "category": "skin", "weapon": "sword",   "color": "#1A1A2E"},
    "skin_sword_ice":        {"price": 35, "name": "Frostblade",        "category": "skin", "weapon": "sword",   "color": "#A0E8FF"},
    "skin_sword_inferno":    {"price": 40, "name": "Inferno Blade",     "category": "skin", "weapon": "sword",   "color": "#FF4500"},
    "skin_sword_electric":   {"price": 40, "name": "Electric Blade",    "category": "skin", "weapon": "sword",   "color": "#FFFF00"},
}

GUN_AMMO   = {"pistol": 12, "smg": 30, "shotgun": 6, "assault": 20, "sniper": 5}
SWORD_COOLDOWN = 7.0   # seconds
SWORD_DMG      = 2     # sword deals 2 HP per swing

def handle_client(pid, conn):
    while True:
        raw = ws_recv(conn)
        if raw is None:
            break
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue

        mtype = msg.get("type")

        if mtype == "createRoom":
            # Player creates a room and enters the LOBBY (not the game yet)
            name = (msg.get("name") or "PLAYER").strip().upper()[:12] or "PLAYER"
            skin = msg.get("skin", "phantom")
            team = msg.get("team", "red")
            with lock:
                code = _gen_code()
                p = make_player(pid, name, team, skin)
                players[pid] = p
                rooms[code]  = {
                    "code": code, "host_pid": pid,
                    "state": "lobby",
                    "players": {pid},
                    "kill_goal": KILL_GOAL,
                    "round_timer": 0,
                }
                pid_room[pid] = code
            ws_send(conn, json.dumps({
                "type": "roomCreated",
                "pid": pid,
                "code": code,
                "player": p,
                "shop": SHOP,
                "kill_goal": KILL_GOAL,
            }))
            print(f"[SERVER] {name} created room {code}")

        elif mtype == "joinRoom":
            # Player joins an existing room by code — lands in LOBBY
            name = (msg.get("name") or "PLAYER").strip().upper()[:12] or "PLAYER"
            skin = msg.get("skin", "phantom")
            team = msg.get("team", "blue")
            code = str(msg.get("code", "")).upper().strip()
            with lock:
                room = rooms.get(code)
                if not room:
                    ws_send(conn, json.dumps({"type": "joinError", "msg": f"Room '{code}' not found"}))
                    continue
                if room["state"] == "ended":
                    ws_send(conn, json.dumps({"type": "joinError", "msg": "Round just ended, wait a moment"}))
                    continue
                p = make_player(pid, name, team, skin)
                players[pid] = p
                room["players"].add(pid)
                pid_room[pid] = code
                lobby_players = {str(k): players[k] for k in room["players"]}

            ws_send(conn, json.dumps({
                "type": "roomJoined",
                "pid": pid,
                "code": code,
                "state": room["state"],
                "player": p,
                "players": lobby_players,
                "shop": SHOP,
                "kill_goal": KILL_GOAL,
            }))
            broadcast({"type": "playerJoined", "player": p}, exclude=pid, room_code=code)
            print(f"[SERVER] {name} joined room {code} (state={room['state']})")

        elif mtype == "startGame":
            # Host starts the round — everyone in lobby moves to ingame
            code = pid_room.get(pid)
            with lock:
                room = rooms.get(code)
                if not room or room["host_pid"] != pid or room["state"] != "lobby":
                    continue
                room["state"]       = "ingame"
                room["round_timer"] = time.time() + ROUND_SECS
            broadcast({
                "type":       "gameStarted",
                "kill_goal":  KILL_GOAL,
                "round_ends": room["round_timer"],
                "players":    {str(k): players[k] for k in room["players"]},
            }, room_code=code)
            print(f"[SERVER] Room {code} — game started")

            # Hard-cap timer thread
            cap_time = room["round_timer"]
            def _timer_end(rc, cap):
                time.sleep(ROUND_SECS + 1)
                with lock:
                    r = rooms.get(rc)
                    if not r or r["state"] != "ingame":
                        return
                    # find leader
                    best_pid  = None
                    best_kills = -1
                    for pp in r["players"]:
                        pl = players.get(pp)
                        if pl and pl["kills"] > best_kills:
                            best_kills = pl["kills"]
                            best_pid   = pp
                end_round(rc, best_pid)
            threading.Thread(target=_timer_end, args=(code, cap_time), daemon=True).start()

        elif mtype == "join":
            # Legacy join — treat as createRoom for backwards compat
            name = (msg.get("name") or "PLAYER").strip().upper()[:12] or "PLAYER"
            team = msg.get("team", "red")
            skin = msg.get("skin", "phantom")
            with lock:
                code = _gen_code()
                p = make_player(pid, name, team, skin)
                players[pid] = p
                rooms[code]  = {
                    "code": code, "host_pid": pid,
                    "state": "lobby",
                    "players": {pid},
                    "kill_goal": KILL_GOAL,
                    "round_timer": 0,
                }
                pid_room[pid] = code
            ws_send(conn, json.dumps({
                "type": "welcome",
                "pid": pid,
                "code": code,
                "players": {str(k): players[k] for k in rooms[code]["players"]},
                "shop": SHOP,
            }))
            broadcast({"type": "playerJoined", "player": p}, exclude=pid, room_code=code)
            print(f"[SERVER] {name} joined via legacy join → room {code}")

        elif mtype == "state":
            # Player sends their position/angle
            code = pid_room.get(pid)
            with lock:
                p = players.get(pid)
                if p and not p["dead"]:
                    p["x"] = msg.get("x", p["x"])
                    p["y"] = msg.get("y", p["y"])
                    p["angle"] = msg.get("angle", p["angle"])
                    p["vx"] = msg.get("vx", 0)
                    p["vy"] = msg.get("vy", 0)
            broadcast({"type":"state","pid":pid,
                       "x":p["x"],"y":p["y"],
                       "angle":p["angle"],"vx":p["vx"],"vy":p["vy"]},
                      exclude=pid, room_code=code)

        elif mtype == "shoot":
            code = pid_room.get(pid)
            broadcast({"type":"shoot","pid":pid,
                       "x":msg.get("x",0),"y":msg.get("y",0),
                       "angle":msg.get("angle",0),
                       "gun":msg.get("gun","pistol")}, exclude=pid, room_code=code)
            with lock:
                p = players.get(pid)
                if p:
                    p["ammo"] = max(0, p["ammo"] - 1)

        elif mtype == "hit":
            target_pid = msg.get("target")
            is_sword = msg.get("isSword", False)
            dmg = SWORD_DMG if is_sword else int(msg.get("dmg", 1))
            code = pid_room.get(pid)
            round_over = False
            round_winner = None
            with lock:
                attacker = players.get(pid)
                target   = players.get(target_pid)
                room     = rooms.get(code) if code else None
                if attacker and target and not target["dead"] and room and room["state"] == "ingame":
                    if target["shield"] > 0:
                        target["shield"] = max(0, target["shield"] - dmg)
                    else:
                        target["hp"] = max(0, target["hp"] - dmg)
                    if target["hp"] <= 0 and not target["dead"]:
                        target["dead"] = True
                        target["respTimer"] = 3.0
                        target["deaths"] += 1
                        attacker["kills"] += 1
                        attacker["score"] += 100
                        attacker["coins"] += 5
                        gun_used   = attacker["gun"]
                        skin_id    = attacker["active_skins"].get(gun_used)
                        skin_color = SHOP[skin_id]["color"] if skin_id and skin_id in SHOP else None
                        kill_info  = {
                            "type": "kill",
                            "killer_pid": pid,
                            "victim_pid": target_pid,
                            "killer_name": attacker["name"],
                            "victim_name": target["name"],
                            "killer_team": attacker["team"],
                            "weapon": gun_used,
                            "skinColor": skin_color,
                            "attacker_kills": attacker["kills"],
                            "attacker_score": attacker["score"],
                            "attacker_coins": attacker["coins"],
                            "victim_deaths": target["deaths"],
                        }
                        # Check kill goal
                        if attacker["kills"] >= room["kill_goal"]:
                            round_over   = True
                            round_winner = pid
                        broadcast(kill_info, room_code=code)
                        if not round_over:
                            def do_respawn(tp, tpid, rc):
                                time.sleep(3)
                                with lock:
                                    p2 = players.get(tpid)
                                    if p2:
                                        p2["dead"] = False
                                        p2["hp"] = p2["maxHp"]
                                        p2["x"] = 100 if p2["team"]=="red" else 1300
                                        p2["y"] = 400
                                        p2["gun"] = "pistol"
                                        p2["ammo"] = 12
                                        p2["shield"] = 0
                                        p2["sword_cd"] = 0.0
                                broadcast({"type":"respawn","pid":tpid}, room_code=rc)
                            threading.Thread(target=do_respawn, args=(target, target_pid, code), daemon=True).start()
                    else:
                        broadcast({"type":"damaged","pid":target_pid,
                                   "hp":target["hp"],"shield":target["shield"]},
                                  room_code=code)
            if round_over:
                end_round(code, round_winner)

        elif mtype == "reload":
            with lock:
                p = players.get(pid)
                if p:
                    p["ammo"] = GUN_AMMO.get(p["gun"], 12)
                    p["reloading"] = False
            ws_send(conn, json.dumps({"type":"reloaded","ammo":GUN_AMMO.get(p["gun"],12)}))

        elif mtype == "buyGun":
            gun_id = msg.get("gun")
            with lock:
                p    = players.get(pid)
                item = SHOP.get(gun_id)
                if p and item and item["category"] == "weapon" and p["coins"] >= item["price"]:
                    p["coins"] -= item["price"]
                    p["gun"]    = gun_id
                    p["ammo"]   = GUN_AMMO.get(gun_id, 0)  # sword has 0 ammo
                    ws_send(conn, json.dumps({
                        "type": "shopResult", "success": True,
                        "gun": gun_id, "ammo": p["ammo"],
                        "coins": p["coins"],
                        "msg": f"Bought {item['name']}!"
                    }))
                elif p:
                    ws_send(conn, json.dumps({
                        "type": "shopResult", "success": False,
                        "coins": p["coins"],
                        "msg": f"Need {item['price'] if item else '?'} coins"
                    }))

        elif mtype == "buySkin":
            skin_id = msg.get("skin_id")
            with lock:
                p    = players.get(pid)
                item = SHOP.get(skin_id)
                if p and item and item["category"] == "skin":
                    if skin_id in p["owned_skins"]:
                        # Already owned — just equip it
                        p["active_skins"][item["weapon"]] = skin_id
                        ws_send(conn, json.dumps({
                            "type": "skinResult", "success": True,
                            "skin_id": skin_id, "weapon": item["weapon"],
                            "color": item["color"], "coins": p["coins"],
                            "msg": f"Equipped {item['name']}!"
                        }))
                    elif p["coins"] >= item["price"]:
                        p["coins"] -= item["price"]
                        p["owned_skins"].append(skin_id)
                        p["active_skins"][item["weapon"]] = skin_id
                        ws_send(conn, json.dumps({
                            "type": "skinResult", "success": True,
                            "skin_id": skin_id, "weapon": item["weapon"],
                            "color": item["color"], "coins": p["coins"],
                            "msg": f"Bought & equipped {item['name']}!"
                        }))
                    else:
                        ws_send(conn, json.dumps({
                            "type": "skinResult", "success": False,
                            "coins": p["coins"],
                            "msg": f"Need {item['price']} coins for {item['name']}"
                        }))

        elif mtype == "swordSwing":
            now  = time.time()
            code = pid_room.get(pid)
            with lock:
                p = players.get(pid)
                allowed   = False
                remaining = 0.0
                if p and p["gun"] == "sword" and not p["dead"]:
                    if now >= p["sword_cd"]:
                        p["sword_cd"] = now + SWORD_COOLDOWN
                        allowed = True
                    else:
                        remaining = round(p["sword_cd"] - now, 2)
            if allowed:
                with lock:
                    p = players.get(pid)
                    skin_color = None
                    if p:
                        sid = p["active_skins"].get("sword")
                        if sid and sid in SHOP:
                            skin_color = SHOP[sid]["color"]
                broadcast({
                    "type": "swordSwing", "pid": pid,
                    "x": msg.get("x", 0), "y": msg.get("y", 0),
                    "angle": msg.get("angle", 0),
                    "skinColor": skin_color,
                    "cooldown": SWORD_COOLDOWN,
                }, room_code=code)
                ws_send(conn, json.dumps({"type": "swordAllowed", "cooldown": SWORD_COOLDOWN}))
            else:
                ws_send(conn, json.dumps({
                    "type": "swordDenied",
                    "remaining": remaining,
                    "msg": f"Sword on cooldown! {remaining}s left"
                }))

        elif mtype == "chat":
            txt = str(msg.get("text",""))[:80]
            code = pid_room.get(pid)
            with lock:
                p = players.get(pid)
                name = p["name"] if p else "?"
            broadcast({"type":"chat","pid":pid,"name":name,"text":txt}, room_code=code)

    remove_client(pid)

# ──────────────────────────────────────────────
# HTTP + WebSocket handler
# ──────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence HTTP logs

    def do_GET(self):
        # WebSocket upgrade?
        upgrade = self.headers.get("Upgrade","").lower()
        if upgrade == "websocket":
            key = self.headers.get("Sec-WebSocket-Key","")
            self.send_response(101)
            self.end_headers()
            # Manually do handshake on raw socket
            conn = self.connection
            ws_handshake(conn, key)
            global next_pid
            with lock:
                pid = next_pid
                next_pid += 1
                clients[pid] = {"conn": conn}
            ws_send(conn, json.dumps({"type":"connected","pid":pid}))
            t = threading.Thread(target=handle_client, args=(pid, conn), daemon=True)
            clients[pid]["thread"] = t
            t.start()
            t.join()
            return

        # Serve HTML
        html = load_html()
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length", len(html))
        self.end_headers()
        self.wfile.write(html)

if __name__ == "__main__":
    server = HTTPServer((HOST, PORT), Handler)
    env = os.environ.get("RAILWAY_ENVIRONMENT")
    print(f"""
  ╔══════════════════════════════════════╗
  ║   RIVALS  —  Multiplayer Server     ║
  ╠══════════════════════════════════════╣
  ║  http://{HOST}:{PORT:<27}║
  ║  {'Railway mode' if env else 'Local — open http://localhost:'+str(PORT):<36}║
  ╚══════════════════════════════════════╝
""", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[SERVER] Shutting down...")
        server.shutdown()
