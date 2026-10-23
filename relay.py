#!/usr/bin/env python3
"""Stream an LG TV's audio to AirPods through a Mac: native DSP capture on the TV, UDP with selective resend, AudioQueue playback."""
import argparse
import array
import json
import os
from pathlib import Path
import signal
import random
import socket
import struct
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import tvssh as tv

PID = ROOT/'relay.pid'
STATE = ROOT/'relay-state.json'
stopping = False

def update(**values):
    temporary = ROOT/'relay-state.tmp'
    temporary.write_text(json.dumps(values, indent=2)+'\n')
    temporary.replace(STATE)

def request_stop(*args):
    global stopping
    stopping = True

def active_pid():
    try:
        value = int(PID.read_text())
        os.kill(value, 0)
        result = subprocess.run(['ps', '-p', str(value), '-o', 'command='], capture_output=True, text=True, timeout=3)
        arguments = shlex.split(result.stdout.strip())
        if str(Path(__file__).resolve()) not in arguments or '_run' not in arguments:
            return None
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None

def require_airpods():
    if os.environ.get('AIRPODS_BRIDGE_TEST_OUTPUT') == '1':
        return 'test output (any device)'
    result = subprocess.run(['system_profiler', 'SPAudioDataType', '-json'], capture_output=True, text=True, check=True, timeout=15)
    data = json.loads(result.stdout)
    for group in data.get('SPAudioDataType', []):
        for item in group.get('_items', []):
            if item.get('coreaudio_default_audio_output_device') == 'spaudio_yes':
                name = item.get('_name', '')
                if 'airpod' not in name.lower():
                    raise RuntimeError('Select your AirPods as the Mac sound output first. Current output: '+name)
                return name
    raise RuntimeError('No accessible default audio output; run this from Terminal with audio access.')

UDP_PORT = 47800
CHUNK = 1920

class Recovery:
    """In-order delivery of sequenced UDP chunks; asks the TV to resend gaps, conceals only past the deadline."""
    HISTORY = 128  # must match the TV's resend history

    def __init__(self, sock, deadline_s):
        self.sock, self.deadline = sock, deadline_s
        self.next = None
        self.highest = None
        self.pending = {}
        self.missing = {}  # seq -> [first noticed, last NACK sent]
        self.previous = bytes(CHUNK)
        self.concealed_run = 0
        self.stats = dict(recovered=0, concealed=0, nacks=0, duplicates=0, max_loop_gap_ms=0, max_late_ms=0, max_read_delay_ms=0)
        self.last_late_log = 0.0
        self.offset = None  # smallest (arrival - TV send time) seen: the no-queueing path
        self.sock_peer = None

    def receive(self, sequence, payload, now, sent_us=None, arrived=None):
        if sent_us is not None:
            # TV clock is 32-bit microseconds; only differences matter. Kernel arrival time keeps relay pauses out of it.
            offset = (arrived if arrived is not None else now) - sent_us/1e6
            if arrived is not None:
                self.stats['max_read_delay_ms'] = max(self.stats['max_read_delay_ms'], round((time.time()-arrived)*1000, 1))
            if self.offset is None or offset < self.offset or offset - self.offset > 4000:
                self.offset = offset
            late_ms = (offset - self.offset)*1000
            self.stats['max_late_ms'] = max(self.stats['max_late_ms'], round(late_ms, 1))
            if late_ms > 40 and time.time()-self.last_late_log > 0.5:
                # Wall-clock record of each late burst, for matching against macOS Wi-Fi logs.
                with (ROOT/'late.log').open('a') as log:
                    log.write('%.3f %.0f\n' % (time.time(), late_ms))
                self.last_late_log = time.time()
        if self.next is None:
            self.next = self.highest = sequence
        if sequence < self.next or sequence in self.pending:
            self.stats['duplicates'] += 1
            return
        if sequence-self.next > self.HISTORY:
            # Too far ahead to recover; resynchronise at the new position.
            self.pending.clear(); self.missing.clear(); self.next = self.highest = sequence
        if sequence in self.missing:
            del self.missing[sequence]
            self.stats['recovered'] += 1
        self.pending[sequence] = payload
        if sequence > self.highest:
            fresh = list(range(self.highest+1, sequence))
            for q in fresh:
                self.missing[q] = [now, now]
            self.highest = sequence
            self._nack(fresh)

    def _nack(self, sequences):
        for i in range(0, len(sequences), 64):
            batch = sequences[i:i+64]
            self.sock.send(b'NACK'+b''.join(q.to_bytes(4, 'little') for q in batch))
            self.stats['nacks'] += 1

    def service(self, now):
        """Return audio ready for the player and re-request gaps that are still open."""
        out = []
        while self.next is not None and self.next <= self.highest:
            if self.next in self.pending:
                self.previous = self.pending.pop(self.next)
                out.append(self.previous); self.concealed_run = 0
            elif now-self.missing.get(self.next, [now])[0] >= self.deadline:
                self.missing.pop(self.next, None)
                self.stats['concealed'] += 1; self.concealed_run += 1
                # Repeat the last chunk for a short gap, silence for a long one.
                out.append(self.previous if self.concealed_run <= 3 else bytes(CHUNK))
            else:
                break
            self.next += 1
        again = [q for q, times in self.missing.items() if now-times[1] >= 0.02]
        for q in again:
            self.missing[q][1] = now
        if again:
            self._nack(again)
        return b''.join(out)

class LinkHealth:
    """Pings the TV over the stream socket and refreshes its Wi-Fi association when the downlink degrades."""
    WINDOW_S, MEDIAN_LIMIT_S, LOSS_LIMIT, COOLDOWN_S = 5.0, 0.08, 0.33, 60.0

    def __init__(self, sock):
        self.sock = sock
        self.sent = {}       # ping id -> send time
        self.samples = []    # (send time, rtt or None)
        self.next_id = 0
        self.last_ping = 0.0
        self.last_reassoc = 0.0
        self.stats = dict(rtt_p50_ms=None, rtt_p90_ms=None, ping_loss=0.0, reassociations=0)

    def pong(self, packet, now):
        ping_id = int.from_bytes(packet[4:8], 'little')
        sent = self.sent.pop(ping_id, None)
        if sent is not None:
            self.samples.append((sent, now-sent))

    def service(self, now):
        if now-self.last_ping >= 0.1:
            self.sock.send(b'PING'+self.next_id.to_bytes(4, 'little'))
            self.sent[self.next_id] = now
            self.next_id += 1
            self.last_ping = now
        for ping_id, sent in list(self.sent.items()):
            if now-sent > 1.0:
                del self.sent[ping_id]
                self.samples.append((sent, None))
        self.samples = [s for s in self.samples if now-s[0] <= self.WINDOW_S]
        if len(self.samples) < 30:
            return
        rtts = sorted(r for _, r in self.samples if r is not None)
        loss = 1-len(rtts)/len(self.samples)
        median = rtts[len(rtts)//2] if rtts else 10.0
        self.stats.update(rtt_p50_ms=round(median*1000, 1), rtt_p90_ms=round(rtts[int(len(rtts)*0.9)]*1000, 1) if rtts else None, ping_loss=round(loss, 2))
        if (median > self.MEDIAN_LIMIT_S or loss > self.LOSS_LIMIT) and now-self.last_reassoc > self.COOLDOWN_S:
            for _ in range(3):
                self.sock.send(b'RASC')
            self.last_reassoc = now
            self.stats['reassociations'] += 1
            with (ROOT/'link.log').open('a') as log:
                log.write('%s reassociate: median %.0f ms, loss %.0f%%\n' % (time.strftime('%F %T'), median*1000, loss*100))
            self.samples.clear()

def local_address():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.connect((tv.HOST, 9))
    address = probe.getsockname()[0]
    probe.close()
    return address

def game_mode(policy):
    """Set macOS Game Mode ('on' or 'auto'); Game Mode raises the Bluetooth polling rate, lowering AirPods latency.
    Needs Xcode's gamepolicyctl; without it the bridge runs unchanged."""
    try:
        tool = subprocess.run(['xcrun', '--find', 'gamepolicyctl'], capture_output=True, text=True, timeout=10).stdout.strip()
        if tool:
            subprocess.run([tool, 'game-mode', 'set', policy], capture_output=True, timeout=10)
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    return False

def tv_audio(method, payload=None):
    """Call the TV's audio service (com.webos.audio) and return its reply, or None if it gave none."""
    command = "luna-send -t 1 -n 1 -w 3000 luna://com.webos.audio/%s '%s' 2>&1" % (method, json.dumps(payload or {}))
    try:
        output = subprocess.run(tv.SSH + [command], capture_output=True, text=True, timeout=15).stdout
        return json.loads(output.split('payload ', 1)[1].splitlines()[0])
    except (IndexError, ValueError, OSError, subprocess.SubprocessError):
        return None

def interactive_qos():
    """Ask macOS to schedule this thread as user-interactive, so 5 ms receive wakeups are not coalesced."""
    import ctypes
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    QOS_CLASS_USER_INTERACTIVE = 0x21
    return libc.pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0)

def run(volume, buffer_ms, transport, recover_ms=50, use_game_mode=True, mute_tv=False):
    interactive_qos()
    if use_game_mode:
        game_mode('on')
    global stopping
    output_name = require_airpods()
    # Capture taps the audio before the TV's own volume and mute, so the TV can be silenced while the AirPods play.
    restore_tv_mute = None
    if mute_tv:
        status = tv_audio('getVolume')
        if status is None or not tv_audio('setMuted', {'muted': True}):
            raise RuntimeError('Could not mute the TV through com.webos.audio')
        restore_tv_mute = bool(status.get('muted'))
    PID.write_text(str(os.getpid())+'\n')
    ssh = player = receiver = recovery = None
    byte_count = peak = window_peak = 0
    started = time.monotonic()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, request_stop)
    try:
        with (ROOT/'tv-capture.log').open('wb') as capture_log, (ROOT/'player.log').open('wb') as player_log:
            # This binary lives in TV RAM and is restored on each explicit start.
            deployment = subprocess.run(tv.SSH + ['mkdir -p /tmp/airpods-bridge && cat > /tmp/airpods-bridge/capture && chmod 700 /tmp/airpods-bridge/capture'], input=(ROOT/'capture').read_bytes(), capture_output=True, timeout=20)
            if deployment.returncode:
                raise RuntimeError('TV helper upload failed: '+deployment.stderr.decode(errors='replace')[-1000:])
            command = 'exec /tmp/airpods-bridge/capture --stream 1 8'
            if transport == 'udp':
                # SSH still owns the capture's lifetime; PCM rides UDP, accepted only from the TV.
                address = local_address()
                receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 18)
                receiver.bind((address, UDP_PORT))
                service = int(os.environ.get('AIRPODS_BRIDGE_SERVICE', '-1'))
                receiver.setsockopt(socket.SOL_SOCKET, 0x0400, 1)  # SO_TIMESTAMP: kernel arrival time per datagram
                if service >= 0:
                    receiver.setsockopt(socket.SOL_SOCKET, 0x1116, service)  # SO_NET_SERVICE_TYPE: tell macOS this is real-time
                command += ' 0 %s %d %s' % (address, UDP_PORT, os.environ.get('AIRPODS_BRIDGE_TOS', '0x00'))
            ssh = subprocess.Popen(tv.SSH[:-1] + ['-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2', tv.SSH[-1], command], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=capture_log, bufsize=0)
            player = subprocess.Popen([str(ROOT/'player'), str(volume), str(buffer_ms)] + (['any'] if os.environ.get('AIRPODS_BRIDGE_TEST_OUTPUT') == '1' else []), stdin=subprocess.PIPE, stdout=player_log, stderr=player_log, bufsize=0)
            update(status='starting', pid=os.getpid(), output=output_name, bytes=0)
            last_update = 0
            import selectors
            selector = selectors.DefaultSelector()
            selector.register(receiver if receiver else ssh.stdout, selectors.EVENT_READ)
            recovery = Recovery(receiver, recover_ms/1000) if receiver else None
            health = None
            last_loop = None
            test_drop = float(os.environ.get('AIRPODS_BRIDGE_TEST_DROP', '0'))
            while not stopping:
                if player.poll() is not None:
                    raise RuntimeError('Audio player exited; see player.log')
                if ssh.poll() is not None:
                    raise RuntimeError('TV capture exited; see tv-capture.log')
                events = selector.select(0.005 if recovery else 0.25)
                if not events and not recovery:
                    if not byte_count and time.monotonic()-started > 10:
                        raise RuntimeError('TV capture produced no PCM within 10 seconds; see tv-capture.log')
                    continue
                if recovery:
                    now = time.monotonic()
                    if last_loop is not None:
                        recovery.stats['max_loop_gap_ms'] = max(recovery.stats['max_loop_gap_ms'], round((now-last_loop)*1000, 1))
                    last_loop = now
                    while True:
                        try:
                            packet, ancillary, _, sender = receiver.recvmsg(8+CHUNK, 64, socket.MSG_DONTWAIT)
                        except BlockingIOError:
                            break
                        arrived = None
                        for level, kind, data in ancillary:
                            if level == socket.SOL_SOCKET and kind == 0x2 and len(data) >= 16:  # SCM_TIMESTAMP timeval
                                seconds, micros = struct.unpack_from('<qi', data)
                                arrived = seconds + micros/1e6
                        if test_drop and random.random() < test_drop:
                            continue  # test switch: simulate Wi-Fi loss to exercise recovery
                        if sender[0] == tv.HOST and packet[:4] == b'PONG' and health:
                            health.pong(packet, now)
                            continue
                        if sender[0] == tv.HOST and len(packet) == 8+CHUNK:
                            if recovery.sock_peer is None:
                                recovery.sock_peer = sender
                                receiver.connect(sender)
                                health = LinkHealth(receiver)
                            recovery.receive(int.from_bytes(packet[:4], 'little'), packet[8:], now, int.from_bytes(packet[4:8], 'little'), arrived)
                    if health:
                        health.service(now)
                    chunk = recovery.service(now)
                    if not chunk:
                        if not byte_count and now-started > 10:
                            raise RuntimeError('TV capture produced no PCM within 10 seconds; see tv-capture.log')
                        continue
                else:
                    chunk = os.read(ssh.stdout.fileno(), 1920)
                    if not chunk:
                        raise RuntimeError('TV capture stream ended; see tv-capture.log')
                samples = array.array('h', chunk[:len(chunk)//2*2])
                chunk_peak = max(map(abs, samples), default=0)
                peak = max(peak, chunk_peak)
                window_peak = max(window_peak, chunk_peak)
                remaining = memoryview(chunk)
                while remaining and not stopping:
                    written = player.stdin.write(remaining)
                    if not written:
                        raise RuntimeError('Audio player stopped accepting PCM')
                    remaining = remaining[written:]
                byte_count += len(chunk)
                now = time.monotonic()
                if now-last_update > 1:
                    update(status='playing', pid=os.getpid(), output=output_name, bytes=byte_count, audio_seconds=round(byte_count/192000, 3), elapsed_seconds=round(now-started, 3), peak=peak, peak_last_second=window_peak, volume=volume, transport=transport, **(recovery.stats if recovery else {}), **(health.stats if recovery and health else {}))
                    last_update = now
                    window_peak = 0
            update(status='stopping', bytes=byte_count, output=output_name)
    except Exception as exc:
        update(status='failed', error=str(exc), bytes=byte_count, output=output_name)
        raise
    finally:
        if restore_tv_mute is not None:
            tv_audio('setMuted', {'muted': restore_tv_mute})
        if use_game_mode:
            game_mode('auto')
        if receiver is not None:
            receiver.close()
        for process in (player, ssh):
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        PID.unlink(missing_ok=True)
        if stopping:
            update(status='stopped', bytes=byte_count, output=output_name)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['start', 'stop', 'status', '_run'])
    parser.add_argument('--volume', type=int, default=35)  # 0 is allowed; useful for silent tests
    parser.add_argument('--transport', choices=['udp', 'ssh'], default='udp', help='udp: sequenced datagrams with selective resend (default); ssh: PCM inside the SSH channel, which stalls up to ~300 ms behind TCP')
    parser.add_argument('--no-game-mode', action='store_true', help='leave macOS Game Mode alone (by default it is on while streaming)')
    parser.add_argument('--mute-tv', action='store_true', help="mute the TV's own speakers or sound output while streaming; its previous mute state returns on stop (default: both play)")
    parser.add_argument('--recover-ms', type=int, default=50, help='udp: how long a missing packet may be re-requested before it is concealed')
    parser.add_argument('--buffer-ms', type=int, default=70, help='jitter buffer target, 20-200 ms; lower is closer to lip sync but drops out on Wi-Fi stalls')
    args = parser.parse_args()
    if not 0 <= args.volume <= 100:
        parser.error('volume must be between 0 and 100')
    if not 20 <= args.buffer_ms <= 200:
        parser.error('buffer-ms must be between 20 and 200')
    if args.command == 'start':
        if active_pid():
            print('Relay already running.'); return
        output = require_airpods()
        with (ROOT/'relay.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '_run', '--volume', str(args.volume), '--buffer-ms', str(args.buffer_ms), '--transport', args.transport, '--recover-ms', str(args.recover_ms)] + (['--no-game-mode'] if args.no_game_mode else []) + (['--mute-tv'] if args.mute_tv else []), stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        print('Starting TV audio to '+output+' (relay PID '+str(process.pid)+').')
    elif args.command == 'stop':
        pid = active_pid()
        if pid:
            os.kill(pid, signal.SIGTERM)
            for _ in range(60):
                if not PID.exists(): break
                time.sleep(0.1)
        print(STATE.read_text() if STATE.exists() else 'Relay is stopped.')
    elif args.command == 'status':
        print(STATE.read_text() if STATE.exists() else 'Relay has not run.')
    else:
        run(args.volume, args.buffer_ms, args.transport, args.recover_ms, not args.no_game_mode, args.mute_tv)

if __name__ == '__main__':
    main()
