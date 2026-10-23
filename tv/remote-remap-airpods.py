#!/usr/bin/python
"""Remaps the LG IR remote. Runs as root on the TV (python 2).

Reads the remote ("LGE RCU" evdev node), re-emits every key through a virtual keyboard, and applies
/var/lib/webosbrew/remote-map.json on the way:
  {"version": 1,
   "global":    {"<button>": ACTION, ...},
   "retroarch": {"<button>": ACTION, ...}}          (applies while RetroArch is the foreground app)
ACTION: {"t":"key","code":N}                         send another key instead
        {"t":"app","id":"<appId>","params":{...}}    open an app (or a game: the game launcher + {"rom": ...})
        {"t":"none"}                                 swallow the button
A button not in the file (or "default") is passed through unchanged, so with no config the remote behaves as
stock. If anything fails the grab is released (process exit) and the remote goes back to normal.

  remote-remap.py [--seconds N]    (N: stop by itself after N seconds, for testing)
"""
from __future__ import print_function
import fcntl, glob, json, os, select, socket, struct, subprocess, sys, time

CONFIG = '/var/lib/webosbrew/remote-map.json'
LOG = '/tmp/remote-remap.log'
LOCK = '/tmp/remote-remap.lock'
EVIOCGRAB = 0x40044590
EV = struct.Struct('llHHi')
RETROARCH = ('com.retroarch.webos', 'org.personal.gamelauncher')   # games run under the game launcher app
DEBUG = '--debug' in sys.argv

# The codes this remote sends (measured; see ~/.lgtv/remote_map.json).
BUTTONS = {
    'num1': 2, 'num2': 3, 'num3': 4, 'num4': 5, 'num5': 6, 'num6': 7, 'num7': 8, 'num8': 9, 'num9': 10, 'num0': 11,
    'dash': 771, 'vol_up': 115, 'vol_down': 114, 'mute': 113, 'ch_up': 402, 'ch_down': 403,
    'home': 773, 'input': 241, 'settings': 139, 'guide': 362, 'back': 412,
    'up': 103, 'down': 108, 'left': 105, 'right': 106, 'ok': 28,
    'red': 398, 'green': 399, 'yellow': 400, 'blue': 401,
    'netflix': 1037, 'prime': 1038, 'disney': 983, 'hulu': 915,
}
NAME_OF = dict((v, k) for k, v in BUTTONS.items())


def log(message):
    try:
        with open(LOG, 'a') as f:
            f.write(time.strftime('%H:%M:%S') + ' ' + message + '\n')
    except IOError:
        pass


def find_remote():
    name = None
    for block in open('/proc/bus/input/devices').read().split('\n\n'):
        if 'Name="LGE RCU"' in block:
            for part in block.split():
                if part.startswith('event'):
                    return '/dev/input/' + part
    return None


class Config(object):
    def __init__(self):
        self.mtime = None
        self.data = {}

    def get(self):
        try:
            m = os.stat(CONFIG).st_mtime
        except OSError:
            self.data, self.mtime = {}, None
            return self.data
        if m != self.mtime:
            try:
                with open(CONFIG) as f:
                    self.data = json.load(f)
                log('config loaded: %d global, %d retroarch' % (len(self.data.get('global', {})), len(self.data.get('retroarch', {}))))
            except (IOError, ValueError) as e:
                log('config unreadable (%r): passing everything through' % (e,))
                self.data = {}
            self.mtime = m
        return self.data


class Foreground(object):
    """Foreground app via a luna subscription, read from the same select loop (this python has no threads)."""
    def __init__(self):
        self.app = None
        self.proc = None
        self.buf = b''
        self.retry_at = 0

    def fileno(self):
        return self.proc.stdout.fileno() if self.proc else None

    def ensure(self):
        if self.proc is None and time.time() >= self.retry_at:
            try:
                self.proc = subprocess.Popen(['luna-send', '-i', 'luna://com.webos.applicationManager/getForegroundAppInfo', '{"subscribe":true}'],
                                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True)
                self.buf = b''
            except Exception as e:
                log('foreground watcher: %r' % (e,))
                self.retry_at = time.time() + 10

    def read(self):
        data = os.read(self.proc.stdout.fileno(), 4096)
        if not data:
            self.proc.wait()
            self.proc = None
            self.retry_at = time.time() + 10
            return
        self.buf += data
        while b'\n' in self.buf:
            line, self.buf = self.buf.split(b'\n', 1)
            try:
                app = json.loads(line.decode('utf-8', 'replace')).get('appId')
            except ValueError:
                continue
            if app is not None:
                self.app = app


VIRTUAL_NAME = 'LGE RCU virtual'
FIFO = '/tmp/remap-out.fifo'
AIRPODS_ROUTE = '/tmp/airpods-bridge/route'


def airpods_key(name, send=True):
    """Forward a volume press to the active Mac relay; return its mode on success."""
    try:
        parts = open(AIRPODS_ROUTE).read().split()
        if len(parts) != 4 or parts[0] not in ('only', 'both'):
            return None
        mode, address, port, pid = parts
        socket.inet_aton(address)
        port, pid = int(port), int(pid)
        if not 1 <= port <= 65535 or pid <= 0:
            return None
        if b'capture' not in open('/proc/%d/cmdline' % pid, 'rb').read():
            return None
        if send:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.sendto(b'KEY ' + name.encode('ascii'), (address, port))
            finally:
                sock.close()
        return mode
    except (IOError, OSError, ValueError, OverflowError, socket.error):
        return None


def virtual_node():
    for block in open('/proc/bus/input/devices').read().split('\n\n'):
        if 'Name="%s"' % VIRTUAL_NAME in block:
            for part in block.split():
                if part.startswith('event'):
                    return '/dev/input/' + part
    return None


def compositor_has(node):
    """True when surface-manager already has the virtual keyboard open (it only looks at start-up)."""
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            if open('/proc/%s/comm' % pid).read().strip() != 'surface-manager':
                continue
            for fd in os.listdir('/proc/%s/fd' % pid):
                if os.readlink('/proc/%s/fd/%s' % (pid, fd)) == node:
                    return True
        except (IOError, OSError):
            continue
    return False


def emit(fd, code, value):
    os.write(fd, EV.pack(0, 0, 1, code, value) + EV.pack(0, 0, 0, 0, 0))


def launch(action):
    try:
        with open(os.devnull, 'w') as null:
            subprocess.Popen(['luna-send', '-n', '1', 'luna://com.webos.applicationManager/launch',
                              json.dumps({'id': action['id'], 'params': action.get('params') or {}})], stdout=null, stderr=null)
        log('launch %s %s' % (action['id'], action.get('params') or ''))
    except Exception as e:
        log('launch failed: %r' % (e,))


def main():
    lock = open(LOCK, 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print('already running')
        return
    deadline = None
    if '--seconds' in sys.argv:
        deadline = time.time() + int(sys.argv[sys.argv.index('--seconds') + 1])
    path = find_remote()
    if not path:
        log('remote device not found; nothing to do')
        return
    # Never take the remote unless the TV's UI is listening to our virtual keyboard: fail closed.
    warned = False
    while True:
        node = virtual_node()
        if node and os.path.exists(FIFO) and compositor_has(node):
            break
        if not warned:
            log('waiting: virtual keyboard %s is not open in surface-manager yet; the remote stays stock' % (node,))
            warned = True
        if deadline is not None and time.time() >= deadline:
            log('gave up (test window over); remote untouched')
            return
        time.sleep(5)
    out = os.open(FIFO, os.O_WRONLY)
    src = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    fcntl.ioctl(src, EVIOCGRAB, 1)
    log('started on %s%s' % (path, ' for %ds' % int(sys.argv[sys.argv.index('--seconds') + 1]) if deadline else ''))
    cfg, fg = Config(), Foreground()
    try:
        while deadline is None or time.time() < deadline:
            fg.ensure()
            watch = [src] + ([fg.fileno()] if fg.proc else [])
            ready, _, _ = select.select(watch, [], [], 1)
            if fg.proc and fg.fileno() in ready:
                fg.read()
            if src not in ready:
                continue
            data = os.read(src, EV.size * 64)
            for off in range(0, len(data) - EV.size + 1, EV.size):
                _, _, etype, code, value = EV.unpack_from(data, off)
                if DEBUG and etype == 1:
                    log('key %d v%d fg=%s' % (code, value, fg.app))
                if etype != 1:
                    continue                                  # sync/scancode events are regenerated
                action = None
                name = NAME_OF.get(code)
                if name in ('vol_up', 'vol_down', 'mute') and value in (1, 2):
                    airpods_mode = airpods_key(name)
                    if airpods_mode == 'only':
                        continue
                elif name in ('vol_up', 'vol_down', 'mute') and value == 0:
                    # Release events must also be swallowed while the TV is muted.
                    if airpods_key(name, send=False) == 'only':
                        continue
                if name:
                    conf = cfg.get()
                    if fg.app in RETROARCH:
                        action = conf.get('retroarch', {}).get(name)
                    if action is None:
                        action = conf.get('global', {}).get(name)
                try:
                    kind = action and action.get('t')
                    if kind == 'key':
                        emit(out, int(action['code']), value)
                    elif kind == 'app':
                        if value == 1:
                            launch(action)
                    elif kind == 'none':
                        pass
                    else:
                        emit(out, code, value)
                except Exception as e:
                    log('action error %r for %s; passing through' % (e, name))
                    emit(out, code, value)
    finally:
        try:
            fcntl.ioctl(src, EVIOCGRAB, 0)
        except IOError:
            pass
        log('stopped')


if __name__ == '__main__':
    main()
