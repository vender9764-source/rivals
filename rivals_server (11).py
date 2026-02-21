#!/usr/bin/env python3
"""RIVALS Multiplayer Server — python3 rivals.py"""
import socket, threading, hashlib, base64, json, time, sys, os
import webbrowser, uuid, random, struct, math
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

# ═══════════════════════════════════════════════════════
#  WEBSOCKET HELPERS
# ═══════════════════════════════════════════════════════
WS_MAGIC = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

def ws_accept(key):
    raw = hashlib.sha1((key.strip() + WS_MAGIC).encode()).digest()
    return base64.b64encode(raw).decode()

def ws_send(wfile, msg):
    if isinstance(msg, dict): msg = json.dumps(msg)
    data = msg.encode('utf-8') if isinstance(msg, str) else msg
    n = len(data)
    if n <= 125:   hdr = bytes([0x81, n])
    elif n <= 65535: hdr = struct.pack('!BBH', 0x81, 126, n)
    else:          hdr = struct.pack('!BBQ', 0x81, 127, n)
    try:
        wfile.write(hdr + data)
        wfile.flush()
        return True
    except: return False

def ws_recv_frame(rfile):
    try:
        h = rfile.read(2)
        if len(h) < 2: return None, None
        opcode = h[0] & 0x0f
        masked = (h[1] & 0x80) != 0
        length = h[1] & 0x7f
        if length == 126:   length = struct.unpack('!H', rfile.read(2))[0]
        elif length == 127: length = struct.unpack('!Q', rfile.read(8))[0]
        mask = rfile.read(4) if masked else b'\x00\x00\x00\x00'
        payload = bytearray(rfile.read(length))
        if masked:
            for i in range(len(payload)): payload[i] ^= mask[i % 4]
        if opcode == 8: return None, 8   # close
        if opcode == 9: return b'', 9    # ping
        return bytes(payload), opcode
    except: return None, None

# ═══════════════════════════════════════════════════════
#  GAME STATE
# ═══════════════════════════════════════════════════════
lock = threading.Lock()

lobby = {
    'players': {},   # id → player_info
    'phase':  'lobby',   # lobby | playing | ended
    'config': {'mode': 'tdm', 'size': '2v2', 'target': 30},
    'scores': {'red': 0, 'blue': 0},
    'map_seed': 0,
    'host_id': None,
}

SIZE_MAP = {'1v1': 1, '2v2': 2, '3v3': 3, '4v4': 4}

def new_player(pid, name, wfile):
    return {
        'id': pid, 'name': name[:12].upper() or 'PLAYER',
        'team': None, 'skin': 'phantom', 'gun': 'pistol',
        'ready': False, 'wfile': wfile,
        'x': 700, 'y': 400, 'angle': 0,
        'hp': 3, 'dead': False,
        'kills': 0, 'deaths': 0, 'score': 0,
        'streak': 0,
        'ammo': 12, 'reloading': False, 'gunId': 'pistol',
        'hasFlag': False, 'attacking': False,
        'vx': 0, 'vy': 0,
        'bot': False,
    }

def broadcast(msg, exclude=None):
    data = json.dumps(msg) if isinstance(msg, dict) else msg
    dead = []
    for pid, p in lobby['players'].items():
        if pid == exclude: continue
        if not ws_send(p['wfile'], data):
            dead.append(pid)
    for pid in dead:
        lobby['players'].pop(pid, None)

def send_to(pid, msg):
    p = lobby['players'].get(pid)
    if p: ws_send(p['wfile'], msg)

def lobby_snapshot():
    players_out = []
    for pid, p in lobby['players'].items():
        players_out.append({
            'id': pid, 'name': p['name'], 'team': p['team'],
            'skin': p['skin'], 'ready': p['ready'],
            'kills': p['kills'], 'deaths': p['deaths'], 'score': p['score'],
            'bot': p.get('bot', False),
            'gun': p.get('gun', 'pistol'),
        })
    return {
        'type': 'lobby',
        'players': players_out,
        'config': lobby['config'],
        'host_id': lobby['host_id'],
        'phase': lobby['phase'],
    }

def check_win():
    m = lobby['config']
    t = m['target']
    mode = m['mode']
    if mode == 'tdm':
        if lobby['scores']['red'] >= t:   end_round('RED TEAM', 'red')
        elif lobby['scores']['blue'] >= t: end_round('BLUE TEAM', 'blue')
    elif mode == 'ffa':
        for p in lobby['players'].values():
            if p['kills'] >= t:
                end_round(p['name'] + ' WINS', 'ffa')
                break
    elif mode == 'koth':
        pass  # handled client-side broadcast
    elif mode == 'ctf':
        rs = sum(1 for p in lobby['players'].values() if p.get('ctf_caps',0) and p['team']=='red')
        bs = sum(1 for p in lobby['players'].values() if p.get('ctf_caps',0) and p['team']=='blue')

def end_round(winner_text, winner_team):
    if lobby['phase'] != 'playing': return
    lobby['phase'] = 'ended'
    snap = lobby_snapshot()
    broadcast({'type': 'end', 'winner_text': winner_text, 'winner_team': winner_team,
               'scores': lobby['scores'], 'players': snap['players']})

# ═══════════════════════════════════════════════════════
#  MESSAGE HANDLER
# ═══════════════════════════════════════════════════════
def handle(pid, raw):
    try: msg = json.loads(raw)
    except: return
    t = msg.get('type')

    with lock:
        p = lobby['players'].get(pid)
        if not p: return

        if t == 'set_name':
            p['name'] = (msg.get('name','')[:12].upper() or 'PLAYER')
            broadcast(lobby_snapshot())

        elif t == 'set_team':
            if lobby['phase'] != 'lobby': return
            team = msg.get('team')
            if team not in ('red','blue','spectator'): return
            size = SIZE_MAP.get(lobby['config']['size'], 2)
            # Count team members
            count = sum(1 for x in lobby['players'].values() if x['team']==team and x['id']!=pid)
            if count >= size: return  # team full
            p['team'] = team
            p['ready'] = False
            broadcast(lobby_snapshot())

        elif t == 'set_skin':
            p['skin'] = msg.get('skin', 'phantom')
            broadcast(lobby_snapshot())

        elif t == 'set_gun':
            p['gun'] = msg.get('gun', 'pistol')
            broadcast(lobby_snapshot())

        elif t == 'set_ready':
            p['ready'] = bool(msg.get('ready'))
            broadcast(lobby_snapshot())

        elif t == 'set_config':
            if pid != lobby['host_id']: return
            if lobby['phase'] != 'lobby': return
            cfg = msg.get('config', {})
            if 'size' in cfg and cfg['size'] in SIZE_MAP:
                lobby['config']['size'] = cfg['size']
                # default targets by size for tdm
                size_targets = {'1v1':10,'2v2':10,'3v3':10,'4v4':30}
                if lobby['config']['mode'] == 'tdm':
                    lobby['config']['target'] = size_targets.get(cfg['size'], 30)
            if 'mode' in cfg:
                lobby['config']['mode'] = cfg['mode']
                targets = {'tdm':30,'ffa':8,'koth':20,'ctf':3,'gun':None,'1v1':10}
                lobby['config']['target'] = targets.get(cfg['mode'], 30)
            if 'size' in cfg and cfg['size'] in SIZE_MAP:
                size_targets = {'1v1':10,'2v2':10,'3v3':10,'4v4':30}
                if lobby['config']['mode'] == 'tdm':
                    lobby['config']['target'] = size_targets.get(cfg.get('size', lobby['config']['size']), 30)
            broadcast(lobby_snapshot())

        elif t == 'start':
            if pid != lobby['host_id']: return
            if lobby['phase'] != 'lobby': return
            # Auto-assign team if player has none
            if p['team'] not in ('red', 'blue'):
                p['team'] = 'red'
            size = SIZE_MAP.get(lobby['config']['size'], 2)
            guns = ['pistol','smg','shotgun','assault','sniper']
            reds  = [x for x in lobby['players'].values() if x['team']=='red' and not x.get('bot')]
            blues = [x for x in lobby['players'].values() if x['team']=='blue' and not x.get('bot')]
            bot_names_r = ['REAPER','BLOODAX','VORTEX','INFERNO']
            bot_names_b = ['SPECTER','NOCTUA','CIPHER','WRAITH']
            while len(reds) < size:
                bid = 'bot_' + str(uuid.uuid4())[:6]
                bot = new_player(bid, bot_names_r[len(reds)%4], None)
                bot['team'] = 'red'; bot['bot'] = True
                bot['gun'] = random.choice(guns)
                lobby['players'][bid] = bot
                reds.append(bot)
            while len(blues) < size:
                bid = 'bot_' + str(uuid.uuid4())[:6]
                bot = new_player(bid, bot_names_b[len(blues)%4], None)
                bot['team'] = 'blue'; bot['bot'] = True
                bot['gun'] = random.choice(guns)
                lobby['players'][bid] = bot
                blues.append(bot)

            lobby['phase'] = 'playing'
            lobby['scores'] = {'red': 0, 'blue': 0}
            lobby['map_seed'] = random.randint(0, 99999)
            for px in lobby['players'].values():
                px['kills'] = 0; px['deaths'] = 0; px['score'] = 0; px['streak'] = 0
            snap = lobby_snapshot()
            broadcast({'type': 'start', 'map_seed': lobby['map_seed'],
                       'config': lobby['config'], 'players': snap['players'],
                       'host_id': lobby['host_id']})

        elif t == 'state':
            # Player broadcasting their position/state
            p['x'] = msg.get('x', p['x'])
            p['y'] = msg.get('y', p['y'])
            p['angle'] = msg.get('angle', p['angle'])
            p['hp'] = msg.get('hp', p['hp'])
            p['dead'] = msg.get('dead', p['dead'])
            p['vx'] = msg.get('vx', 0)
            p['vy'] = msg.get('vy', 0)
            p['gunId'] = msg.get('gunId', p.get('gunId','pistol'))
            p['ammo'] = msg.get('ammo', p.get('ammo',12))
            p['reloading'] = msg.get('reloading', False)
            p['hasFlag'] = msg.get('hasFlag', False)
            p['attacking'] = msg.get('attacking', False)
            # relay to all others
            relay = {'type':'state','id':pid,'x':p['x'],'y':p['y'],
                     'angle':p['angle'],'hp':p['hp'],'dead':p['dead'],
                     'vx':p['vx'],'vy':p['vy'],'gunId':p['gunId'],
                     'ammo':p['ammo'],'reloading':p['reloading'],
                     'hasFlag':p['hasFlag'],'attacking':p['attacking'],
                     'skin':p['skin'],'name':p['name'],'team':p['team']}
            broadcast(relay, exclude=pid)

        elif t == 'kill':
            if lobby['phase'] != 'playing': return
            victim_id = msg.get('victim_id')
            killer_id = msg.get('killer_id', pid)
            victim = lobby['players'].get(victim_id)
            if not victim: return
            # Killer may be a bot (different id from the sending player)
            killer = lobby['players'].get(killer_id, p)
            killer['kills'] += 1; killer['streak'] += 1; killer['score'] += 100
            victim['deaths'] += 1; victim['streak'] = 0; victim['score'] = max(0,victim['score']-25)
            if lobby['config']['mode'] == 'tdm':
                lobby['scores'][killer['team']] = lobby['scores'].get(killer['team'],0) + 1
            broadcast({'type':'kill','killer_id':killer_id,'victim_id':victim_id,
                       'killer_name':killer['name'],'victim_name':victim['name'],
                       'killer_team':killer['team'],'killer_kills':killer['kills'],
                       'scores':lobby['scores'],
                       'streak': killer['streak']})
            check_win()

        elif t == 'koth_tick':
            # host sends koth progress
            lobby['scores']['red'] = msg.get('red', lobby['scores']['red'])
            lobby['scores']['blue'] = msg.get('blue', lobby['scores']['blue'])
            broadcast({'type':'koth_update','scores':lobby['scores'],'players':
                       [{k:v for k,v in x.items() if k!='wfile'} for x in lobby['players'].values()]
                       }, exclude=pid)
            if lobby['scores']['red'] >= lobby['config']['target']:
                end_round('RED TEAM', 'red')
            elif lobby['scores']['blue'] >= lobby['config']['target']:
                end_round('BLUE TEAM', 'blue')

        elif t == 'ctf_cap':
            team = msg.get('team')
            if team in ('red','blue'):
                lobby['scores'][team] = lobby['scores'].get(team,0) + 1
                broadcast({'type':'ctf_cap','team':team,'scores':lobby['scores']})
                if lobby['scores'][team] >= lobby['config']['target']:
                    end_round(team.upper()+' TEAM', team)

        elif t == 'chat':
            text = str(msg.get('text',''))[:80]
            broadcast({'type':'chat','from':p['name'],'team':p['team'],'text':text})

        elif t == 'play_again':
            if pid != lobby['host_id']: return
            # Reset to lobby
            lobby['phase'] = 'lobby'
            lobby['scores'] = {'red':0,'blue':0}
            # Remove bots
            bots = [bid for bid,b in lobby['players'].items() if b.get('bot')]
            for bid in bots: del lobby['players'][bid]
            # Reset players
            for px in lobby['players'].values():
                px['ready'] = False
                px['kills'] = 0; px['deaths'] = 0; px['score'] = 0
            broadcast(lobby_snapshot())

# ═══════════════════════════════════════════════════════
#  HTTP + WEBSOCKET SERVER
# ═══════════════════════════════════════════════════════
HTML_CONTENT = ''  # filled at bottom

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass  # silent

    def do_GET(self):
        upgrade = self.headers.get('Upgrade','').lower()
        if upgrade == 'websocket':
            self._ws_upgrade()
        else:
            body = HTML_CONTENT.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

    def _ws_upgrade(self):
        key = self.headers.get('Sec-WebSocket-Key','')
        accept = ws_accept(key)
        resp = (f'HTTP/1.1 101 Switching Protocols\r\n'
                f'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                f'Sec-WebSocket-Accept: {accept}\r\n\r\n')
        self.wfile.write(resp.encode())
        self.wfile.flush()

        pid = str(uuid.uuid4())[:8]
        with lock:
            p = new_player(pid, 'PLAYER', self.wfile)
            lobby['players'][pid] = p
            if lobby['host_id'] is None or lobby['host_id'] not in lobby['players']:
                lobby['host_id'] = pid

        ws_send(self.wfile, {'type':'welcome','id':pid,
                              'is_host': lobby['host_id']==pid})
        ws_send(self.wfile, lobby_snapshot())

        try:
            while True:
                frame, opcode = ws_recv_frame(self.rfile)
                if frame is None: break
                if opcode == 9:  # ping → pong
                    self.wfile.write(bytes([0x8A, 0]))
                    self.wfile.flush()
                    continue
                if opcode == 8: break
                if frame: handle(pid, frame)
        except: pass
        finally:
            with lock:
                lobby['players'].pop(pid, None)
                if lobby['host_id'] == pid:
                    remaining = [x for x in lobby['players'] if not lobby['players'][x].get('bot')]
                    lobby['host_id'] = remaining[0] if remaining else None
                    if lobby['host_id']:
                        send_to(lobby['host_id'], {'type':'promoted_host'})
                broadcast(lobby_snapshot())

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

# ═══════════════════════════════════════════════════════
#  GET LAN IP
# ═══════════════════════════════════════════════════════
def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except: return '127.0.0.1'

def find_port(start=7373):
    for p in range(start, start+20):
        try:
            s = socket.socket(); s.bind(('',p)); s.close(); return p
        except: continue
    return start


# ═══════════════════════════════════════════════════════
#  EMBED HTML + MAIN
# ═══════════════════════════════════════════════════════
def load_html():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RIVALS</title>
<link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&family=Barlow+Condensed:wght@500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--y:#FFE600;--cy:#00F5FF;--red:#FF3131;--blue:#1E90FF;--green:#22C55E;--purple:#A855F7;--bg:#050508;--surf:#0d0d18;--bdr:#1a1a2e;--txt:#f0f0ff;--muted:#5a5a90}
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden;background:var(--bg);font-family:'Barlow Condensed',sans-serif;color:var(--txt);font-weight:500}
.screen{position:fixed;inset:0;display:none;flex-direction:column;align-items:center;justify-content:flex-start;overflow-y:auto}
.screen.active{display:flex}

/* ─── LOBBY ─── */
#s-lobby{background:var(--bg);background-image:linear-gradient(rgba(255,230,0,.032) 1px,transparent 1px),linear-gradient(90deg,rgba(255,230,0,.032) 1px,transparent 1px);background-size:55px 55px;animation:gs 22s linear infinite;padding:1.2rem 1rem 2rem}
@keyframes gs{from{background-position:0 0}to{background-position:55px 55px}}
@keyframes fu{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
@keyframes glitch{0%,90%,100%{transform:none;filter:none}91%{transform:translate(-3px,0);filter:drop-shadow(3px 0 var(--red))}92%{transform:translate(3px,0);filter:drop-shadow(-3px 0 var(--cy))}93%{transform:none}}
.orb{position:absolute;border-radius:50%;filter:blur(90px);animation:of 9s ease-in-out infinite;pointer-events:none}
.o1{width:450px;height:450px;background:rgba(255,0,128,.08);top:-10%;left:-8%}
.o2{width:300px;height:300px;background:rgba(0,245,255,.06);bottom:0;right:0;animation-delay:-5s}
@keyframes of{0%,100%{transform:translateY(0)}50%{transform:translateY(-25px)}}

.linner{position:relative;z-index:1;width:100%;max-width:940px;margin:0 auto}
.mt{font-family:'Press Start 2P',monospace;font-size:clamp(2rem,7vw,4.5rem);color:var(--y);line-height:1;text-shadow:0 0 35px rgba(255,230,0,.9);animation:glitch 5s infinite,fu .6s .1s both;text-align:center;margin-bottom:.25rem}
.msub{font-family:'Press Start 2P',monospace;font-size:.42rem;color:var(--muted);letter-spacing:.14em;text-align:center;margin-bottom:1.5rem;animation:fu .6s .2s both}
.lbl{font-family:'Press Start 2P',monospace;font-size:.36rem;color:var(--muted);letter-spacing:.15em;display:block;margin-bottom:.4rem}

.lobby-layout{display:grid;grid-template-columns:1fr 1fr;gap:1rem;animation:fu .6s .25s both}
.panel{background:rgba(13,13,24,.85);border:1px solid var(--bdr);padding:.9rem}
.panel-title{font-family:'Press Start 2P',monospace;font-size:.38rem;color:var(--y);letter-spacing:.1em;margin-bottom:.8rem;padding-bottom:.5rem;border-bottom:1px solid var(--bdr)}

/* Name input */
.ni{background:var(--bg);border:2px solid var(--bdr);color:var(--txt);font-family:'Barlow Condensed',sans-serif;font-weight:700;font-size:1.1rem;padding:.5rem .9rem;outline:none;width:100%;letter-spacing:.05em;transition:border-color .2s;margin-bottom:.7rem}
.ni:focus{border-color:var(--y)}.ni::placeholder{color:var(--muted)}

/* Team buttons */
.team-btns{display:grid;grid-template-columns:1fr 1fr;gap:.5rem;margin-bottom:.7rem}
.tbt{font-family:'Press Start 2P',monospace;font-size:.4rem;padding:.7rem .5rem;border:2px solid;background:transparent;cursor:pointer;transition:all .2s;clip-path:polygon(8px 0%,100% 0%,calc(100% - 8px) 100%,0% 100%)}
.tbt-r{border-color:var(--red);color:var(--red)}.tbt-r:hover,.tbt-r.sel{background:var(--red);color:#fff;box-shadow:0 0 22px rgba(255,49,49,.45)}
.tbt-b{border-color:var(--blue);color:var(--blue)}.tbt-b:hover,.tbt-b.sel{background:var(--blue);color:#fff;box-shadow:0 0 22px rgba(30,144,255,.45)}
.team-count{font-family:'Press Start 2P',monospace;font-size:.3rem;color:var(--muted);text-align:center;margin-bottom:.7rem}

/* Skin grid */
.sg{display:grid;grid-template-columns:repeat(6,1fr);gap:.35rem;margin-bottom:.7rem}
.sk{border:2px solid var(--bdr);padding:.4rem .2rem;cursor:pointer;transition:all .2s;display:flex;flex-direction:column;align-items:center;gap:.25rem}
.sk:hover{border-color:rgba(255,230,0,.45)}.sk.sel{border-color:var(--y);background:rgba(255,230,0,.06)}
.sp{width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:.85rem}
.sn2{font-family:'Press Start 2P',monospace;font-size:.25rem;color:rgba(255,255,255,.6)}

/* Gun picker */
.gg{display:grid;grid-template-columns:1fr 1fr;gap:.35rem;margin-bottom:.7rem}
.gc{border:2px solid var(--bdr);padding:.45rem .4rem;cursor:pointer;transition:all .2s;text-align:center}
.gc:hover{border-color:rgba(255,230,0,.4)}.gc.sel{border-color:var(--y);background:rgba(255,230,0,.06)}
.gn{font-family:'Press Start 2P',monospace;font-size:.3rem;display:block;margin-bottom:.3rem}
.pips{display:flex;gap:2px;justify-content:center}.pip{width:6px;height:4px;border-radius:1px}

/* Ready button */
.rdy{font-family:'Press Start 2P',monospace;font-size:.5rem;letter-spacing:.08em;width:100%;padding:.75rem;border:2px solid var(--green);color:var(--green);background:transparent;cursor:pointer;transition:all .2s;clip-path:polygon(10px 0%,100% 0%,calc(100% - 10px) 100%,0% 100%)}
.rdy:hover,.rdy.active{background:var(--green);color:#000;box-shadow:0 0 22px rgba(34,197,94,.4)}
.rdy.active{border-color:var(--green)}

/* Player list */
.plist{display:flex;flex-direction:column;gap:.35rem;max-height:220px;overflow-y:auto}
.pentry{display:flex;align-items:center;gap:.6rem;padding:.4rem .6rem;background:rgba(0,0,0,.3);border:1px solid var(--bdr);font-size:.9rem;font-weight:700}
.pentry.red{border-left:3px solid var(--red)}.pentry.blue{border-left:3px solid var(--blue)}
.p-name{flex:1;letter-spacing:.03em}.p-ready{font-size:.75rem}.p-host{font-family:'Press Start 2P',monospace;font-size:.28rem;color:var(--y)}

/* Config (host only) */
.cfg-row{display:flex;gap:.4rem;flex-wrap:wrap;margin-bottom:.6rem}
.cfgb{font-family:'Press Start 2P',monospace;font-size:.32rem;padding:.45rem .6rem;border:1px solid var(--bdr);background:transparent;color:var(--muted);cursor:pointer;transition:all .2s}
.cfgb:hover{border-color:rgba(255,230,0,.4);color:var(--y)}.cfgb.sel{border-color:var(--y);color:var(--y);background:rgba(255,230,0,.06)}
.start-btn{font-family:'Press Start 2P',monospace;font-size:.5rem;letter-spacing:.08em;width:100%;padding:.75rem;background:var(--y);color:#000;border:none;cursor:pointer;clip-path:polygon(10px 0%,100% 0%,calc(100% - 10px) 100%,0% 100%);transition:transform .2s,filter .2s;margin-top:.4rem}
.start-btn:hover{transform:translateY(-2px);filter:brightness(1.12)}
.start-btn:disabled{opacity:.4;cursor:not-allowed}
.status-msg{font-family:'Press Start 2P',monospace;font-size:.36rem;color:var(--muted);text-align:center;padding:.6rem;letter-spacing:.08em;line-height:1.8}

/* Chat */
.chat-box{height:120px;overflow-y:auto;background:rgba(0,0,0,.3);border:1px solid var(--bdr);padding:.4rem;margin-bottom:.4rem;display:flex;flex-direction:column;gap:.2rem}
.chat-msg{font-size:.82rem;line-height:1.4}
.chat-msg .cn{font-weight:700}.chat-msg.red .cn{color:var(--red)}.chat-msg.blue .cn{color:var(--blue)}.chat-msg .ct{color:var(--muted)}
.chat-inp{display:flex;gap:.35rem}
.chat-ni{flex:1;background:var(--bg);border:1px solid var(--bdr);color:var(--txt);font-family:'Barlow Condensed',sans-serif;font-size:.95rem;padding:.35rem .6rem;outline:none}
.chat-ni:focus{border-color:var(--y)}
.chat-send{font-family:'Press Start 2P',monospace;font-size:.3rem;background:transparent;border:1px solid var(--bdr);color:var(--muted);padding:.35rem .6rem;cursor:pointer}
.chat-send:hover{border-color:var(--y);color:var(--y)}

/* Controls hint */
.ctrl-hint{display:flex;gap:1.2rem;justify-content:center;flex-wrap:wrap;margin-top:.8rem;animation:fu .6s .5s both}
.ci{font-family:'Press Start 2P',monospace;font-size:.3rem;color:var(--muted);text-align:center;line-height:2}
kbd{display:inline-block;background:var(--surf);border:1px solid var(--bdr);padding:.08rem .3rem;border-radius:2px;color:rgba(255,255,255,.6);font-family:inherit;font-size:.85em}

/* ─── GAME ─── */
#s-game{padding:0;justify-content:flex-start;align-items:flex-start;overflow:hidden}
#gc{position:fixed;inset:0;width:100%;height:100%;display:block;cursor:crosshair}
#hud{position:fixed;inset:0;pointer-events:none;z-index:10}
#hs{position:absolute;top:.8rem;left:50%;transform:translateX(-50%);display:flex;align-items:center;gap:1rem;background:rgba(5,5,8,.92);border:1px solid rgba(255,255,255,.06);padding:.5rem 1.4rem;backdrop-filter:blur(10px);white-space:nowrap}
.snum{font-family:'Press Start 2P',monospace;font-size:1.5rem;line-height:1;transition:all .3s}
.snum.red{color:var(--red);text-shadow:0 0 14px rgba(255,49,49,.7)}
.snum.blue{color:var(--blue);text-shadow:0 0 14px rgba(30,144,255,.7)}
.snum.ffa{color:var(--y)}
.ssep{font-family:'Press Start 2P',monospace;font-size:.36rem;color:var(--muted);text-align:center;line-height:1.9;letter-spacing:.06em}
#hmode-tag{font-family:'Press Start 2P',monospace;font-size:.3rem;color:rgba(255,255,255,.25);position:absolute;top:.2rem;left:50%;transform:translateX(-50%);white-space:nowrap}
#hl{position:absolute;top:.8rem;left:.9rem;font-family:'Press Start 2P',monospace;font-size:.32rem;color:var(--muted);background:rgba(5,5,8,.88);border:1px solid rgba(255,255,255,.04);padding:.38rem .6rem;line-height:2.1;letter-spacing:.05em}
#hl .v{color:var(--y)}
#hfeed{position:absolute;top:.8rem;right:.9rem;display:flex;flex-direction:column;gap:.25rem;align-items:flex-end;max-width:230px}
.kf{font-family:'Press Start 2P',monospace;font-size:.3rem;padding:.22rem .5rem;background:rgba(5,5,8,.92);border-right:3px solid;letter-spacing:.02em;white-space:nowrap;animation:kfi .3s ease}
.kf.red{border-color:var(--red)}.kf.blue{border-color:var(--blue)}.kf.ffa{border-color:var(--y)}
@keyframes kfi{from{opacity:0;transform:translateX(14px)}to{opacity:1;transform:none}}
#hbot{position:absolute;bottom:1rem;left:50%;transform:translateX(-50%);display:flex;flex-direction:column;align-items:center;gap:.3rem}
#hhp{display:flex;align-items:center;gap:.35rem;background:rgba(5,5,8,.88);border:1px solid rgba(255,255,255,.04);padding:.35rem .8rem}
.hpl{font-family:'Press Start 2P',monospace;font-size:.32rem;color:var(--muted);margin-right:.2rem}
.heart{font-size:1rem;transition:all .22s}.heart.e{opacity:.13;transform:scale(.75)}
#hammo{display:flex;align-items:center;gap:.5rem;background:rgba(5,5,8,.88);border:1px solid rgba(255,255,255,.04);padding:.32rem .8rem}
#hgname{font-family:'Press Start 2P',monospace;font-size:.32rem}
#hbullets{display:flex;gap:2px;align-items:center;max-width:140px;flex-wrap:wrap}
.bpip{width:4px;height:8px;border-radius:1px}
#hreload{font-family:'Press Start 2P',monospace;font-size:.3rem;color:var(--y);animation:rp 1s ease-in-out infinite;display:none}
@keyframes rp{0%,100%{opacity:1}50%{opacity:.3}}
#hpow{display:flex;gap:.25rem;background:rgba(5,5,8,.88);border:1px solid rgba(255,255,255,.04);padding:.28rem .7rem;min-height:30px;align-items:center}
#mapname{position:absolute;bottom:1rem;right:.9rem;font-family:'Press Start 2P',monospace;font-size:.3rem;color:rgba(255,255,255,.16);letter-spacing:.07em}
#hrespawn{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center;display:none}
#hrespawn.show{display:block}
.rsd{font-family:'Press Start 2P',monospace;font-size:.52rem;color:var(--red);letter-spacing:.18em;margin-bottom:.6rem}
.rsc{font-family:'Press Start 2P',monospace;font-size:3.2rem;color:var(--y);text-shadow:0 0 40px rgba(255,230,0,.9);animation:pu 1s ease-in-out infinite}
@keyframes pu{0%,100%{transform:scale(1)}50%{transform:scale(1.07)}}
#streak-banner{position:absolute;top:35%;left:50%;transform:translateX(-50%);pointer-events:none;display:none;text-align:center}
#streak-banner.show{display:block}
.sb-text{font-family:'Press Start 2P',monospace;font-size:clamp(.7rem,2.2vw,1.1rem);color:var(--y);text-shadow:0 0 40px rgba(255,230,0,.9);animation:sbA .5s ease}
@keyframes sbA{from{opacity:0;transform:scale(.5)}to{opacity:1;transform:scale(1)}}
#pow-banner{position:absolute;top:42%;left:50%;transform:translateX(-50%);pointer-events:none;display:none;text-align:center}
#pow-banner.show{display:block}
.pb-text{font-family:'Press Start 2P',monospace;font-size:.48rem;color:var(--cy);animation:sbA .4s ease}
#koth-bar{position:absolute;top:5rem;left:50%;transform:translateX(-50%);display:none;flex-direction:column;align-items:center;gap:.25rem}
#koth-bar.show{display:flex}
.koth-track{width:240px;height:9px;background:rgba(255,255,255,.05);border:1px solid var(--bdr);position:relative;overflow:hidden}
.koth-fill-r{position:absolute;left:0;top:0;height:100%;background:var(--red);transition:width .3s}
.koth-fill-b{position:absolute;right:0;top:0;height:100%;background:var(--blue);transition:width .3s}
.koth-lbl{font-family:'Press Start 2P',monospace;font-size:.28rem;color:var(--muted);display:flex;gap:1.4rem}
.kl-r{color:var(--red)}.kl-b{color:var(--blue)}
#ctf-hud{position:absolute;top:5rem;left:50%;transform:translateX(-50%);display:none;gap:1.8rem;font-family:'Press Start 2P',monospace;font-size:.36rem;background:rgba(5,5,8,.88);border:1px solid rgba(255,255,255,.04);padding:.38rem 1rem}
#ctf-hud.show{display:flex}
.ctf-r{color:var(--red)}.ctf-b{color:var(--blue)}
#flash{position:fixed;inset:0;pointer-events:none;z-index:5;opacity:0;transition:opacity .12s}

/* ─── END ─── */
#s-end{background:rgba(5,5,8,.97);backdrop-filter:blur(20px);justify-content:center;align-items:center;overflow-y:auto;padding:2rem}
.ewt{font-family:'Press Start 2P',monospace;font-size:clamp(1rem,3.5vw,2.2rem);margin-bottom:.4rem;animation:fu .5s ease;text-align:center}
.ewt.red{color:var(--red);text-shadow:0 0 50px rgba(255,49,49,.8)}
.ewt.blue{color:var(--blue);text-shadow:0 0 50px rgba(30,144,255,.8)}
.ewt.you{color:var(--y);text-shadow:0 0 50px rgba(255,230,0,.8)}
.esub{font-family:'Press Start 2P',monospace;font-size:.4rem;color:var(--muted);letter-spacing:.15em;margin-bottom:1.5rem;text-align:center}
.sb-table{width:100%;max-width:700px;border-collapse:collapse;margin-bottom:1.5rem}
.sb-table th{font-family:'Press Start 2P',monospace;font-size:.28rem;color:var(--muted);letter-spacing:.07em;padding:.45rem .6rem;text-align:left;border-bottom:2px solid var(--y)}
.sb-table td{padding:.55rem .6rem;font-size:.92rem;font-weight:700;border-bottom:1px solid var(--bdr)}
.sb-table tr.me td{background:rgba(255,230,0,.06)}
.sb-table tr.me td:first-child{border-left:3px solid var(--y)}
.sb-k{color:#FF0080}.sb-kd{color:var(--cy)}.sb-sc{color:var(--y)}.sb-team-r{color:var(--red)}.sb-team-b{color:var(--blue)}
.ba{font-family:'Press Start 2P',monospace;font-size:.5rem;letter-spacing:.08em;background:var(--y);color:#000;padding:.8rem 2.5rem;border:none;cursor:pointer;clip-path:polygon(12px 0%,100% 0%,calc(100% - 12px) 100%,0% 100%);transition:transform .2s,filter .2s}
.ba:hover{transform:translateY(-3px);filter:brightness(1.1)}

/* Ping indicator */
#ping{position:absolute;bottom:.5rem;left:.9rem;font-family:'Press Start 2P',monospace;font-size:.28rem;color:rgba(255,255,255,.2)}
#coins-hud{position:absolute;top:.8rem;right:13rem;font-family:'Press Start 2P',monospace;font-size:.38rem;color:var(--y);background:rgba(5,5,8,.9);border:1px solid rgba(255,255,255,.06);padding:.35rem .7rem;pointer-events:none;white-space:nowrap}

#pause-overlay{position:fixed;inset:0;background:rgba(0,0,0,.82);z-index:300;display:none;align-items:center;justify-content:center;flex-direction:column;gap:1.2rem}
#pause-overlay.open{display:flex}
#pause-box{background:#0b0b16;border:2px solid var(--y);padding:2rem 2.5rem;display:flex;flex-direction:column;align-items:center;gap:1rem;min-width:260px}
#pause-title{font-family:'Press Start 2P',monospace;font-size:.7rem;color:var(--y);margin-bottom:.5rem;letter-spacing:.1em}
.pause-btn{font-family:'Press Start 2P',monospace;font-size:.4rem;width:100%;padding:.7rem;border:2px solid;cursor:pointer;background:transparent;transition:all .2s;letter-spacing:.06em}
.pause-btn-resume{border-color:var(--green);color:var(--green)}.pause-btn-resume:hover{background:var(--green);color:#000}
.pause-btn-leave{border-color:var(--red);color:var(--red)}.pause-btn-leave:hover{background:var(--red);color:#fff}
.pause-hint{font-family:'Press Start 2P',monospace;font-size:.28rem;color:var(--muted);margin-top:.3rem}
#shop-btn{position:absolute;bottom:1rem;right:.9rem;font-family:'Press Start 2P',monospace;font-size:.32rem;padding:.5rem .8rem;background:rgba(5,5,8,.92);border:2px solid var(--y);color:var(--y);cursor:pointer;pointer-events:all;z-index:20;transition:all .2s;letter-spacing:.06em}
#shop-btn:hover{background:var(--y);color:#000}
#shop-overlay{position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:200;display:none;align-items:center;justify-content:center}
#shop-overlay.open{display:flex}
#shop-panel{background:#0b0b16;border:2px solid var(--y);padding:1.4rem;width:min(560px,96vw);max-height:88vh;overflow-y:auto;position:relative;border-radius:2px}
#shop-panel h2{font-family:'Press Start 2P',monospace;font-size:.6rem;color:var(--y);margin-bottom:.5rem;text-align:center;letter-spacing:.1em}
.shop-bal{font-family:'Press Start 2P',monospace;font-size:.38rem;color:var(--y);text-align:center;margin-bottom:1rem}
.shop-sec{font-family:'Press Start 2P',monospace;font-size:.28rem;color:var(--muted);letter-spacing:.14em;margin:.8rem 0 .4rem;border-bottom:1px solid var(--bdr);padding-bottom:.3rem}
.shop-grid{display:grid;grid-template-columns:1fr 1fr;gap:.45rem;margin-bottom:.5rem}
.shop-item{border:1px solid var(--bdr);padding:.6rem .7rem;cursor:pointer;transition:all .18s;background:rgba(0,0,0,.35)}
.shop-item:hover:not(.si-owned):not(.si-eq){border-color:var(--y);background:rgba(255,230,0,.05)}
.si-owned{border-color:var(--green);opacity:.55;cursor:default}
.si-eq{border-color:var(--cy)!important;background:rgba(0,245,255,.05)!important;opacity:1!important;cursor:default}
.si-name{font-family:'Press Start 2P',monospace;font-size:.3rem;margin-bottom:.25rem}
.si-desc{font-size:.82rem;color:rgba(255,255,255,.42);margin-bottom:.32rem;line-height:1.4}
.si-price{font-family:'Press Start 2P',monospace;font-size:.28rem;color:var(--y)}
.si-price.broke{color:var(--red)}
#shop-close{position:absolute;top:.55rem;right:.75rem;font-family:'Press Start 2P',monospace;font-size:.4rem;color:var(--muted);background:none;border:none;cursor:pointer;padding:.2rem}
#shop-close:hover{color:var(--y)}
</style>
</head>
<body>
<div id="flash"></div>

<!-- ═══ LOBBY ═══ -->
<div id="s-lobby" class="screen active">
  <div class="orb o1"></div><div class="orb o2"></div>
  <div class="linner">
    <h1 class="mt">RIVALS</h1>
    <p class="msub">MULTIPLAYER LOBBY</p>

    <div class="lobby-layout">
      <!-- LEFT: Setup -->
      <div>
        <div class="panel" style="margin-bottom:.7rem">
          <div class="panel-title">YOUR SETUP</div>
          <label class="lbl">NAME</label>
          <input id="ni" class="ni" type="text" placeholder="ENTER NAME" maxlength="12" spellcheck="false" autocomplete="off"/>
          <label class="lbl">TEAM</label>
          <div class="team-btns">
            <button class="tbt tbt-r" onclick="selTeam('red')">🔴 RED</button>
            <button class="tbt tbt-b" onclick="selTeam('blue')">🔵 BLUE</button>
          </div>
          <div class="team-count" id="team-counts">RED: 0 · BLUE: 0</div>
          <label class="lbl">SKIN</label>
          <div class="sg" id="sg"></div>
          <label class="lbl">WEAPON</label>
          <div class="gg" id="gg"></div>
          <button class="rdy" id="rdybtn" onclick="toggleReady()">READY UP</button>
        </div>

        <div class="panel" id="host-panel" style="display:none">
          <div class="panel-title">⭐ HOST CONTROLS</div>
          <label class="lbl">MATCH SIZE</label>
          <div class="cfg-row" id="size-row">
            <button class="cfgb" data-v="1v1" onclick="setCfg('size','1v1')">1v1</button>
            <button class="cfgb" data-v="2v2" onclick="setCfg('size','2v2')">2v2</button>
            <button class="cfgb" data-v="3v3" onclick="setCfg('size','3v3')">3v3</button>
            <button class="cfgb" data-v="4v4" onclick="setCfg('size','4v4')">4v4</button>
          </div>
          <label class="lbl">GAME MODE</label>
          <div class="cfg-row" id="mode-row">
            <button class="cfgb" data-v="tdm" onclick="setCfg('mode','tdm')">⚔️ TDM</button>
            <button class="cfgb" data-v="ffa" onclick="setCfg('mode','ffa')">💀 FFA</button>
            <button class="cfgb" data-v="koth" onclick="setCfg('mode','koth')">👑 KOTH</button>
            <button class="cfgb" data-v="ctf" onclick="setCfg('mode','ctf')">🚩 CTF</button>
            <button class="cfgb" data-v="gun" onclick="setCfg('mode','gun')">🔫 GUN</button>
          </div>
          <div class="status-msg" id="status-msg">Waiting for players…</div>
          <button class="start-btn" id="start-btn" onclick="doStart()" disabled>START MATCH ⚡</button>
        </div>
        <div class="status-msg" id="guest-status" style="display:none;margin-top:.5rem">Waiting for host to start the match…</div>
      </div>

      <!-- RIGHT: Players + Chat -->
      <div>
        <div class="panel" style="margin-bottom:.7rem">
          <div class="panel-title">PLAYERS IN LOBBY</div>
          <div class="plist" id="plist"></div>
        </div>

        <div class="panel">
          <div class="panel-title">CHAT</div>
          <div class="chat-box" id="chat-box"></div>
          <div class="chat-inp">
            <input class="chat-ni" id="chat-in" placeholder="type here…" maxlength="80" onkeydown="if(event.key==='Enter')sendChat()"/>
            <button class="chat-send" onclick="sendChat()">SEND</button>
          </div>
        </div>

        <div class="ctrl-hint">
          <div class="ci"><kbd>WASD</kbd><br>MOVE</div>
          <div class="ci">MOUSE<br>AIM</div>
          <div class="ci"><kbd>LMB</kbd><br>SHOOT</div>
          <div class="ci"><kbd>F</kbd>/<kbd>RMB</kbd><br>SWORD</div>
          <div class="ci"><kbd>R</kbd><br>RELOAD</div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ═══ GAME ═══ -->
<div id="s-game" class="screen">
  <canvas id="gc"></canvas>
  <div id="hud">
    <span id="hmode-tag"></span>
    <div id="hs">
      <span class="snum red" id="sr">0</span>
      <div class="ssep" id="ssep">KILLS<br>TO 30</div>
      <span class="snum blue" id="sb2">0</span>
    </div>
    <div id="hl">KILLS &nbsp;<span class="v" id="mk">0</span><br>DEATHS <span class="v" id="md">0</span><br>STREAK <span class="v" id="ms">0</span></div>
    <div id="hfeed"></div>
    <div id="koth-bar">
      <div class="koth-lbl"><span class="kl-r" id="kr-s">0s</span><span style="color:var(--muted)">ZONE</span><span class="kl-b" id="kb-s">0s</span></div>
      <div class="koth-track"><div class="koth-fill-r" id="kr-fill" style="width:0%"></div><div class="koth-fill-b" id="kb-fill" style="width:0%"></div></div>
    </div>
    <div id="ctf-hud"><span class="ctf-r">🚩 <span id="ctf-r">0</span></span><span class="ctf-b">🏳 <span id="ctf-b">0</span></span></div>
    <div id="hbot">
      <div id="hhp"><span class="hpl">HP</span><span class="heart" id="h1">❤️</span><span class="heart" id="h2">❤️</span><span class="heart" id="h3">❤️</span><span id="shield-pip"></span></div>
      <div id="hpow"></div>
      <div id="hammo"><span id="hgname">PISTOL</span><div id="hbullets"></div><span id="hreload">RELOADING…</span></div>
    </div>
    <div id="mapname"></div>
    <div id="ping"></div>
    <div id="coins-hud">🪙 <span id="coins-val">0</span></div>
    <button id="shop-btn" onclick="openShop()">🛒 SHOP [B]</button>
    <div id="hrespawn"><div class="rsd">— YOU DIED —</div><div class="rsc" id="rsc">3</div></div>
    <div id="streak-banner"><div class="sb-text" id="sb-text"></div></div>
    <div id="pow-banner"><div class="pb-text" id="pb-text"></div></div>
  </div>
</div>

<!-- ═══ PAUSE ═══ -->
<div id="pause-overlay">
  <div id="pause-box">
    <div id="pause-title">⏸ PAUSED</div>
    <button class="pause-btn pause-btn-resume" onclick="closePause()">▶ RESUME [Q]</button>
    <button class="pause-btn pause-btn-leave" onclick="leaveGame()">✕ LEAVE MATCH</button>
    <div class="pause-hint">leaving returns you to lobby</div>
  </div>
</div>

<!-- ═══ SHOP ═══ -->
<div id="shop-overlay">
  <div id="shop-panel">
    <button id="shop-close" onclick="closeShop()">✕</button>
    <h2>🛒 SHOP</h2>
    <div class="shop-bal">🪙 <span id="shop-bal-val">0</span> COINS — EARN 50 PER KILL</div>
    <div class="shop-sec">── WEAPONS ──</div>
    <div class="shop-grid" id="shop-guns"></div>
    <div class="shop-sec">── UPGRADES ──</div>
    <div class="shop-grid" id="shop-upgrades"></div>
    <div style="font-family:'Press Start 2P',monospace;font-size:.27rem;color:var(--muted);text-align:center;margin-top:.8rem">[B] to open · [ESC] to close</div>
  </div>
</div>

<!-- ═══ END ═══ -->
<div id="s-end" class="screen">
  <div class="ewt" id="et">RED WINS!</div>
  <div class="esub" id="es">ROUND OVER</div>
  <table class="sb-table"><thead><tr><th>#</th><th>PLAYER</th><th>TEAM</th><th>K</th><th>D</th><th>K/D</th><th>SCORE</th></tr></thead><tbody id="sb-body"></tbody></table>
  <button class="ba" onclick="doPlayAgain()">LOBBY ⚡</button>
</div>

<script>
// ══════════════════════════════════════════════════════
//  DATA
// ══════════════════════════════════════════════════════
const SKINS=[
  {id:'phantom',bodyCol:'#8B5CF6',accCol:'#C4B5FD',glowCol:'rgba(139,92,246,.7)',shape:'rings',emoji:'👻',name:'PHANTOM'},
  {id:'demon',  bodyCol:'#DC2626',accCol:'#FCA5A5',glowCol:'rgba(220,38,38,.7)', shape:'eyes', emoji:'😈',name:'DEMON'},
  {id:'ghost',  bodyCol:'#94A3B8',accCol:'#F1F5F9',glowCol:'rgba(200,210,230,.5)',shape:'soft', emoji:'🤍',name:'GHOST'},
  {id:'blaze',  bodyCol:'#F97316',accCol:'#FED7AA',glowCol:'rgba(249,115,22,.7)',shape:'fire', emoji:'🔥',name:'BLAZE'},
  {id:'void',   bodyCol:'#312E81',accCol:'#818CF8',glowCol:'rgba(99,102,241,.7)', shape:'cross',emoji:'⚫',name:'VOID'},
  {id:'storm',  bodyCol:'#0891B2',accCol:'#67E8F9',glowCol:'rgba(8,145,178,.7)',  shape:'zap',  emoji:'⚡',name:'STORM'},
];
const GUNS=[
  {id:'pistol', name:'PISTOL', col:'#94A3B8',dmg:1,rate:.42,spd:620,spread:.07,maxAmmo:12,reload:1.3,pellets:1,range:1.5,tw:2, d:2,f:3,a:3,icon:'🔫'},
  {id:'smg',    name:'SMG',    col:'#22D3EE',dmg:1,rate:.09,spd:560,spread:.19,maxAmmo:30,reload:2.0,pellets:1,range:1.2,tw:1.5,d:1,f:5,a:5,icon:'🔧'},
  {id:'shotgun',name:'SHOTGUN',col:'#F97316',dmg:1,rate:.88,spd:420,spread:.34,maxAmmo:6, reload:1.9,pellets:5,range:.55,tw:2.5,d:5,f:1,a:1,icon:'💥'},
  {id:'sniper', name:'SNIPER', col:'#A3E635',dmg:3,rate:1.3,spd:1150,spread:.01,maxAmmo:5,reload:2.2,pellets:1,range:2.0,tw:3,  d:5,f:1,a:1,icon:'🎯'},
  {id:'assault',name:'ASSAULT',col:'#FB7185',dmg:1,rate:.19,spd:680,spread:.10,maxAmmo:20,reload:1.6,pellets:1,range:1.4,tw:2, d:2,f:4,a:4,icon:'🔴'},
];
const GUN_ORDER=['pistol','smg','shotgun','assault','sniper'];
const STREAK_NAMES={2:'DOUBLE KILL!',3:'TRIPLE KILL!',4:'QUAD KILL!',5:'RAMPAGE!!',6:'UNSTOPPABLE!!!',7:'GODLIKE!!!!!'};
const POW_TYPES=[
  {id:'health',icon:'❤️',col:'#FF3131',label:'HEALTH BOOST'},
  {id:'speed', icon:'⚡',col:'#FFE600',label:'SPEED BOOST'},
  {id:'damage',icon:'🔥',col:'#F97316',label:'DAMAGE AMP'},
  {id:'shield',icon:'🛡️',col:'#00F5FF',label:'SHIELD'},
];
const MAP_THEMES=[
  {name:'INDUSTRIAL',floor:'#050a0a',grid:'rgba(255,140,0,.03)', wf:'#0a0e12',ws:'rgba(255,140,50,.28)',wa:'rgba(255,140,50,.75)',st:'rgba(255,100,0,.07)'},
  {name:'RUINS',     floor:'#080710',grid:'rgba(180,150,70,.03)',wf:'#0f0a06',ws:'rgba(190,140,60,.3)', wa:'rgba(210,170,80,.75)',st:'rgba(160,110,40,.07)'},
  {name:'BUNKER',    floor:'#040810',grid:'rgba(0,210,90,.03)',  wf:'#040a06',ws:'rgba(0,180,80,.27)',  wa:'rgba(0,220,100,.75)', st:'rgba(0,170,70,.07)'},
  {name:'VOID',      floor:'#050408',grid:'rgba(160,0,255,.03)', wf:'#0a0510',ws:'rgba(140,0,230,.3)',  wa:'rgba(170,0,255,.75)', st:'rgba(130,0,210,.07)'},
  {name:'NEON',      floor:'#060408',grid:'rgba(255,0,130,.03)', wf:'#0d0410',ws:'rgba(255,0,100,.27)', wa:'rgba(255,0,130,.75)', st:'rgba(210,0,95,.07)'},
];

function seededRNG(seed){
  let s=seed;
  return ()=>{s=(s*1664525+1013904223)&0xffffffff;return(s>>>0)/0xffffffff;};
}

function genMap(seed){
  const rng=seededRNG(seed);
  const themeIdx=Math.floor(rng()*MAP_THEMES.length);
  const theme=MAP_THEMES[themeIdx];
  const gens=[genSymmetric,genCorridors,genFortress,genOpen,genMaze,gen1v1];
  const genIdx=Math.floor(rng()*gens.length);
  const obstacles=gens[genIdx]();
  return{theme,obstacles,name:theme.name};
}
const AW=1400,AH=800,SPAWN_C=210;
function genSymmetric(){return[{x:645,y:345,w:110,h:110},{x:295,y:140,w:85,h:170},{x:295,y:490,w:85,h:170},{x:AW-380,y:140,w:85,h:170},{x:AW-380,y:490,w:85,h:170},{x:490,y:65,w:130,h:65},{x:490,y:AH-130,w:130,h:65},{x:AW-620,y:65,w:130,h:65},{x:AW-620,y:AH-130,w:130,h:65}];}
function genCorridors(){return[{x:225,y:245,w:350,h:40},{x:825,y:245,w:350,h:40},{x:225,y:AH-285,w:350,h:40},{x:825,y:AH-285,w:350,h:40},{x:AW/2-35,y:150,w:70,h:165},{x:AW/2-35,y:AH-315,w:70,h:165},{x:AW/2-35,y:AH/2-35,w:70,h:70}];}
function genFortress(){return[{x:385,y:185,w:75,h:195},{x:385,y:185,w:195,h:75},{x:385,y:AH-380,w:75,h:195},{x:385,y:AH-260,w:195,h:75},{x:AW-460,y:185,w:75,h:195},{x:AW-580,y:185,w:195,h:75},{x:AW-460,y:AH-380,w:75,h:195},{x:AW-580,y:AH-260,w:195,h:75},{x:AW/2-45,y:AH/2-90,w:90,h:180}];}
function genOpen(){return[{x:AW/2-55,y:AH/2-55,w:110,h:110},{x:330,y:180,w:75,h:75},{x:330,y:AH-255,w:75,h:75},{x:AW-405,y:180,w:75,h:75},{x:AW-405,y:AH-255,w:75,h:75},{x:540,y:AH/2-38,w:95,h:76},{x:AW-635,y:AH/2-38,w:95,h:76}];}
function genMaze(){return[{x:310,y:0,w:60,h:280},{x:310,y:400,w:60,h:400},{x:AW-370,y:0,w:60,h:280},{x:AW-370,y:400,w:60,h:400},{x:560,y:160,w:60,h:280},{x:560,y:560,w:60,h:240},{x:AW-620,y:160,w:60,h:280},{x:AW-620,y:560,w:60,h:240},{x:AW/2-35,y:0,w:70,h:160},{x:AW/2-35,y:640,w:70,h:160}];}
function gen1v1(){return[{x:AW/2-55,y:AH/2-55,w:110,h:110},{x:AW/2-160,y:AH/2-160,w:55,h:55},{x:AW/2+105,y:AH/2-160,w:55,h:55},{x:AW/2-160,y:AH/2+105,w:55,h:55},{x:AW/2+105,y:AH/2+105,w:55,h:55},{x:AW/2-250,y:AH/2-25,w:70,h:50},{x:AW/2+180,y:AH/2-25,w:70,h:50},{x:AW/2-25,y:AH/2-220,w:50,h:70},{x:AW/2-25,y:AH/2+150,w:50,h:70}];}

// ══════════════════════════════════════════════════════
//  WEBSOCKET CLIENT
// ══════════════════════════════════════════════════════
let ws=null, myId=null, isHost=false;
let currentConfig={mode:'tdm',size:'2v2',target:30};
let lobbyPlayers={}; // id → player info from server
let gameConfig=null, mapSeed=0, hostId=null;
let pingStart=0, pingMs=0;

function wsConnect(){
  const proto=location.protocol==='https:'?'wss:':'ws:';
  ws=new WebSocket(proto+'//'+location.host);
  ws.onopen=()=>{
    // Send name on open
    const name=document.getElementById('ni').value.trim().toUpperCase()||'PLAYER';
    wsSend({type:'set_name',name});
    pingStart=Date.now();
    ws.send(JSON.stringify({type:'ping'}));
  };
  ws.onmessage=e=>onServerMsg(JSON.parse(e.data));
  ws.onclose=()=>{
    setTimeout(wsConnect,2000);
    addChat('SYSTEM','','Reconnecting…');
  };
  ws.onerror=()=>ws.close();
}

function wsSend(obj){ if(ws&&ws.readyState===1) ws.send(JSON.stringify(obj)); }

// ── Ping every 3s ──
setInterval(()=>{
  if(ws&&ws.readyState===1){pingStart=Date.now();wsSend({type:'ping'});}
},3000);

// ══════════════════════════════════════════════════════
//  SERVER MESSAGE HANDLER
// ══════════════════════════════════════════════════════
function onServerMsg(msg){
  switch(msg.type){
    case 'pong':
      pingMs=Date.now()-pingStart;
      document.getElementById('ping').textContent='PING '+pingMs+'ms';
      break;
    case 'welcome':
      myId=msg.id; isHost=msg.is_host;
      break;
    case 'promoted_host':
      isHost=true;
      addChat('SYSTEM','','You are now the host');
      renderLobby({players:Object.values(lobbyPlayers),config:currentConfig,host_id:myId});
      break;
    case 'lobby':
      renderLobby(msg);
      break;
    case 'start':{
      gameConfig=msg.config;
      mapSeed=msg.map_seed;
      hostId=msg.host_id;
      // Build remote player list — only HUMAN players, not bots (bots are local)
      remotePlayers={};
      msg.players.forEach(p=>{
        if(p.id!==myId && !p.bot){
          remotePlayers[p.id]={...p, x:p.team==='red'?100:AW-100, y:AH/2, angle:0, vx:0, vy:0, hp:3, dead:false, attacking:false, hasFlag:false};
        }
      });
      // Find my player info
      const myInfo=msg.players.find(p=>p.id===myId)||{team:'red',skin:'phantom',gun:'pistol'};
      startGame(myInfo, msg.players);
      break;
    }
    case 'state':{
      // Ignore our own state and bot states (bots are simulated locally)
      if(!msg.id||msg.id===myId||msg.id.startsWith('bot_')){
        delete remotePlayers[myId];break;
      }
      if(remotePlayers[msg.id]){
        const rp=remotePlayers[msg.id];
        const dx=msg.x-rp.x, dy=msg.y-rp.y, dist=Math.hypot(dx,dy);
        if(dist>200){rp.x=msg.x;rp.y=msg.y;}
        else{rp.x+=dx*.3;rp.y+=dy*.3;}
        if(rp.dead&&!msg.dead)rp._hp=MAX_HP;
        Object.assign(rp,msg);
      } else {
        remotePlayers[msg.id]={...msg,_hp:MAX_HP};
      }
      break;
    }
    case 'kill':
      handleKillMsg(msg);
      break;
    case 'koth_update':
      kothScore.red=msg.scores.red||0;
      kothScore.blue=msg.scores.blue||0;
      updKothHud();
      break;
    case 'ctf_cap':
      ctfCaps[msg.team]=(ctfCaps[msg.team]||0)+1;
      updCtfHud();
      break;
    case 'end':
      triggerEnd(msg.winner_text, msg.winner_team, msg.players);
      break;
    case 'chat':
      addChat(msg.from, msg.team, msg.text);
      break;
  }
}

function handleKillMsg(msg){
  // Update scores from server
  gameScores.red=msg.scores?.red||gameScores.red;
  gameScores.blue=msg.scores?.blue||gameScores.blue;

  // Update killer stats — remote human or bot
  if(remotePlayers[msg.killer_id]){
    remotePlayers[msg.killer_id].kills=msg.killer_kills||0;
  }
  const killerBot=botEntities.find(b=>b.id===msg.killer_id);
  if(killerBot){
    killerBot.kills=(msg.killer_kills||killerBot.kills+1);
    killerBot.streak=(killerBot.streak||0)+1;
    killerBot.score=(killerBot.score||0)+100;
    // Bot team scores a point in TDM
    if(gameConfig?.mode==='tdm'){
      gameScores[killerBot.team]=(gameScores[killerBot.team]||0)+1;
    }
  }

  // Update victim stats — could be a bot
  const victimBot=botEntities.find(b=>b.id===msg.victim_id);
  if(victimBot){
    victimBot.deaths=(victimBot.deaths||0)+1;
    victimBot.streak=0;
  }

  updScoreHud();
  addFeed(msg.killer_name, msg.victim_name, msg.killer_team||'ffa');

  // Streak banners for MY kills
  if(msg.killer_id===myId){
    if(STREAK_NAMES[msg.streak])showStreak(STREAK_NAMES[msg.streak]);
  }

  // If I was the victim — respawn
  if(msg.victim_id===myId){
    localPlayer.hp=0;
    localPlayer.dead=true;
    localPlayer.deaths++;
    localPlayer.streak=0;
    localPlayer.respawnTimer=3;
    setRespawnUI(3);
    updHpHud();
  }
}

// ══════════════════════════════════════════════════════
//  LOBBY UI
// ══════════════════════════════════════════════════════
let chosenTeam=null, chosenSkin='phantom', chosenGun='pistol';
let amReady=false;

function renderLobby(msg){
  const players=msg.players||[];
  lobbyPlayers={};
  players.forEach(p=>lobbyPlayers[p.id]=p);
  currentConfig=msg.config||currentConfig;
  hostId=msg.host_id;
  isHost=(myId===hostId);

  // Player list
  const pl=document.getElementById('plist');
  pl.innerHTML='';
  const reds=players.filter(p=>p.team==='red');
  const blues=players.filter(p=>p.team==='blue');
  document.getElementById('team-counts').textContent=`RED: ${reds.length} · BLUE: ${blues.length}`;

  players.forEach(p=>{
    const d=document.createElement('div');
    d.className='pentry '+(p.team||'');
    const skinObj=SKINS.find(s=>s.id===p.skin)||SKINS[0];
    d.innerHTML=`<span style="font-size:1rem">${skinObj.emoji}</span><span class="p-name">${p.name}</span>${p.id===hostId?'<span class="p-host">HOST</span>':''}${p.ready?'<span class="p-ready">✅</span>':'<span class="p-ready" style="opacity:.3">⬜</span>'}`;
    pl.appendChild(d);
  });

  // Host panel
  document.getElementById('host-panel').style.display=isHost?'block':'none';
  document.getElementById('guest-status').style.display=isHost?'none':'block';

  if(isHost){
    // Config buttons
    document.querySelectorAll('#size-row .cfgb').forEach(b=>b.classList.toggle('sel',b.dataset.v===currentConfig.size));
    document.querySelectorAll('#mode-row .cfgb').forEach(b=>b.classList.toggle('sel',b.dataset.v===currentConfig.mode));

    document.getElementById('start-btn').disabled=false;
    document.getElementById('status-msg').textContent=
      `🔴 RED: ${reds.length}  🔵 BLUE: ${blues.length}  — Bots fill empty slots. Hit START!`;
  }
}

function selTeam(team){
  if(!myId)return;
  chosenTeam=team;
  document.querySelectorAll('.tbt').forEach(b=>b.classList.remove('sel'));
  document.querySelector('.tbt-'+team.charAt(0)).classList.add('sel');
  wsSend({type:'set_team',team});
  wsSend({type:'set_ready',ready:false});
  amReady=false;
  document.getElementById('rdybtn').classList.remove('active');
  document.getElementById('rdybtn').textContent='READY UP';
}

function toggleReady(){
  if(!chosenTeam){alert('Pick a team first!');return;}
  amReady=!amReady;
  wsSend({type:'set_ready',ready:amReady});
  const btn=document.getElementById('rdybtn');
  btn.classList.toggle('active',amReady);
  btn.textContent=amReady?'✅ READY!':'READY UP';
}

function selSkin(id){
  chosenSkin=id;
  document.querySelectorAll('.sk').forEach(s=>s.classList.toggle('sel',s.dataset.id===id));
  wsSend({type:'set_skin',skin:id});
}

function selGun(id){
  chosenGun=id;
  document.querySelectorAll('.gc').forEach(g=>g.classList.toggle('sel',g.dataset.id===id));
  wsSend({type:'set_gun',gun:id});
}

function setCfg(key,val){wsSend({type:'set_config',config:{[key]:val}});}

function doStart(){wsSend({type:'start'});}

function addChat(from, team, text){
  const box=document.getElementById('chat-box');
  const d=document.createElement('div');
  d.className='chat-msg '+(team||'');
  d.innerHTML=`<span class="cn">${from}</span><span class="ct">: ${text}</span>`;
  box.appendChild(d);
  box.scrollTop=box.scrollHeight;
  if(box.children.length>80)box.removeChild(box.children[0]);
}

function sendChat(){
  const inp=document.getElementById('chat-in');
  const text=inp.value.trim();
  if(!text)return;
  inp.value='';
  wsSend({type:'chat',text});
}

// ══════════════════════════════════════════════════════
//  BUILD LOBBY MENU
// ══════════════════════════════════════════════════════
function buildLobbyMenu(){
  const sg=document.getElementById('sg');
  SKINS.forEach(sk=>{
    const d=document.createElement('div');d.className='sk';d.dataset.id=sk.id;d.onclick=()=>selSkin(sk.id);
    d.innerHTML=`<div class="sp" style="background:${sk.bodyCol};box-shadow:0 0 8px ${sk.glowCol}">${sk.emoji}</div><div class="sn2">${sk.name}</div>`;
    sg.appendChild(d);
  });
  // Default skin
  sg.querySelector('[data-id="phantom"]').classList.add('sel');

  const gg=document.getElementById('gg');
  GUNS.forEach(g=>{
    const d=document.createElement('div');d.className='gc';d.dataset.id=g.id;d.onclick=()=>selGun(g.id);
    const pips=(n,max,col)=>'<div class="pips">'+[...Array(max)].map((_,i)=>`<div class="pip" style="background:${i<n?col:'var(--bdr)'}"></div>`).join('')+'</div>';
    d.innerHTML=`<span class="gn" style="color:${g.col}">${g.icon} ${g.name}</span><div style="display:flex;gap:.2rem;flex-direction:column">${pips(g.d,5,g.col)}<br>${pips(g.f,5,'#FFE600')}</div>`;
    gg.appendChild(d);
  });
  gg.querySelector('[data-id="pistol"]').classList.add('sel');
}

// Name input: send on change
document.addEventListener('DOMContentLoaded', ()=>{
  buildLobbyMenu();
  wsConnect();
  document.getElementById('ni').addEventListener('change',e=>{
    wsSend({type:'set_name',name:e.target.value.trim().toUpperCase()||'PLAYER'});
  });
});

// ══════════════════════════════════════════════════════
//  GAME ENGINE
// ══════════════════════════════════════════════════════
let canvas,ctx,currentMap;
let localPlayer=null;
let remotePlayers={};  // id → state from server
let bullets=[],particles=[],powerups=[];
let gameScores={red:0,blue:0};
let kothScore={red:0,blue:0};
let ctfCaps={red:0,blue:0};
let feedItems=[];
let running=false,lastTs=0;
let keys={},mouse={x:AW/2,y:AH/2},mousedown=false;
let shakeX=0,shakeY=0,shakeMag=0;
let scl=1,ox=0,oy=0;
let powupTimer=0;
let localKothTimer=0; // how long I've been holding zone
let flags={red:null,blue:null};
let allGamePlayers=[]; // all players (for scoreboard)

const RAD=15,MAX_HP=3;
let coins=0;
function updCoinsHud(){const el=document.getElementById('coins-val');if(el)el.textContent=coins;}
const SWORD_R=68,SWORD_ARC=Math.PI*.68,ATK_DUR=.25,SWORD_CD=.55;
const P_SPEED=250,RESPAWN_S=3;

// ── Bot state (host runs bots) ──
let botEntities=[]; // local bot objects

// ══════════════════════════════════════════════════════
//  BULLET / PARTICLE
// ══════════════════════════════════════════════════════
class Bullet{
  constructor(x,y,angle,shooterRef,gun,shooterId){
    this.x=x;this.y=y;this.px=x;this.py=y;
    this.shooterId=shooterId;
    this.shooterTeam=shooterRef.team;
    this.dmg=gun.dmg*(shooterRef.powDamage?2:1);
    this.gunId=gun.id;this.spd=gun.spd;
    this.vx=Math.cos(angle)*this.spd;this.vy=Math.sin(angle)*this.spd;
    this.life=gun.range;this.col=gun.col;this.tw=gun.tw;this.dead=false;
  }
  update(dt){
    this.px=this.x;this.py=this.y;
    this.x+=this.vx*dt;this.y+=this.vy*dt;
    this.life-=dt;
    if(this.life<=0||this.x<-10||this.x>AW+10||this.y<-10||this.y>AH+10){this.dead=true;return;}
    for(const ob of currentMap.obstacles){
      if(this.x>ob.x&&this.x<ob.x+ob.w&&this.y>ob.y&&this.y<ob.y+ob.h){this.dead=true;spawnBurst(this.x,this.y,this.col,4);return;}
    }
    const lp=localPlayer;
    // Enemy bullets hitting me
    if(lp&&!lp.dead&&this.shooterId!==myId&&this.shooterTeam!==lp.team){
      if(Math.hypot(lp.x-this.x,lp.y-this.y)<RAD+5){
        lp.hp=Math.max(0,lp.hp-this.dmg);
        lp.hitFlash=.22;this.dead=true;
        spawnBurst(this.x,this.y,this.col,10);
        shake(this.gunId==='sniper'?9:this.gunId==='shotgun'?7:3);
        updHpHud();
        if(lp.hp<=0&&!lp.dead){
          lp.dead=true;lp.deaths++;lp.streak=0;
          lp.respawnTimer=RESPAWN_S;setRespawnUI(RESPAWN_S);
          spawnBurst(lp.x,lp.y,'#FF3131',28,true);
          wsSend({type:'kill',victim_id:myId,killer_id:this.shooterId});
        }
        return;
      }
    }
    // My bullets hitting enemies
    if(this.shooterId===myId&&lp){
      const myTeam=lp.team;
      // Hit enemy bots
      for(const bot of botEntities){
        if(bot.dead||bot.team===myTeam)continue;
        if(Math.hypot(bot.x-this.x,bot.y-this.y)<RAD+5){
          this.dead=true;spawnBurst(this.x,this.y,this.col,10);
          bot.hp=Math.max(0,bot.hp-this.dmg);bot.hitFlash=.18;
          if(bot.hp<=0&&!bot.dead){
            bot.dead=true;bot.deaths++;bot.respawnTimer=RESPAWN_S;
            spawnBurst(bot.x,bot.y,bot.teamCol,28,true);
            lp.kills++;lp.streak++;lp.score+=100;
            coins+=50;updCoinsHud();
            gameScores[myTeam]=(gameScores[myTeam]||0)+1;
            wsSend({type:'kill',victim_id:bot.id,killer_id:myId,streak:lp.streak});
            if(STREAK_NAMES[lp.streak])showStreak(STREAK_NAMES[lp.streak]);
            updStatsHud();updScoreHud();
          }
          return;
        }
      }
      // Hit remote human enemies
      for(const[rid,r]of Object.entries(remotePlayers)){
        if(!r||r.dead||r.team===myTeam||rid===myId)continue;
        if(Math.hypot(r.x-this.x,r.y-this.y)<RAD+5){
          this.dead=true;spawnBurst(this.x,this.y,this.col,10);
          r._hp=Math.max(0,(r._hp>0?r._hp:MAX_HP)-this.dmg);
          if(r._hp<=0){
            r._hp=MAX_HP;coins+=50;updCoinsHud();
            wsSend({type:'kill',victim_id:rid,killer_id:myId,streak:(lp.streak||0)+1});
          }
          return;
        }
      }
    }
  }
  draw(){
    const a=Math.min(1,this.life*3);
    ctx.save();ctx.globalAlpha=a*.8;
    ctx.strokeStyle=this.col;ctx.lineWidth=this.tw;
    ctx.shadowColor=this.col;ctx.shadowBlur=this.gunId==='sniper'?12:5;
    ctx.beginPath();ctx.moveTo(this.px,this.py);ctx.lineTo(this.x,this.y);ctx.stroke();
    ctx.shadowBlur=14;ctx.fillStyle='#fff';
    ctx.beginPath();ctx.arc(this.x,this.y,this.tw,0,Math.PI*2);ctx.fill();
    ctx.restore();
  }
}

class Particle{
  constructor(x,y,col,big){
    this.x=x;this.y=y;this.col=col;
    const a=Math.random()*Math.PI*2,s=big?110+Math.random()*290:50+Math.random()*180;
    this.vx=Math.cos(a)*s;this.vy=Math.sin(a)*s;
    this.life=big?.4+Math.random()*.5:.2+Math.random()*.3;
    this.ml=this.life;this.r=big?2+Math.random()*5:1.5+Math.random()*3;
  }
  update(dt){this.x+=this.vx*dt;this.y+=this.vy*dt;this.vx*=.87;this.vy*=.87;this.life-=dt;}
  draw(){const a=Math.max(0,this.life/this.ml);ctx.globalAlpha=a;ctx.beginPath();ctx.arc(this.x,this.y,this.r*Math.sqrt(a),0,Math.PI*2);ctx.fillStyle=this.col;ctx.fill();ctx.globalAlpha=1;}
}

class Powerup{
  constructor(x,y,type){this.x=x;this.y=y;this.type=type;this.anim=Math.random()*Math.PI*2;this.dead=false;}
  draw(){
    const bob=Math.sin(Date.now()*.002+this.anim)*4;
    ctx.save();ctx.translate(this.x,this.y+bob);
    ctx.shadowColor=this.type.col;ctx.shadowBlur=14;
    ctx.beginPath();ctx.arc(0,0,11,0,Math.PI*2);
    ctx.fillStyle='rgba(5,5,8,.9)';ctx.fill();
    ctx.strokeStyle=this.type.col;ctx.lineWidth=2;ctx.stroke();
    ctx.shadowBlur=0;
    ctx.font='12px sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';
    ctx.fillText(this.type.icon,0,0);
    ctx.restore();
  }
}

function spawnBurst(x,y,col,n,big=false){for(let i=0;i<n;i++)particles.push(new Particle(x,y,col,big));}
function shake(mag){shakeMag=Math.max(shakeMag,mag);}

// ══════════════════════════════════════════════════════
//  LOCAL PLAYER
// ══════════════════════════════════════════════════════
function makeLocalPlayer(info){
  return{
    id:myId, name:info.name||'PLAYER',
    team:info.team||'red', skin:SKINS.find(s=>s.id===info.skin)||SKINS[0],
    gun:GUNS.find(g=>g.id===(gameConfig.mode==='gun'?'pistol':info.gun))||GUNS[0],
    x:info.team==='red'?100:AW-100, y:AH/2,
    angle:0, vx:0, vy:0,
    hp:MAX_HP, dead:false, hitFlash:0,
    kills:0, deaths:0, streak:0, score:0,
    ammo:0, reloading:false, reloadTimer:0, fireCooldown:0,
    atkCD:0, attacking:false, atkAngle:0, atkTimer:0, hitTargets:new Set(),
    powSpeed:false, powSpeedTimer:0,
    powDamage:false, powDamageTimer:0,
    shield:0, hasFlag:false,
    respawnTimer:0,
    gunIndex:0,
    get teamCol(){return this.team==='red'?'#FF3131':'#1E90FF';},
    get teamGlow(){return this.team==='red'?'rgba(255,49,49,.55)':'rgba(30,144,255,.55)';},
  };
}

function setGun(gId){
  const g=GUNS.find(x=>x.id===gId)||GUNS[0];
  localPlayer.gun=g;
  localPlayer.ammo=g.maxAmmo;
  localPlayer.reloading=false;
  localPlayer.reloadTimer=0;
  localPlayer.fireCooldown=0;
  updAmmoHud();
}
localPlayer=null;

// ══════════════════════════════════════════════════════
//  BOT ENTITY (host-controlled)
// ══════════════════════════════════════════════════════
class BotEntity{
  constructor(id,name,team,skinId,gunId){
    this.id=id;this.name=name;this.team=team;this.bot=true;
    this.skin=SKINS.find(s=>s.id===skinId)||SKINS[Math.floor(Math.random()*SKINS.length)];
    this.gun=GUNS.find(g=>g.id===gunId)||GUNS[Math.floor(Math.random()*GUNS.length)];
    this.x=team==='red'?80+Math.random()*120:AW-80-Math.random()*120;
    this.y=100+Math.random()*(AH-200);
    this.angle=team==='red'?0:Math.PI;
    this.vx=0;this.vy=0;
    this.hp=MAX_HP;this.dead=false;this.hitFlash=0;
    this.kills=0;this.deaths=0;this.score=0;this.streak=0;
    this.ammo=this.gun.maxAmmo;this.reloading=false;this.reloadTimer=0;this.fireCooldown=0;
    this.atkCD=0;this.attacking=false;this.atkTimer=0;
    this.powSpeed=false;this.powDamage=false;
    this.wanderAngle=Math.random()*Math.PI*2;this.wanderTimer=0;
    this.aggro=.4+Math.random()*.6;
    this.respawnTimer=0;this.botGunCD=0;
    this.stateTimer=0;this.state='chase';
    this._stuckTimer=0;this._lastX=this.x;this._lastY=this.y;this._forcedWander=0;
  }
  get teamCol(){return this.team==='red'?'#FF3131':'#1E90FF';}
  get teamGlow(){return this.team==='red'?'rgba(255,49,49,.55)':'rgba(30,144,255,.55)';}

  respawn(){
    let x,y,safe=false,t=0;
    while(!safe&&t<30){
      x=this.team==='red'?55+Math.random()*130:AW-55-Math.random()*130;
      y=80+Math.random()*(AH-160);safe=true;
      for(const ob of currentMap.obstacles)if(x>ob.x-RAD&&x<ob.x+ob.w+RAD&&y>ob.y-RAD&&y<ob.y+ob.h+RAD){safe=false;break;}
      t++;
    }
    this.x=x;this.y=y;this.hp=MAX_HP;this.dead=false;this.hitFlash=0;
    this.ammo=this.gun.maxAmmo;this.reloading=false;this.reloadTimer=0;this.fireCooldown=0;this.atkCD=0;
  }

  update(dt){
    if(this.hitFlash>0)this.hitFlash=Math.max(0,this.hitFlash-dt);
    if(this.fireCooldown>0)this.fireCooldown-=dt;
    if(this.atkCD>0)this.atkCD-=dt;
    if(this.reloading){this.reloadTimer-=dt;if(this.reloadTimer<=0){this.reloading=false;this.ammo=this.gun.maxAmmo;}}
    if(this.dead){
      this.respawnTimer-=dt;
      if(this.respawnTimer<=0)this.respawn();
      return;
    }
    // AI
    const AVOID_R=70;let avoidX=0,avoidY=0;
    const ep=55;
    if(this.x<ep)avoidX+=(ep-this.x)/ep;if(this.x>AW-ep)avoidX-=(this.x-(AW-ep))/ep;
    if(this.y<ep)avoidY+=(ep-this.y)/ep;if(this.y>AH-ep)avoidY-=(this.y-(AH-ep))/ep;
    for(const ob of currentMap.obstacles){
      const cx=Math.max(ob.x,Math.min(ob.x+ob.w,this.x)),cy=Math.max(ob.y,Math.min(ob.y+ob.h,this.y));
      const dx=this.x-cx,dy=this.y-cy,dist=Math.hypot(dx,dy);
      if(dist<AVOID_R&&dist>0){const str=(AVOID_R-dist)/AVOID_R;avoidX+=dx/dist*str*2.2;avoidY+=dy/dist*str*2.2;}
    }
    const avoidLen=Math.hypot(avoidX,avoidY);
    if(avoidLen>1){avoidX/=avoidLen;avoidY/=avoidLen;}
    this._stuckTimer+=dt;
    if(this._stuckTimer>0.6){
      const moved=Math.hypot(this.x-this._lastX,this.y-this._lastY);
      if(moved<8){this.wanderAngle=Math.random()*Math.PI*2;this._forcedWander=.7;}
      this._lastX=this.x;this._lastY=this.y;this._stuckTimer=0;
    }
    if(this._forcedWander>0){
      this._forcedWander-=dt;
      this.vx=Math.cos(this.wanderAngle)*155+avoidX*155;
      this.vy=Math.sin(this.wanderAngle)*155+avoidY*155;
      this.angle=this.wanderAngle;
    } else {
      // Target: prefer local player if enemy team
      let target=null;
      if(localPlayer&&!localPlayer.dead&&localPlayer.team!==this.team)target=localPlayer;
      else{
        let best=Infinity;
        for(const b of botEntities){
          if(b===this||b.team===this.team||b.dead)continue;
          const d=Math.hypot(b.x-this.x,b.y-this.y);
          if(d<best){best=d;target=b;}
        }
      }
      if(!target){
        this.wanderTimer-=dt;
        if(this.wanderTimer<=0){this.wanderAngle=(Math.random()-.5)*Math.PI*2;this.wanderTimer=.5+Math.random()*.8;}
        this.vx=Math.cos(this.wanderAngle)*75+avoidX*75;
        this.vy=Math.sin(this.wanderAngle)*75+avoidY*75;
        this.angle=Math.atan2(this.vy,this.vx);
      } else {
        const tx=target.x,ty=target.y,d=Math.hypot(tx-this.x,ty-this.y);
        const toT=Math.atan2(ty-this.y,tx-this.x);
        this.angle=toT;
        this.stateTimer-=dt;
        if(this.stateTimer<=0){
          if(this.hp<=1&&d>120)this.state='retreat';
          else if(d<SWORD_R+22)this.state='strafe';
          else this.state='chase';
          this.stateTimer=.35+Math.random()*.5;
        }
        // Shoot
        this.botGunCD-=dt;
        if(d>90&&d<900&&!this.reloading&&this.ammo>0&&this.botGunCD<=0){
          const lead=d/this.gun.spd;
          const aimX=tx+target.vx*lead,aimY=ty+target.vy*lead;
          const aimA=Math.atan2(aimY-this.y,aimX-this.x);
          const inaccuracy=.25*(1-this.aggro);
          // Fire bullet
          const a=aimA+(Math.random()-.5)*inaccuracy;
          if(this.ammo>0&&this.fireCooldown<=0){
            bullets.push(new Bullet(this.x,this.y,a,this,this.gun,this.id));
            this.ammo--;this.fireCooldown=this.gun.rate;
            if(this.ammo===0){this.reloading=true;this.reloadTimer=this.gun.reload;}
          }
          this.botGunCD=this.gun.rate*(1+Math.random()*.4);
        }
        const spd=155;const sdir=this.kills%2===0?1:-1;
        const chaseX=Math.cos(toT),chaseY=Math.sin(toT);
        const aw=Math.min(1,avoidLen*1.4);
        switch(this.state){
          case'chase':this.vx=(chaseX*(1-aw)+avoidX*aw)*spd;this.vy=(chaseY*(1-aw)+avoidY*aw)*spd;break;
          case'strafe':const st=toT+Math.PI/2*sdir;this.vx=(Math.cos(st)*(1-aw)+avoidX*aw)*spd*.85;this.vy=(Math.sin(st)*(1-aw)+avoidY*aw)*spd*.85;break;
          case'retreat':this.vx=(-chaseX+avoidX*aw)*spd*.9;this.vy=(-chaseY+avoidY*aw)*spd*.9;break;
        }
      }
    }
    this.x+=this.vx*dt;this.y+=this.vy*dt;
    this.x=Math.max(RAD,Math.min(AW-RAD,this.x));
    this.y=Math.max(RAD,Math.min(AH-RAD,this.y));
    for(const ob of currentMap.obstacles)pushOut(this,ob);
    // Bots are simulated locally by every client - no network broadcast needed
  }

  draw(){
    if(this.dead)return;
    const c=ctx;
    c.save();c.translate(this.x,this.y);
    c.shadowColor=this.teamGlow;c.shadowBlur=12;
    c.beginPath();c.arc(0,0,RAD,0,Math.PI*2);
    c.fillStyle=this.hitFlash>0?'#fff':this.teamCol;
    if(this.hitFlash>0)c.globalAlpha=.6+this.hitFlash*2;
    c.fill();c.globalAlpha=1;c.shadowBlur=0;
    c.beginPath();c.arc(0,0,RAD*.7,0,Math.PI*2);c.fillStyle=this.skin.bodyCol;c.fill();
    this._drawSkinDetail(c);
    const sa=this.angle;
    c.save();c.rotate(sa);
    drawGun(c, this.gun.id, this.gun.col, false);
    c.restore();
    c.beginPath();c.arc(Math.cos(this.angle)*(RAD+4),Math.sin(this.angle)*(RAD+4),2.5,0,Math.PI*2);c.fillStyle='rgba(255,255,255,.9)';c.fill();
    c.restore();
    const bw=36,bh=4,bx=this.x-bw/2,by=this.y-RAD-13;
    c.fillStyle='rgba(0,0,0,.65)';c.fillRect(bx-1,by-1,bw+2,bh+2);
    c.fillStyle=this.hp===1?'#FF3131':this.teamCol;c.fillRect(bx,by,bw*(this.hp/MAX_HP),bh);
    c.font='700 9px Barlow Condensed,sans-serif';c.textAlign='center';c.fillStyle='rgba(200,210,230,.6)';
    c.fillText(this.name+' 🤖',this.x,this.y-RAD-18);
  }
  _drawSkinDetail(c){
    const s=this.skin;c.save();
    switch(s.shape){
      case'rings':c.beginPath();c.arc(0,0,RAD*.48,0,Math.PI*2);c.strokeStyle=s.accCol+'bb';c.lineWidth=1.5;c.stroke();c.beginPath();c.arc(0,0,RAD*.22,0,Math.PI*2);c.fillStyle=s.accCol;c.fill();break;
      case'eyes':[-5,5].forEach(ex=>{c.beginPath();c.arc(ex,-1.5,3,0,Math.PI*2);c.shadowColor='#F00';c.shadowBlur=7;c.fillStyle='#F00';c.fill();c.shadowBlur=0;});break;
      case'cross':c.beginPath();c.moveTo(-RAD*.42,0);c.lineTo(RAD*.42,0);c.moveTo(0,-RAD*.42);c.lineTo(0,RAD*.42);c.strokeStyle=s.accCol;c.lineWidth=2.5;c.stroke();break;
      default:c.beginPath();c.arc(0,0,3,0,Math.PI*2);c.fillStyle=s.accCol;c.fill();break;
    }
    c.restore();
  }
}

function pushOut(e,r){
  const cx=Math.max(r.x,Math.min(r.x+r.w,e.x)),cy=Math.max(r.y,Math.min(r.y+r.h,e.y));
  const dx=e.x-cx,dy=e.y-cy,d=Math.hypot(dx,dy);
  if(d===0)e.x+=RAD;else if(d<RAD){const ov=RAD-d;e.x+=(dx/d)*ov;e.y+=(dy/d)*ov;}
}

// ══════════════════════════════════════════════════════
//  REMOTE PLAYER DRAWING
// ══════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════
//  GUN DRAWING
// ══════════════════════════════════════════════════════
function drawGun(c, gunId, gunCol, attacking){
  // All guns drawn rotated — caller does c.save();c.rotate(angle) first
  c.shadowBlur=attacking?10:4;
  c.shadowColor=gunCol;
  switch(gunId){
    case'pistol':
      // Small compact pistol
      c.fillStyle=gunCol;c.strokeStyle=gunCol;
      c.fillRect(RAD,  -3, 16, 6);   // body
      c.fillRect(RAD+4, 3,  6, 5);   // grip
      c.fillRect(RAD+12,-3, 5, 3);   // barrel
      break;
    case'smg':
      // Boxy SMG with extended mag
      c.fillStyle=gunCol;
      c.fillRect(RAD,   -3, 22, 5);  // body
      c.fillRect(RAD+3,  2,  5, 8);  // mag
      c.fillRect(RAD+18,-3,  8, 3);  // barrel
      c.fillStyle='rgba(255,255,255,.3)';
      c.fillRect(RAD+8, -2, 10, 3);  // sight rail
      break;
    case'shotgun':
      // Wide double barrel
      c.fillStyle=gunCol;
      c.fillRect(RAD,   -5, 28, 4);  // top barrel
      c.fillRect(RAD,    1, 28, 4);  // bottom barrel
      c.fillRect(RAD+3, -6,  8,14);  // stock/receiver
      c.fillStyle='rgba(0,0,0,.4)';
      c.fillRect(RAD+24,-5,  4, 4);  // muzzle top
      c.fillRect(RAD+24, 1,  4, 4);  // muzzle bot
      break;
    case'sniper':
      // Long thin rifle with scope
      c.fillStyle=gunCol;
      c.fillRect(RAD,   -2, 38, 4);  // long barrel
      c.fillRect(RAD+2, -5,  9, 3);  // scope body
      c.fillRect(RAD+4, -8,  5, 3);  // scope top
      c.fillRect(RAD+6,  2,  5, 6);  // grip
      c.strokeStyle=gunCol;c.lineWidth=1;
      c.beginPath();c.moveTo(RAD+4,-5);c.lineTo(RAD+9,-5);c.stroke(); // scope line
      break;
    case'assault':
    default:
      // Assault rifle — medium with mag
      c.fillStyle=gunCol;
      c.fillRect(RAD,   -3, 26, 5);  // body
      c.fillRect(RAD+5,  2,  6, 7);  // mag
      c.fillRect(RAD+20,-3,  9, 3);  // barrel
      c.fillRect(RAD,   -3,  7, 5);  // stock
      c.fillStyle='rgba(255,255,255,.25)';
      c.fillRect(RAD+10,-2, 10, 2);  // rail
      break;
  }
  c.shadowBlur=0;
}
function drawRemotePlayer(p){
  if(p.dead) return;
  const c=ctx;
  const skin=SKINS.find(s=>s.id===p.skin)||SKINS[0];
  const teamCol=p.team==='red'?'#FF3131':'#1E90FF';
  const teamGlow=p.team==='red'?'rgba(255,49,49,.55)':'rgba(30,144,255,.55)';
  c.save();c.translate(p.x,p.y);
  c.shadowColor=teamGlow;c.shadowBlur=12;
  c.beginPath();c.arc(0,0,RAD,0,Math.PI*2);c.fillStyle=teamCol;c.fill();c.shadowBlur=0;
  c.beginPath();c.arc(0,0,RAD*.7,0,Math.PI*2);c.fillStyle=skin.bodyCol;c.fill();
  c.beginPath();c.arc(0,0,3.5,0,Math.PI*2);c.fillStyle=skin.accCol;c.fill();
  if(p.attacking){
    const arcEnd=p.angle-SWORD_ARC+SWORD_ARC*2;
    c.beginPath();c.moveTo(0,0);c.arc(0,0,SWORD_R,p.angle-SWORD_ARC,arcEnd);c.closePath();
    c.fillStyle=p.team==='red'?'rgba(255,49,49,.17)':'rgba(30,144,255,.17)';c.fill();
  }
  c.save();c.rotate(p.angle);
  drawGun(c, p.gunId||'pistol', GUNS.find(g=>g.id===(p.gunId||'pistol'))?.col||'#94A3B8', false);
  c.restore();
  c.restore();
  const bw=36,bh=4,bx=p.x-bw/2,by=p.y-RAD-13;
  c.fillStyle='rgba(0,0,0,.65)';c.fillRect(bx-1,by-1,bw+2,bh+2);
  c.fillStyle=p.hp===1?'#FF3131':teamCol;c.fillRect(bx,by,bw*(Math.max(0,p.hp)/3),bh);
  c.font='700 9px Barlow Condensed,sans-serif';c.textAlign='center';
  c.fillStyle='rgba(200,210,230,.6)';c.fillText(p.name,p.x,p.y-RAD-18);
}

// ══════════════════════════════════════════════════════
//  LOCAL PLAYER DRAW
// ══════════════════════════════════════════════════════
function drawLocalPlayer(){
  if(!localPlayer||localPlayer.dead)return;
  const p=localPlayer,c=ctx;
  c.save();c.translate(p.x,p.y);
  if(p.attacking){
    const t=p.atkTimer/ATK_DUR,arcEnd=p.atkAngle-SWORD_ARC+(1-t)*SWORD_ARC*2;
    c.beginPath();c.moveTo(0,0);c.arc(0,0,SWORD_R,p.atkAngle-SWORD_ARC,arcEnd);c.closePath();
    c.fillStyle=p.team==='red'?'rgba(255,49,49,.18)':'rgba(30,144,255,.18)';c.fill();
  }
  c.shadowColor=p.skin.glowCol;c.shadowBlur=28;
  c.beginPath();c.arc(0,0,RAD,0,Math.PI*2);
  c.fillStyle=p.hitFlash>0?'#fff':p.teamCol;
  if(p.hitFlash>0)c.globalAlpha=.6+p.hitFlash*2;
  c.fill();c.globalAlpha=1;c.shadowBlur=0;
  c.beginPath();c.arc(0,0,RAD*.7,0,Math.PI*2);c.fillStyle=p.skin.bodyCol;c.fill();
  // Skin detail
  const s=p.skin;c.save();
  switch(s.shape){
    case'rings':c.beginPath();c.arc(0,0,RAD*.48,0,Math.PI*2);c.strokeStyle=s.accCol+'bb';c.lineWidth=1.5;c.stroke();c.beginPath();c.arc(0,0,RAD*.22,0,Math.PI*2);c.fillStyle=s.accCol;c.fill();break;
    case'eyes':[-5,5].forEach(ex=>{c.beginPath();c.arc(ex,-1.5,3,0,Math.PI*2);c.shadowColor='#F00';c.shadowBlur=7;c.fillStyle='#F00';c.fill();c.shadowBlur=0;});break;
    case'fire':for(let i=0;i<8;i++){const a=(i/8)*Math.PI*2+Date.now()*.004;c.beginPath();c.arc(Math.cos(a)*RAD*.4,Math.sin(a)*RAD*.4,2,0,Math.PI*2);c.fillStyle=i%2===0?'#F60':'#FD0';c.fill();}break;
    case'cross':c.beginPath();c.moveTo(-RAD*.42,0);c.lineTo(RAD*.42,0);c.moveTo(0,-RAD*.42);c.lineTo(0,RAD*.42);c.strokeStyle=s.accCol;c.lineWidth=2.5;c.shadowColor=s.accCol;c.shadowBlur=5;c.stroke();c.shadowBlur=0;break;
    case'zap':c.beginPath();c.moveTo(-RAD*.3,-RAD*.3);c.lineTo(RAD*.08,0);c.lineTo(-RAD*.15,RAD*.3);c.strokeStyle=s.accCol;c.lineWidth=2.2;c.shadowColor=s.accCol;c.shadowBlur=6;c.stroke();c.shadowBlur=0;break;
    default:c.beginPath();c.arc(0,0,4,0,Math.PI*2);c.fillStyle='rgba(255,255,255,.5)';c.fill();break;
  }
  c.restore();
  if(p.shield>0){c.beginPath();c.arc(0,0,RAD+7,0,Math.PI*2);c.strokeStyle='rgba(0,245,255,.7)';c.lineWidth=2.5;c.shadowColor='#00F5FF';c.shadowBlur=10;c.stroke();c.shadowBlur=0;}
  c.beginPath();c.arc(0,0,RAD+5,0,Math.PI*2);c.strokeStyle='rgba(255,230,0,.8)';c.lineWidth=2;c.stroke();
  const sa=p.attacking?p.atkAngle-SWORD_ARC+(1-p.atkTimer/ATK_DUR)*SWORD_ARC*2:p.angle;
  c.save();c.rotate(sa);
  drawGun(c, p.gun.id, p.attacking?'#FFE600':p.gun.col, p.attacking);
  c.restore();
  c.beginPath();c.arc(Math.cos(p.angle)*(RAD+4),Math.sin(p.angle)*(RAD+4),2.5,0,Math.PI*2);c.fillStyle='rgba(255,255,255,.9)';c.fill();
  c.restore();
  const bw=36,bh=4,bx=p.x-bw/2,by=p.y-RAD-13;
  c.fillStyle='rgba(0,0,0,.65)';c.fillRect(bx-1,by-1,bw+2,bh+2);
  c.fillStyle=p.hp===1?'#FF3131':p.teamCol;c.fillRect(bx,by,bw*(Math.max(0,p.hp)/MAX_HP),bh);
  c.font='bold 11px Barlow Condensed,sans-serif';c.textAlign='center';c.fillStyle='rgba(255,230,0,.95)';
  c.fillText('▶ YOU',p.x,p.y-RAD-18);
}

// ══════════════════════════════════════════════════════
//  ARENA DRAW
// ══════════════════════════════════════════════════════
function drawArena(){
  const t=currentMap.theme,c=ctx;
  c.fillStyle=t.floor;c.fillRect(0,0,AW,AH);
  c.strokeStyle=t.grid;c.lineWidth=1;
  for(let x=0;x<=AW;x+=55){c.beginPath();c.moveTo(x,0);c.lineTo(x,AH);c.stroke();}
  for(let y=0;y<=AH;y+=55){c.beginPath();c.moveTo(0,y);c.lineTo(AW,y);c.stroke();}
  c.strokeStyle=t.wa;c.lineWidth=2.5;c.strokeRect(3,3,AW-6,AH-6);
  const cs=22,bpts=[[0,0,1,1],[AW,0,-1,1],[0,AH,1,-1],[AW,AH,-1,-1]];
  bpts.forEach(([bx,by,sx,sy])=>{c.beginPath();c.moveTo(bx+sx*cs,by);c.lineTo(bx,by);c.lineTo(bx,by+sy*cs);c.stroke();});
  c.fillStyle='rgba(255,49,49,.04)';c.fillRect(0,0,SPAWN_C,AH);
  c.fillStyle='rgba(30,144,255,.04)';c.fillRect(AW-SPAWN_C,0,SPAWN_C,AH);
  c.setLineDash([8,10]);
  c.strokeStyle='rgba(255,49,49,.12)';c.lineWidth=1;c.beginPath();c.moveTo(SPAWN_C,0);c.lineTo(SPAWN_C,AH);c.stroke();
  c.strokeStyle='rgba(30,144,255,.12)';c.beginPath();c.moveTo(AW-SPAWN_C,0);c.lineTo(AW-SPAWN_C,AH);c.stroke();
  c.setLineDash([]);
  c.font='bold 10px Barlow Condensed,sans-serif';c.textAlign='center';
  c.fillStyle='rgba(255,49,49,.22)';c.fillText('RED SPAWN',SPAWN_C/2,24);
  c.fillStyle='rgba(30,144,255,.22)';c.fillText('BLUE SPAWN',AW-SPAWN_C/2,24);
  c.save();c.globalAlpha=.02;c.font='bold 200px Barlow Condensed,sans-serif';c.textAlign='center';c.fillStyle='#fff';c.fillText('RIVALS',AW/2,AH/2+65);c.restore();
  if(gameConfig&&gameConfig.mode==='koth'){
    const zx=AW/2,zy=AH/2,zr=85;
    c.beginPath();c.arc(zx,zy,zr,0,Math.PI*2);c.fillStyle='rgba(255,230,0,.04)';c.fill();
    c.strokeStyle='rgba(255,230,0,.35)';c.lineWidth=2;c.setLineDash([10,8]);c.stroke();c.setLineDash([]);
    c.font='bold 9px Barlow Condensed,sans-serif';c.textAlign='center';c.fillStyle='rgba(255,230,0,.35)';c.fillText('KING',zx,zy+zr+14);
  }
  for(const ob of currentMap.obstacles){
    c.fillStyle=t.wf;c.fillRect(ob.x,ob.y,ob.w,ob.h);
    c.strokeStyle=t.ws;c.lineWidth=2;c.strokeRect(ob.x,ob.y,ob.w,ob.h);
    c.save();c.beginPath();c.rect(ob.x,ob.y,ob.w,ob.h);c.clip();
    c.strokeStyle=t.st;c.lineWidth=1;
    for(let dd=-ob.h;dd<ob.w+ob.h;dd+=18){c.beginPath();c.moveTo(ob.x+dd,ob.y);c.lineTo(ob.x+dd+ob.h,ob.y+ob.h);c.stroke();}
    c.restore();
    const cs2=9;c.strokeStyle=t.wa;c.lineWidth=2;
    [[ob.x,ob.y,1,1],[ob.x+ob.w,ob.y,-1,1],[ob.x,ob.y+ob.h,1,-1],[ob.x+ob.w,ob.y+ob.h,-1,-1]].forEach(([bx,by,sx,sy])=>{c.beginPath();c.moveTo(bx+sx*cs2,by);c.lineTo(bx,by);c.lineTo(bx,by+sy*cs2);c.stroke();});
  }
}

// ══════════════════════════════════════════════════════
//  GAME LOOP
// ══════════════════════════════════════════════════════
let stateInterval=null;

function startGame(myInfo, allPlayers){
  allGamePlayers=allPlayers;
  currentMap=genMap(mapSeed);
  document.getElementById('mapname').textContent=currentMap.theme.name;

  gameScores={red:0,blue:0};
  kothScore={red:0,blue:0};
  ctfCaps={red:0,blue:0};
  feedItems=[];
  powerups=[];bullets=[];particles=[];
  botEntities=[];

  document.getElementById('hfeed').innerHTML='';
  document.getElementById('koth-bar').classList.toggle('show',gameConfig.mode==='koth');
  document.getElementById('ctf-hud').classList.toggle('show',gameConfig.mode==='ctf');
  document.getElementById('hmode-tag').textContent={tdm:'⚔️ TDM',ffa:'💀 FFA',koth:'👑 KOTH',ctf:'🚩 CTF',gun:'🔫 GUN GAME','1v1':'🥊 1v1'}[gameConfig.mode]||'';

  // Setup local player
  localPlayer=makeLocalPlayer(myInfo);
  if(gameConfig.mode==='gun'){setGun('pistol');}else{setGun(myInfo.gun||'pistol');}

  // Every client creates bots locally so everyone sees them
  isHost=(myId===hostId);
  allPlayers.filter(p=>p.bot).forEach(b=>{
    const bot=new BotEntity(b.id,b.name,b.team,b.skin||'phantom',b.gun||'pistol');
    botEntities.push(bot);
  });

  updScoreHud();updStatsHud();updHpHud();updAmmoHud();
  hideRespawnUI();

  // Spawn initial powerups
  for(let i=0;i<3;i++)spawnPowerup();

  showScreen('s-game');
  running=true;lastTs=performance.now();

  // Send state every 50ms
  if(stateInterval)clearInterval(stateInterval);
  stateInterval=setInterval(sendMyState,50);

  requestAnimationFrame(loop);
}

function sendMyState(){
  if(!localPlayer||!running)return;
  wsSend({type:'state',x:localPlayer.x,y:localPlayer.y,angle:localPlayer.angle,
    hp:localPlayer.hp,dead:localPlayer.dead,vx:localPlayer.vx,vy:localPlayer.vy,
    gunId:localPlayer.gun.id,ammo:localPlayer.ammo,reloading:localPlayer.reloading,
    hasFlag:localPlayer.hasFlag,attacking:localPlayer.attacking,
    skin:localPlayer.skin.id,name:localPlayer.name||'PLAYER',team:localPlayer.team});
}

function loop(ts){
  if(!running)return;
  if(paused){requestAnimationFrame(loop);return;}
  const dt=Math.min((ts-lastTs)/1000,.065);lastTs=ts;

  // Decay feed
  feedItems=feedItems.filter(f=>{f.life-=dt;return f.life>0;});

  // Shake
  if(shakeMag>.1){shakeX=(Math.random()-.5)*shakeMag*2;shakeY=(Math.random()-.5)*shakeMag*2;shakeMag*=.78;}
  else{shakeX=0;shakeY=0;shakeMag=0;}

  // Powerup spawn
  powupTimer+=dt;if(powupTimer>=9){powupTimer=0;if(powerups.filter(p=>!p.dead).length<4)spawnPowerup();}
  powerups=powerups.filter(p=>!p.dead);

  // All clients simulate bots locally
  botEntities.forEach(b=>b.update(dt));

  // Update local player
  if(localPlayer&&!localPlayer.dead){
    updateLocalPlayer(dt);
    // Pick up powerups
    powerups.forEach(p=>{if(!p.dead&&Math.hypot(p.x-localPlayer.x,p.y-localPlayer.y)<22){p.dead=true;applyPowerup(p.type);}});
  } else if(localPlayer&&localPlayer.dead){
    localPlayer.respawnTimer-=dt;
    setRespawnUI(Math.ceil(localPlayer.respawnTimer));
    if(localPlayer.respawnTimer<=0){
      respawnLocal();
      hideRespawnUI();
    }
    if(localPlayer.hitFlash>0)localPlayer.hitFlash=Math.max(0,localPlayer.hitFlash-dt);
  }

  // Update powerup timers
  if(localPlayer){
    if(localPlayer.powSpeed){localPlayer.powSpeedTimer-=dt;if(localPlayer.powSpeedTimer<=0)localPlayer.powSpeed=false;}
    if(localPlayer.powDamage){localPlayer.powDamageTimer-=dt;if(localPlayer.powDamageTimer<=0)localPlayer.powDamage=false;}
  }

  // Bullets
  bullets=bullets.filter(b=>{if(b.dead)return false;b.update(dt);return!b.dead;});
  particles=particles.filter(p=>p.life>0);particles.forEach(p=>p.update(dt));

  // KOTH (host ticks, all clients update via server)
  if(gameConfig&&gameConfig.mode==='koth'&&localPlayer&&!localPlayer.dead){
    const zx=AW/2,zy=AH/2,zr=85;
    const inZone=Math.hypot(localPlayer.x-zx,localPlayer.y-zy)<zr;
    if(inZone){
      localKothTimer+=dt;
      if(isHost){
        kothScore[localPlayer.team]=(kothScore[localPlayer.team]||0)+dt;
        wsSend({type:'koth_tick',red:kothScore.red,blue:kothScore.blue});
        updKothHud();
      }
    }
  }

  updPowHud();
  render();
  requestAnimationFrame(loop);
}

function updateLocalPlayer(dt){
  const p=localPlayer;
  if(p.hitFlash>0)p.hitFlash=Math.max(0,p.hitFlash-dt);
  if(p.fireCooldown>0)p.fireCooldown-=dt;
  if(p.atkCD>0)p.atkCD-=dt;
  if(p.reloading){p.reloadTimer-=dt;if(p.reloadTimer<=0){p.reloading=false;p.ammo=p.gun.maxAmmo;updAmmoHud();}}
  if(p.attacking){p.atkTimer-=dt;if(p.atkTimer<=0){p.attacking=false;}else{// melee vs remote & bots
    checkMeleeHits();
  }}
  let dx=0,dy=0;
  if(keys['w']||keys['arrowup'])dy-=1;if(keys['s']||keys['arrowdown'])dy+=1;
  if(keys['a']||keys['arrowleft'])dx-=1;if(keys['d']||keys['arrowright'])dx+=1;
  const l=Math.hypot(dx,dy);if(l>0){dx/=l;dy/=l;}
  const spd=P_SPEED*(p.powSpeed?1.55:1);
  p.vx=dx*spd;p.vy=dy*spd;
  const wx=(mouse.x-ox)/scl,wy=(mouse.y-oy)/scl;
  p.angle=Math.atan2(wy-p.y,wx-p.x);
  if(mousedown)doShoot();
  p.x+=p.vx*dt;p.y+=p.vy*dt;
  p.x=Math.max(RAD,Math.min(AW-RAD,p.x));
  p.y=Math.max(RAD,Math.min(AH-RAD,p.y));
  for(const ob of currentMap.obstacles)pushOut(p,ob);
}

function checkMeleeHits(){
  const p=localPlayer;
  if(!p.hitTargets)p.hitTargets=new Set();
  // Check remote players
  Object.values(remotePlayers).forEach(r=>{
    if(r.team===p.team||r.dead||p.hitTargets.has(r.id))return;
    const d=Math.hypot(r.x-p.x,r.y-p.y);
    if(d>SWORD_R+RAD)return;
    let diff=Math.atan2(r.y-p.y,r.x-p.x)-p.atkAngle;
    while(diff>Math.PI)diff-=Math.PI*2;while(diff<-Math.PI)diff+=Math.PI*2;
    if(Math.abs(diff)<=SWORD_ARC){
      p.hitTargets.add(r.id);
      // Signal server to deal damage
      wsSend({type:'kill',victim_id:r.id,killer_id:myId,streak:p.streak+1,weapon:'sword'});
    }
  });
  // Check bots
  botEntities.forEach(b=>{
    if(b.team===p.team||b.dead||p.hitTargets.has(b.id))return;
    const d=Math.hypot(b.x-p.x,b.y-p.y);
    if(d>SWORD_R+RAD)return;
    let diff=Math.atan2(b.y-p.y,b.x-p.x)-p.atkAngle;
    while(diff>Math.PI)diff-=Math.PI*2;while(diff<-Math.PI)diff+=Math.PI*2;
    if(Math.abs(diff)<=SWORD_ARC){
      p.hitTargets.add(b.id);
      b.hp=Math.max(0,b.hp-1);
      if(b.hp<=0&&!b.dead){
        b.dead=true;b.respawnTimer=RESPAWN_S;b.deaths++;
        spawnBurst(b.x,b.y,b.teamCol,28,true);
        p.kills++;p.streak++;p.score+=100;
        gameScores[p.team]=(gameScores[p.team]||0)+1;
        wsSend({type:'kill',victim_id:b.id,killer_id:myId,streak:p.streak});
        if(STREAK_NAMES[p.streak])showStreak(STREAK_NAMES[p.streak]);
        updStatsHud();updScoreHud();
      }
    }
  });
}

function respawnLocal(){
  const p=localPlayer;
  p.hp=MAX_HP;p.dead=false;p.hitFlash=0;p.shield=0;
  p.ammo=p.gun.maxAmmo;p.reloading=false;p.reloadTimer=0;p.fireCooldown=0;p.atkCD=0;p.streak=0;
  p.x=p.team==='red'?80+Math.random()*120:AW-80-Math.random()*120;
  p.y=100+Math.random()*(AH-200);
  for(const ob of currentMap.obstacles)pushOut(p,ob);
  updHpHud();updAmmoHud();
}

function doShoot(){
  const p=localPlayer;
  if(!p||!p.gun||p.fireCooldown>0||p.reloading||p.ammo<=0||p.dead)return;
  for(let i=0;i<p.gun.pellets;i++){
    const spread=(Math.random()-.5)*p.gun.spread*2+(p.gun.pellets>1?(i-2)*.06:0);
    bullets.push(new Bullet(p.x,p.y,p.angle+spread,p,p.gun,myId));
  }
  p.ammo--;p.fireCooldown=p.gun.rate;
  if(p.ammo===0){p.reloading=true;p.reloadTimer=p.gun.reload;}
  shake(p.gun.id==='shotgun'?5:p.gun.id==='sniper'?4:1);
  updAmmoHud();
}

function doMelee(){
  const p=localPlayer;
  if(p.atkCD>0||p.attacking||p.dead)return;
  p.attacking=true;p.atkAngle=p.angle;p.atkTimer=ATK_DUR;p.atkCD=SWORD_CD;
  p.hitTargets=new Set();
}

function doReload(){const p=localPlayer;if(p.reloading||p.ammo===p.gun.maxAmmo||p.dead)return;p.reloading=true;p.reloadTimer=p.gun.reload;}

function spawnPowerup(){
  let x,y,safe=false,t=0;
  while(!safe&&t<30){
    x=SPAWN_C+30+Math.random()*(AW-SPAWN_C*2-60);y=60+Math.random()*(AH-120);safe=true;
    for(const ob of currentMap.obstacles)if(x>ob.x-20&&x<ob.x+ob.w+20&&y>ob.y-20&&y<ob.y+ob.h+20){safe=false;break;}
    t++;
  }
  powerups.push(new Powerup(x,y,POW_TYPES[Math.floor(Math.random()*POW_TYPES.length)]));
}

function applyPowerup(type){
  const p=localPlayer;
  switch(type.id){
    case'health':p.hp=Math.min(MAX_HP,p.hp+1);updHpHud();break;
    case'speed':p.powSpeed=true;p.powSpeedTimer=5;break;
    case'damage':p.powDamage=true;p.powDamageTimer=5;break;
    case'shield':p.shield=2;updHpHud();break;
  }
  showPowBanner(type.label+'!');
  spawnBurst(p.x,p.y,type.col,10);
}

// ══════════════════════════════════════════════════════
//  RENDER
// ══════════════════════════════════════════════════════
function render(){
  scl=Math.min(canvas.width/AW,canvas.height/AH);
  ox=(canvas.width-AW*scl)/2;oy=(canvas.height-AH*scl)/2;
  ctx.clearRect(0,0,canvas.width,canvas.height);
  ctx.fillStyle='#000';ctx.fillRect(0,0,canvas.width,canvas.height);
  ctx.save();ctx.translate(ox+shakeX,oy+shakeY);ctx.scale(scl,scl);
  drawArena();
  powerups.forEach(p=>{if(!p.dead)p.draw();});
  particles.forEach(p=>p.draw());
  bullets.forEach(b=>b.draw());
  botEntities.forEach(b=>b.draw());
  Object.values(remotePlayers).forEach(p=>{if(p&&!p.bot&&p.id!==myId)drawRemotePlayer(p);});
  drawLocalPlayer();
  ctx.restore();
}

// ══════════════════════════════════════════════════════
//  HUD
// ══════════════════════════════════════════════════════
function updScoreHud(){
  const m=gameConfig||{mode:'tdm',target:30};
  const t=m.target;
  if(m.mode==='tdm'||m.mode==='koth'||m.mode==='ctf'||m.mode==='1v1'){
    document.getElementById('sr').textContent=m.mode==='koth'?Math.floor(kothScore.red)+'s':m.mode==='ctf'?ctfCaps.red:gameScores.red;
    document.getElementById('sb2').textContent=m.mode==='koth'?Math.floor(kothScore.blue)+'s':m.mode==='ctf'?ctfCaps.blue:gameScores.blue;
    document.getElementById('sr').className='snum red';
    document.getElementById('sb2').className='snum blue';
    document.getElementById('ssep').innerHTML=(m.mode==='koth'?`ZONE<br>${t}s`:m.mode==='ctf'?`CAPS<br>OF ${t}`:`KILLS<br>TO ${t}`);
  } else if(m.mode==='ffa'||m.mode==='gun'){
    const myK=localPlayer?localPlayer.kills:0;
    document.getElementById('sr').textContent=myK;
    document.getElementById('sb2').textContent=t||'—';
    document.getElementById('sr').className='snum ffa';
    document.getElementById('sb2').className='snum ffa';
    document.getElementById('ssep').innerHTML=m.mode==='gun'?'WEAPON<br>TIER':'MY KILLS<br>TARGET';
  }
  // Big score pulse
  const rEl=document.getElementById('sr'),bEl=document.getElementById('sb2');
  rEl.style.fontSize=gameScores.red>=gameScores.blue?'1.8rem':'1.4rem';
  bEl.style.fontSize=gameScores.blue>gameScores.red?'1.8rem':'1.4rem';
}

function updStatsHud(){
  if(!localPlayer)return;
  document.getElementById('mk').textContent=localPlayer.kills;
  document.getElementById('md').textContent=localPlayer.deaths;
  document.getElementById('ms').textContent=localPlayer.streak;
}

function updHpHud(){
  if(!localPlayer)return;
  for(let i=1;i<=3;i++)document.getElementById('h'+i).classList.toggle('e',i>localPlayer.hp);
  document.getElementById('shield-pip').textContent=localPlayer.shield>0?'🛡️'.repeat(localPlayer.shield):'';
}

function updAmmoHud(){
  if(!localPlayer)return;
  const g=localPlayer.gun;
  document.getElementById('hgname').textContent=g.name;
  document.getElementById('hgname').style.color=g.col;
  const bp=document.getElementById('hbullets');bp.innerHTML='';
  for(let i=0;i<Math.min(g.maxAmmo,30);i++){
    const d=document.createElement('div');d.className='bpip';
    d.style.background=i<localPlayer.ammo?g.col:'rgba(255,255,255,.1)';
    d.style.height=g.id==='sniper'?'12px':'8px';
    bp.appendChild(d);
  }
  document.getElementById('hreload').style.display=localPlayer.reloading?'block':'none';
}

function updPowHud(){
  if(!localPlayer)return;
  const hp=document.getElementById('hpow');hp.innerHTML='';
  if(localPlayer.powSpeed){const s=document.createElement('span');s.textContent='⚡ SPEED';s.style.cssText='font-size:.8rem;color:#FFE600';hp.appendChild(s);}
  if(localPlayer.powDamage){const s=document.createElement('span');s.textContent=' 🔥 DMG AMP';s.style.cssText='font-size:.8rem;color:#F97316';hp.appendChild(s);}
}

function updKothHud(){
  const t=gameConfig?.target||20;
  document.getElementById('kr-s').textContent=Math.floor(kothScore.red)+'s';
  document.getElementById('kb-s').textContent=Math.floor(kothScore.blue)+'s';
  document.getElementById('kr-fill').style.width=(kothScore.red/t*100).toFixed(1)+'%';
  document.getElementById('kb-fill').style.width=(kothScore.blue/t*100).toFixed(1)+'%';
  updScoreHud();
}

function updCtfHud(){
  document.getElementById('ctf-r').textContent=ctfCaps.red;
  document.getElementById('ctf-b').textContent=ctfCaps.blue;
  updScoreHud();
}

function addFeed(killer, victim, team){
  feedItems.unshift({killer,victim,team,life:4.5});
  if(feedItems.length>5)feedItems.pop();
  document.getElementById('hfeed').innerHTML=feedItems.map(k=>
    `<div class="kf ${k.team||'ffa'}"><span style="color:rgba(255,255,255,.9)">${k.killer.slice(0,9)}</span><span style="color:var(--y)"> ⚔ </span><span style="color:var(--muted)">${k.victim.slice(0,9)}</span></div>`
  ).join('');
}

function setRespawnUI(n){document.getElementById('hrespawn').classList.add('show');document.getElementById('rsc').textContent=Math.max(0,n);}
function hideRespawnUI(){document.getElementById('hrespawn').classList.remove('show');}

let streakTO;
function showStreak(txt){
  clearTimeout(streakTO);
  document.getElementById('sb-text').textContent=txt;
  const b=document.getElementById('streak-banner');b.classList.remove('show');void b.offsetWidth;b.classList.add('show');
  streakTO=setTimeout(()=>b.classList.remove('show'),2200);
}
let powTO;
function showPowBanner(txt){
  clearTimeout(powTO);
  document.getElementById('pb-text').textContent=txt;
  const b=document.getElementById('pow-banner');b.classList.remove('show');void b.offsetWidth;b.classList.add('show');
  powTO=setTimeout(()=>b.classList.remove('show'),1800);
}

// ══════════════════════════════════════════════════════
//  END ROUND
// ══════════════════════════════════════════════════════
function triggerEnd(winnerText, winnerTeam, serverPlayers){
  if(!running)return;
  running=false;
  clearInterval(stateInterval);
  const won=winnerTeam===localPlayer?.team||(winnerTeam==='ffa'&&winnerText.includes(localPlayer?.name));
  const fl=document.getElementById('flash');
  fl.style.background=won?'rgba(255,230,0,.14)':'rgba(255,49,49,.14)';fl.style.opacity='1';
  setTimeout(()=>fl.style.opacity='0',700);
  setTimeout(()=>{
    const et=document.getElementById('et');
    et.textContent=won?'🏆 VICTORY! '+winnerText:winnerText+' WINS!';
    et.className='ewt '+(won?'you':winnerTeam==='ffa'?'ffa':winnerTeam);
    document.getElementById('es').textContent=(gameConfig?.mode||'TDM').toUpperCase()+' — ROUND OVER';
    buildScoreboard(serverPlayers||[]);
    showScreen('s-end');
  },850);
}

function buildScoreboard(serverPlayers){
  // Merge server stats
  const allP=[...serverPlayers].sort((a,b)=>b.score-a.score);
  if(!allP.length){
    // Build from local+remote
    const me={id:myId,name:localPlayer?.name||'YOU',team:localPlayer?.team||'red',
              kills:localPlayer?.kills||0,deaths:localPlayer?.deaths||0,score:localPlayer?.score||0};
    allP.push(me);
    Object.values(remotePlayers).forEach(r=>allP.push({id:r.id,name:r.name,team:r.team,kills:r.kills||0,deaths:r.deaths||0,score:r.score||0}));
    botEntities.forEach(b=>allP.push({id:b.id,name:b.name+'🤖',team:b.team,kills:b.kills,deaths:b.deaths,score:b.score}));
    allP.sort((a,b)=>b.kills-a.kills);
  }
  const tb=document.getElementById('sb-body');tb.innerHTML='';
  allP.forEach((p,i)=>{
    const kd=p.deaths>0?(p.kills/p.deaths).toFixed(2):p.kills+'.00';
    const teamLabel=p.team==='red'?`<span class="sb-team-r">RED</span>`:`<span class="sb-team-b">BLUE</span>`;
    const tr=document.createElement('tr');
    if(p.id===myId)tr.className='me';
    tr.innerHTML=`<td>${i===0?'👑':'#'+(i+1)}</td><td>${p.id===myId?'<b>'+p.name+' ◀</b>':p.name}</td><td>${teamLabel}</td><td class="sb-k">${p.kills}</td><td>${p.deaths}</td><td class="sb-kd">${kd}</td><td class="sb-sc">${(p.score||0).toLocaleString()}</td>`;
    tb.appendChild(tr);
  });
}

function doPlayAgain(){
  wsSend({type:'play_again'});
  showScreen('s-lobby');
}

// ══════════════════════════════════════════════════════
//  SCREENS + INPUT
// ══════════════════════════════════════════════════════
function showScreen(id){
  document.querySelectorAll('.screen').forEach(s=>s.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}

document.addEventListener('keydown',e=>{
  keys[e.key.toLowerCase()]=true;
  if(!localPlayer||localPlayer.dead||!running)return;
  if(e.key===' '){e.preventDefault();doShoot();}
  if(e.key.toLowerCase()==='f')doMelee();
  if(e.key.toLowerCase()==='r')doReload();
});
document.addEventListener('keyup',e=>keys[e.key.toLowerCase()]=false);
document.addEventListener('mousemove',e=>{mouse.x=e.clientX;mouse.y=e.clientY;});

window.addEventListener('load',()=>{
  canvas=document.getElementById('gc');
  ctx=canvas.getContext('2d');
  const resize=()=>{canvas.width=window.innerWidth;canvas.height=window.innerHeight;};
  resize();window.addEventListener('resize',resize);
  canvas.addEventListener('mousedown',e=>{
    mousedown=true;
    if(!localPlayer||localPlayer.dead||!running)return;
    if(e.button===0)doShoot();
    if(e.button===2)doMelee();
  });
  canvas.addEventListener('mouseup',()=>mousedown=false);
  canvas.addEventListener('mouseleave',()=>mousedown=false);
  canvas.addEventListener('contextmenu',e=>e.preventDefault());
});

// ═══════════════════════════════════════════════════════
//  COINS + SHOP
// ═══════════════════════════════════════════════════════
const SHOP_GUNS=[
  {id:'smg',    name:'SMG',     price:150, desc:'Rapid fire, medium range',     icon:'🔧'},
  {id:'shotgun',name:'SHOTGUN', price:200, desc:'Massive close-range damage',   icon:'💥'},
  {id:'assault',name:'ASSAULT', price:250, desc:'Balanced power and speed',     icon:'🔴'},
  {id:'sniper', name:'SNIPER',  price:300, desc:'One shot one kill, long range', icon:'🎯'},
];
const SHOP_UPS=[
  {id:'heal',   name:'FULL HEAL',   price:80,  desc:'Restore all HP instantly',     icon:'❤️'},
  {id:'ammo',   name:'MAX AMMO',    price:60,  desc:'Refill current gun ammo',      icon:'🎯'},
  {id:'speed',  name:'SPEED BOOST', price:150, desc:'Move 55% faster for 10s',     icon:'⚡'},
  {id:'damage', name:'DMG AMP',     price:200, desc:'Double bullet damage for 10s', icon:'🔥'},
  {id:'shield', name:'SHIELD +1',   price:175, desc:'Block one incoming hit',       icon:'🛡️'},
];
const ownedGuns=new Set(['pistol']);
let equippedGunId='pistol';

function openShop(){
  if(!localPlayer||!running)return;
  document.getElementById('shop-bal-val').textContent=coins;
  document.getElementById('shop-overlay').classList.add('open');
  renderShop();
}
function closeShop(){document.getElementById('shop-overlay').classList.remove('open');}

function renderShop(){
  document.getElementById('shop-bal-val').textContent=coins;
  // Guns
  const gg=document.getElementById('shop-guns'); gg.innerHTML='';
  SHOP_GUNS.forEach(item=>{
    const owned=ownedGuns.has(item.id);
    const eq=equippedGunId===item.id;
    const canBuy=coins>=item.price;
    const div=document.createElement('div');
    div.className='shop-item'+(eq?' si-eq':owned?' si-owned':'');
    div.innerHTML=`<div class="si-name">${item.icon} ${item.name}</div>`+
      `<div class="si-desc">${item.desc}</div>`+
      `<div class="si-price${(!owned&&!canBuy)?' broke':''}">${eq?'✓ EQUIPPED':owned?'TAP TO EQUIP':'🪙 '+item.price}</div>`;
    div.onclick=()=>{
      if(eq)return;
      if(owned){equippedGunId=item.id;setGun(item.id);renderShop();return;}
      if(!canBuy)return;
      coins-=item.price;updCoinsHud();
      ownedGuns.add(item.id);equippedGunId=item.id;setGun(item.id);renderShop();
    };
    gg.appendChild(div);
  });
  // Upgrades
  const ug=document.getElementById('shop-upgrades'); ug.innerHTML='';
  SHOP_UPS.forEach(item=>{
    const canBuy=coins>=item.price;
    const div=document.createElement('div');
    div.className='shop-item';
    div.innerHTML=`<div class="si-name">${item.icon} ${item.name}</div>`+
      `<div class="si-desc">${item.desc}</div>`+
      `<div class="si-price${!canBuy?' broke':''}">🪙 ${item.price}</div>`;
    div.onclick=()=>{
      if(!canBuy||!localPlayer)return;
      coins-=item.price;updCoinsHud();
      if(item.id==='heal'){localPlayer.hp=MAX_HP;updHpHud();}
      else if(item.id==='ammo'){localPlayer.ammo=localPlayer.gun.maxAmmo;localPlayer.reloading=false;updAmmoHud();}
      else if(item.id==='speed'){localPlayer.powSpeed=true;localPlayer.powSpeedTimer=10;updPowHud();}
      else if(item.id==='damage'){localPlayer.powDamage=true;localPlayer.powDamageTimer=10;updPowHud();}
      else if(item.id==='shield'){localPlayer.shield=Math.min(3,(localPlayer.shield||0)+1);updHpHud();}
      renderShop();
    };
    ug.appendChild(div);
  });
}

document.addEventListener('keydown',e=>{
  const k=e.key.toLowerCase();
  if(k==='b'&&running&&localPlayer&&!localPlayer.dead&&!paused)openShop();
  if(k==='q'&&running){
    if(paused)closePause();
    else{closeShop();openPause();}
  }
  if(e.key==='Escape'){closeShop();if(paused)closePause();}
});


// ══════════════════════════════════════════════════════
//  PAUSE / LEAVE MENU
// ══════════════════════════════════════════════════════
let paused=false;
function openPause(){
  if(!running)return;
  paused=true;
  document.getElementById('pause-overlay').classList.add('open');
}
function closePause(){
  paused=false;
  document.getElementById('pause-overlay').classList.remove('open');
  lastTs=performance.now(); // reset dt so game doesn't jump
}
function leaveGame(){
  paused=false;
  running=false;
  document.getElementById('pause-overlay').classList.remove('open');
  clearInterval(stateInterval);
  // Reset lobby state
  lobby_phase='lobby';
  wsSend({type:'play_again'});
  showScreen('s-lobby');
  // Reset game vars
  bullets=[];particles=[];botEntities=[];remotePlayers={};localPlayer=null;
  coins=0;updCoinsHud();
}

</script>
</body>
</html>

"""


def main():
    global HTML_CONTENT
    HTML_CONTENT = load_html()

    port = int(os.environ.get("PORT", find_port(7373)))
    lan_ip = get_lan_ip()
    local_url = f'http://localhost:{port}'
    lan_url   = f'http://{lan_ip}:{port}'

    server = ThreadedHTTPServer(('', port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    print()
    print('  ██████╗ ██╗██╗   ██╗ █████╗ ██╗      ███████╗')
    print('  ██╔══██╗██║██║   ██║██╔══██╗██║      ██╔════╝')
    print('  ██████╔╝██║██║   ██║███████║██║      ███████╗')
    print('  ██╔══██╗██║╚██╗ ██╔╝██╔══██║██║      ╚════██║')
    print('  ██║  ██║██║ ╚████╔╝ ██║  ██║███████╗ ███████║')
    print('  ╚═╝  ╚═╝╚═╝  ╚═══╝  ╚═╝  ╚═╝╚══════╝ ╚══════╝')
    print()
    print(f'  🎮  Your browser  : {local_url}')
    print(f'  🌐  Friends join  : {lan_url}')
    print()
    print('  ─── HOW TO PLAY WITH FRIENDS ──────────────────')
    print(f'  Share this with people on your WiFi:')
    print(f'  ➜  {lan_url}')
    print()
    print('  ─── GAME MODES ────────────────────────────────')
    print('  ⚔️  Team DM   — First team to 30 kills')
    print('  💀  Free FAll — Every man for himself')
    print('  👑  King Hill — Hold center zone 20s')
    print('  🚩  Capture   — Steal enemy flag × 3')
    print('  🔫  Gun Game  — Cycle all weapons first')
    print()
    print('  ─── MATCH SIZES ───────────────────────────────')
    print('  1v1 · 2v2 · 3v3 · 4v4  (bots fill empty slots)')
    print()
    print('  Press Ctrl+C to quit.')
    print()


    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print('\n  Thanks for playing RIVALS! 👋\n')
        server.shutdown()
        sys.exit(0)

if __name__ == '__main__':
    main()
