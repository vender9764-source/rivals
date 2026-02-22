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
HTML_PATH  = os.path.join(SCRIPT_DIR, "rivals.html")

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
lock    = threading.Lock()
players = {}   # pid -> {name, team, x, y, angle, hp, kills, deaths, score, gun, ammo, dead, coins}
next_pid = 1

def make_player(pid, name, team, skin):
    x = 100 if team == "red" else 1300
    y = 400
    return dict(
        pid=pid, name=name[:12], team=team, skin=skin,
        x=x, y=y, angle=0.0,
        hp=3, maxHp=3, dead=False, respTimer=0,
        kills=0, deaths=0, score=0, coins=0,
        gun="pistol", ammo=12,
        reloading=False,
        vx=0, vy=0,
        shield=0,
    )

def broadcast(msg_dict, exclude=None):
    text = json.dumps(msg_dict)
    dead_pids = []
    for pid, info in list(clients.items()):
        if pid == exclude:
            continue
        if not ws_send(info["conn"], text):
            dead_pids.append(pid)
    for pid in dead_pids:
        remove_client(pid)

clients = {}   # pid -> {conn, thread}

def remove_client(pid):
    with lock:
        clients.pop(pid, None)
        p = players.pop(pid, None)
    if p:
        broadcast({"type": "playerLeft", "pid": pid})
        print(f"[SERVER] {p['name']} disconnected")

# ──────────────────────────────────────────────
# Shop prices
# ──────────────────────────────────────────────
SHOP = {
    "smg":    {"price": 150, "name": "SMG"},
    "shotgun":{"price": 200, "name": "SHOTGUN"},
    "assault":{"price": 250, "name": "ASSAULT"},
    "sniper": {"price": 350, "name": "SNIPER"},
}

GUN_AMMO = {"pistol":12,"smg":30,"shotgun":6,"assault":20,"sniper":5}

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

        if mtype == "join":
            name = (msg.get("name") or "PLAYER").strip().upper()[:12] or "PLAYER"
            team = msg.get("team", "red")
            skin = msg.get("skin", "phantom")
            with lock:
                p = make_player(pid, name, team, skin)
                players[pid] = p
            # Send this player their pid + current state
            ws_send(conn, json.dumps({
                "type": "welcome",
                "pid": pid,
                "players": {str(k): v for k, v in players.items()},
                "shop": SHOP,
            }))
            broadcast({"type": "playerJoined", "player": p}, exclude=pid)
            print(f"[SERVER] {name} joined as {team}")

        elif mtype == "state":
            # Player sends their position/angle
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
                       "angle":p["angle"],"vx":p["vx"],"vy":p["vy"]}, exclude=pid)

        elif mtype == "shoot":
            broadcast({"type":"shoot","pid":pid,
                       "x":msg.get("x",0),"y":msg.get("y",0),
                       "angle":msg.get("angle",0),
                       "gun":msg.get("gun","pistol")}, exclude=pid)
            with lock:
                p = players.get(pid)
                if p:
                    p["ammo"] = max(0, p["ammo"] - 1)

        elif mtype == "hit":
            target_pid = msg.get("target")
            dmg = int(msg.get("dmg", 1))
            with lock:
                attacker = players.get(pid)
                target   = players.get(target_pid)
                if attacker and target and not target["dead"]:
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
                        attacker["coins"] += 50
                        kill_info = {
                            "type": "kill",
                            "killer_pid": pid,
                            "victim_pid": target_pid,
                            "killer_name": attacker["name"],
                            "victim_name": target["name"],
                            "killer_team": attacker["team"],
                            "attacker_kills": attacker["kills"],
                            "attacker_score": attacker["score"],
                            "attacker_coins": attacker["coins"],
                            "victim_deaths": target["deaths"],
                        }
                        broadcast(kill_info)
                        # Schedule respawn
                        def do_respawn(tp, tpid):
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
                            broadcast({"type":"respawn","pid":tpid})
                        threading.Thread(target=do_respawn, args=(target, target_pid), daemon=True).start()
                    else:
                        broadcast({"type":"damaged","pid":target_pid,
                                   "hp":target["hp"],"shield":target["shield"]})

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
                p = players.get(pid)
                item = SHOP.get(gun_id)
                if p and item and p["coins"] >= item["price"]:
                    p["coins"] -= item["price"]
                    p["gun"] = gun_id
                    p["ammo"] = GUN_AMMO.get(gun_id, 12)
                    ws_send(conn, json.dumps({
                        "type": "shopResult",
                        "success": True,
                        "gun": gun_id,
                        "ammo": p["ammo"],
                        "coins": p["coins"],
                        "msg": f"Bought {item['name']}!"
                    }))
                elif p:
                    ws_send(conn, json.dumps({
                        "type": "shopResult",
                        "success": False,
                        "coins": p["coins"],
                        "msg": f"Need {item['price'] if item else '?'} coins"
                    }))

        elif mtype == "chat":
            txt = str(msg.get("text",""))[:80]
            with lock:
                p = players.get(pid)
                name = p["name"] if p else "?"
            broadcast({"type":"chat","pid":pid,"name":name,"text":txt})

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
