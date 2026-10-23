#!/usr/bin/env python3
"""Install the AirPods volume routing in this TV's existing remote remapper."""
import hashlib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import tvssh as tv

REMOTE = '/var/lib/webosbrew/remote-remap.py'
ORIGINAL_SHA256 = '08422f63f3c8e9a0bc3c3bdb6c74c9b6b4184a9bacf41ff0224980921004a3a8'


def ssh(command, data=None):
    result = subprocess.run(tv.SSH + [command], input=data, capture_output=True, timeout=45)
    if result.returncode:
        raise SystemExit(result.stderr.decode(errors='replace')[-1000:])
    return result.stdout.decode(errors='replace').strip()


def main():
    data = (HERE/'remote-remap-airpods.py').read_bytes()
    installed = hashlib.sha256(data).hexdigest()
    current = ssh('sha256sum %s' % REMOTE).split()[0]
    if current == installed:
        print('AirPods remote routing already installed.')
        return
    if current != ORIGINAL_SHA256:
        raise SystemExit('Remote remapper differs from the tested version; review changes before installing.')
    ssh('cp %s %s.before-airpods && cat > %s.candidate && /usr/bin/python -c \'compile(open("%s.candidate").read(), "candidate", "exec")\' && mv %s.candidate %s && chmod 755 %s' %
        (REMOTE, REMOTE, REMOTE, REMOTE, REMOTE, REMOTE, REMOTE), data)
    pid = ssh('ps -eo pid,args | awk \'$2=="/usr/bin/python" && $3=="%s" {print $1}\'' % REMOTE)
    if pid.isdigit():
        ssh('kill %s; sleep 1; sh /var/lib/webosbrew/init.d/62-remote-remap' % pid)
    print('Installed AirPods volume routing. Original saved as %s.before-airpods.' % REMOTE)


if __name__ == '__main__':
    main()
