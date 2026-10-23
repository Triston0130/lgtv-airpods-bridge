#!/usr/bin/env python3
"""Lets the TV's Settings menu start and stop the bridge.

Listens on the Mac's LAN address only, answers only the TV's address, and requires a per-install token that it
writes to the TV over SSH. It understands three requests and nothing else:
    GET  /status
    POST /start?mode=only   AirPods only (the TV is muted while streaming)
    POST /start?mode=both   TV and AirPods together
    POST /stop
Run by the LaunchAgent that `agent.py install` writes; `agent.py uninstall` removes it.
"""
import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import relay  # noqa: E402  (shares the TV connection settings and AirPods check)

tv = relay.tv
PORT = 47801
TOKEN_FILE = ROOT/'agent-token'
# The listener's last explicit choice from the TV: "only", "both", or null after choosing a TV output.
CHOICE_FILE = ROOT/'agent-choice.json'
TV_DIR = '/var/lib/webosbrew/airpods-bridge'
LABEL = 'com.lgtv-airpods-bridge.agent'
PLIST = Path.home()/'Library/LaunchAgents'/(LABEL+'.plist')
lock = threading.Lock()
airpods_cache = (0.0, None)


def token():
    if not TOKEN_FILE.exists():
        TOKEN_FILE.write_text(secrets.token_urlsafe(24))
        TOKEN_FILE.chmod(0o600)
    return TOKEN_FILE.read_text().strip()


def lan_address():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((tv.HOST, 9))
        return probe.getsockname()[0]
    finally:
        probe.close()


def publish_endpoint(address):
    """Tell the TV where to reach this agent (the Mac's address can change with DHCP)."""
    endpoint = json.dumps({'url': 'http://%s:%d' % (address, PORT), 'token': token()})
    command = 'mkdir -p %s && umask 077 && cat > %s/endpoint.json' % (TV_DIR, TV_DIR)
    result = subprocess.run(tv.SSH + [command], input=endpoint.encode(), capture_output=True, timeout=30)
    return result.returncode == 0


def airpods_name():
    """Name of the AirPods selected as the Mac's output, or None. Cached briefly: system_profiler is slow."""
    global airpods_cache
    checked, name = airpods_cache
    if time.monotonic()-checked < 5:
        return name
    try:
        name = relay.require_airpods()
    except Exception:
        name = None
    airpods_cache = (time.monotonic(), name)
    return name


def status():
    state = json.loads(relay.STATE.read_text()) if relay.STATE.exists() else {}
    running = relay.active_pid() is not None
    # A failure is news only briefly; an old one (say, from before the TV was switched off) would read as broken.
    recent = relay.STATE.exists() and time.time()-relay.STATE.stat().st_mtime < 120
    return {
        'running': running,
        'mode': state.get('mode') if running else None,
        'state': state.get('status'),
        'output': state.get('output') if running else None,
        'error': state.get('error') if state.get('status') == 'failed' and recent else None,
        'airpods': airpods_name(),
    }


def remembered_mode():
    try:
        return json.loads(CHOICE_FILE.read_text()).get('mode')
    except (OSError, ValueError):
        return None


def remember(mode):
    CHOICE_FILE.write_text(json.dumps({'mode': mode, 'chosen': time.strftime('%F %T')}))


def auto_connect():
    """Start the remembered mode when the AirPods connect to the Mac, like Bluetooth headphones reconnecting.
    Only when the last choice on the TV was an AirPods mode; choosing a TV output turns this off."""
    was_present = False
    last_attempt = 0.0
    while True:
        time.sleep(3)
        mode = remembered_mode()
        present = bool(airpods_name())
        appeared = present and not was_present
        was_present = present
        if not (mode and present) or relay.active_pid():
            continue
        if not appeared and time.monotonic()-last_attempt < 60:
            continue
        last_attempt = time.monotonic()
        with lock:
            if relay.active_pid():
                continue
            result = start(mode)
        sys.stderr.write('%s auto-connect (%s): %s\n' % (time.strftime('%F %T'), mode, 'playing' if result.get('running') else result.get('error') or result.get('state')))


def control(*arguments):
    return subprocess.run([sys.executable, str(ROOT/'relay.py')] + list(arguments), capture_output=True, text=True, timeout=30)


def start(mode):
    if relay.active_pid():
        current = json.loads(relay.STATE.read_text()).get('mode') if relay.STATE.exists() else None
        if current == mode:
            return status()
        control('stop')
    if not airpods_name():
        return dict(status(), error='Connect your AirPods to the Mac and select them as the sound output.')
    relay.STATE.unlink(missing_ok=True)
    control('start', *(['--mute-tv'] if mode == 'only' else []))
    deadline = time.monotonic()+12
    while time.monotonic() < deadline:
        state = json.loads(relay.STATE.read_text()) if relay.STATE.exists() else {}
        if state.get('status') in ('playing', 'failed'):
            break
        time.sleep(0.3)
    return status()


class Handler(BaseHTTPRequestHandler):
    server_version = 'airpods-bridge'

    def log_message(self, form, *args):
        sys.stderr.write('%s %s\n' % (time.strftime('%F %T'), form % args))

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        return self.client_address[0] == tv.HOST and hmac.compare_digest(self.headers.get('X-Token', ''), token())

    def handle_request(self, method):
        if not self.authorized():
            return self.reply(403, {'error': 'forbidden'})
        url = urlparse(self.path)
        with lock:
            if method == 'GET' and url.path == '/status':
                return self.reply(200, status())
            if method == 'POST' and url.path == '/start':
                mode = parse_qs(url.query).get('mode', ['both'])[0]
                if mode not in ('only', 'both'):
                    return self.reply(400, {'error': 'mode must be only or both'})
                remember(mode)
                return self.reply(200, start(mode))
            if method == 'POST' and url.path == '/stop':
                remember(None)
                control('stop')
                return self.reply(200, status())
        self.reply(404, {'error': 'not found'})

    def do_GET(self):
        self.handle_request('GET')

    def do_POST(self):
        self.handle_request('POST')


def serve():
    relay.interactive_qos()
    threading.Thread(target=auto_connect, daemon=True).start()
    address = None
    server = None
    while True:
        # Rebind when the Mac's LAN address changes, and re-publish it to the TV.
        current = lan_address()
        if current != address:
            if server:
                server.shutdown(); server.server_close()
            server = ThreadingHTTPServer((current, PORT), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            address = current
            published = False
        if not published:
            published = publish_endpoint(address)
            sys.stderr.write('%s listening on %s:%d, endpoint published to TV: %s\n' % (time.strftime('%F %T'), address, PORT, published))
        time.sleep(30)


def install():
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text('''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key><array><string>%s</string><string>%s</string><string>serve</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardErrorPath</key><string>%s</string>
  <key>StandardOutPath</key><string>%s</string>
</dict></plist>
''' % (LABEL, sys.executable, ROOT/'agent.py', ROOT/'agent.log', ROOT/'agent.log'))
    domain = 'gui/%d' % os.getuid()
    subprocess.run(['launchctl', 'bootout', domain, str(PLIST)], capture_output=True)
    subprocess.run(['launchctl', 'bootstrap', domain, str(PLIST)], check=True)
    print('Installed and started', LABEL)


def uninstall():
    subprocess.run(['launchctl', 'bootout', 'gui/%d' % os.getuid(), str(PLIST)], capture_output=True)
    PLIST.unlink(missing_ok=True)
    print('Removed', LABEL)


if __name__ == '__main__':
    command = sys.argv[1] if len(sys.argv) > 1 else 'serve'
    {'serve': serve, 'install': install, 'uninstall': uninstall}[command]()
