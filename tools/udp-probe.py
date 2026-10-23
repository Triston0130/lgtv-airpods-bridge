"""Stream a timestamped UDP copy of TV capture channel 0 and split delay into TV-side and network-side.

usage: udp-probe.py SECONDS TOS PORT [MAC_SERVICE_TYPE]    e.g. udp-probe.py 40 0x00 47811
Run it only while the relay is stopped: never run two capture copies at once.
"""
import socket, subprocess, sys, time, struct
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO)); import tvssh as tv
seconds, tos, port = float(sys.argv[1]), sys.argv[2], int(sys.argv[3])
service = int(sys.argv[4]) if len(sys.argv) > 4 else -1  # macOS SO_NET_SERVICE_TYPE: 0 BE, 3 VI, 4 VO, 5 RV
binary = '/tmp/airpods-bridge/probe-capture'
up = subprocess.run(tv.SSH + ['cat > %s && chmod 700 %s' % (binary, binary)], input=(REPO/'capture').read_bytes(), capture_output=True, timeout=30)
assert up.returncode == 0, up.stderr
probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); probe.connect((tv.HOST, 9)); me = probe.getsockname()[0]; probe.close()
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20); s.bind((me, port)); s.settimeout(1)
if service >= 0: s.setsockopt(socket.SOL_SOCKET, 0x1116, service)
ssh = subprocess.Popen(tv.SSH + ['exec %s --stream 0 8 0 %s %d %s' % (binary, me, port, tos)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
rows = {}; end = None
while True:
    try: data, sender = s.recvfrom(4096)
    except socket.timeout:
        if end is not None and time.monotonic() > end: break
        continue
    now = time.monotonic()
    if end is None: end = now + seconds
    if now > end: break
    if sender[0] == tv.HOST and len(data) == 8 + 1920:
        q, sent = struct.unpack_from('<II', data)
        rows.setdefault(q, (sent / 1e6, now))
ssh.terminate(); ssh.wait()
seqs = sorted(rows)
# unwrap the 32-bit microsecond clock
sent = {}; prev = None; wrap = 0
for q in seqs:
    t = rows[q][0]
    if prev is not None and t + wrap < prev - 2000: wrap += 2 ** 32 / 1e6
    sent[q] = t + wrap; prev = sent[q]
def fit(values):
    n = len(seqs); mq = sum(seqs) / n; mt = sum(values[q] for q in seqs) / n
    return sum((q - mq) * (values[q] - mt) for q in seqs) / sum((q - mq) ** 2 for q in seqs)
period = fit(sent)
tv_lag = {q: sent[q] - q * period for q in seqs}; b = min(tv_lag.values()); tv_lag = {q: (v - b) * 1000 for q, v in tv_lag.items()}
net = {q: rows[q][1] - sent[q] for q in seqs}; b = min(net.values()); net = {q: (v - b) * 1000 for q, v in net.items()}
def summary(name, d):
    v = sorted(d.values()); p = lambda x: v[min(len(v) - 1, int(x * len(v)))]
    print('%-8s p50=%6.1f p90=%6.1f p99=%6.1f p99.9=%6.1f max=%6.1f ms' % (name, p(.5), p(.9), p(.99), p(.999), v[-1]))
missing = [q for q in range(seqs[0], seqs[-1] + 1) if q not in rows]
print('tos=%s period=%.3fms packets=%d lost=%d' % (tos, period * 1000, seqs[-1] - seqs[0] + 1, len(missing)))
summary('tv-side', tv_lag); summary('network', net)
total = {q: tv_lag[q] + net[q] for q in seqs}; summary('total', total)
print('per-second max: tv/net ms')
line = []
for i in range(0, len(seqs), 100):
    chunk = seqs[i:i + 100]
    line.append('%d/%d' % (max(tv_lag[q] for q in chunk), max(net[q] for q in chunk)))
print(' '.join(line))
