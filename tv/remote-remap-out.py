#!/usr/bin/python
"""Keeper for the virtual keyboard that remote-remap.py sends keys through.

LG's surface-manager opens its input devices once, at start-up, and never notices new ones. So this tiny process
creates the device once (it must exist BEFORE surface-manager starts or is restarted) and then just forwards raw
input events written to /tmp/remap-out.fifo. The remap logic lives in remote-remap.py and can be restarted freely.
It does nothing else, and never touches the real remote.
"""
from __future__ import print_function
import fcntl, os, struct, sys, time
FIFO = '/tmp/remap-out.fifo'
LOCK = '/tmp/remap-out.lock'
NAME = 'LGE RCU virtual'
EV = struct.Struct('llHHi')

lock = open(LOCK, 'w')
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except IOError:
    sys.exit(0)
fd = os.open('/dev/uinput', os.O_WRONLY)
fcntl.ioctl(fd, 0x40045564, 0)
fcntl.ioctl(fd, 0x40045564, 1)
declared = 0
for code in range(0, 1300):
    try:
        fcntl.ioctl(fd, 0x40045565, code)
        declared += 1
    except IOError:
        pass
os.write(fd, struct.pack('80sHHHHi' + 'i' * 256, NAME.encode(), 3, 1, 1, 1, 0, *([0] * 256)))
fcntl.ioctl(fd, 0x5501)
if os.path.exists(FIFO):
    os.unlink(FIFO)
os.mkfifo(FIFO, 0o666)
os.chmod(FIFO, 0o666)
fifo = os.open(FIFO, os.O_RDWR)           # read+write so it never sees EOF
with open('/tmp/remap-out.log', 'a') as f:
    f.write('%s keeper up, %d key codes declared\n' % (time.strftime('%H:%M:%S'), declared))
buf = b''
while True:
    buf += os.read(fifo, 4096)
    while len(buf) >= EV.size:
        os.write(fd, buf[:EV.size])
        buf = buf[EV.size:]
