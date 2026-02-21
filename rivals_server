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
                # default targets by mode
            if 'mode' in cfg:
                lobby['config']['mode'] = cfg['mode']
                targets = {'tdm':30,'ffa':8,'koth':20,'ctf':3,'gun':None,'1v1':10}
                lobby['config']['target'] = targets.get(cfg['mode'], 30)
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
            victim = lobby['players'].get(victim_id)
            if not victim: return
            p['kills'] += 1; p['streak'] += 1; p['score'] += 100
            victim['deaths'] += 1; victim['streak'] = 0; victim['score'] = max(0,victim['score']-25)
            if lobby['config']['mode'] == 'tdm':
                lobby['scores'][p['team']] = lobby['scores'].get(p['team'],0) + 1
            broadcast({'type':'kill','killer_id':pid,'victim_id':victim_id,
                       'killer_name':p['name'],'victim_name':victim['name'],
                       'killer_team':p['team'],'killer_kills':p['kills'],
                       'scores':lobby['scores'],
                       'streak': p['streak']})
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
    for name in ['rivals_game.html','rivals_game (4).html']:
        path = os.path.join(os.path.dirname(__file__), name)
        if os.path.exists(path): break
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()

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
