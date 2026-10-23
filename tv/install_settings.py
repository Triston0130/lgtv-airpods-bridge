#!/usr/bin/env python3
"""Install the AirPods choices into the TV's Settings > Sound > Sound Out and the Quick Settings Sound Out pills.

Copies the stock Settings app to /var/lib/webosbrew/airpods-bridge/settings/app on the TV, patches that copy, and
bind-mounts it over the stock app (now, and at boot through a startup hook). Re-running it reinstalls cleanly.

Why a whole copy: the app manager caches appinfo.json at boot and starts Settings from a V8 snapshot of the stock
code. The snapshot skips main.js, so every localized index page's loader is changed to always run main.js, which
then renders itself.

usage: install_settings.py [--check]   (--check only verifies that the patch applies; nothing changes on the TV)
"""
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import relay  # noqa: E402  (shares the TV connection settings)

tv = relay.tv
APP = '/usr/palm/applications/com.palm.app.settings'
DIR = '/var/lib/webosbrew/airpods-bridge'
QS = '/usr/lib/qt5/qml/QuickSettings'
QS_FILES = ('SoundOutServiceModel.qml', 'QuickSettings.qml')
LOADER_OLD = 'if("undefined"==typeof App){'
LOADER_NEW = 'if(!0){'


def ssh(command, data=None, timeout=120):
    result = subprocess.run(tv.SSH + [command], input=data, capture_output=True, timeout=timeout)
    if result.returncode:
        raise SystemExit('TV command failed (%s): %s' % (command[:70], result.stderr.decode(errors='replace')[-300:]))
    return result.stdout.decode(errors='replace')


def fetch(path):
    return subprocess.run(tv.SSH + ['cat '+path], capture_output=True, timeout=60, check=True).stdout


def main():
    check = '--check' in sys.argv
    mounts = ssh('cat /proc/mounts')
    mounted = any((' %s ' % target) in mounts for target in (APP, APP+'/main.js', QS))
    if mounted and check:
        raise SystemExit('The menus are patched right now; --check needs the stock files.')
    if mounted:
        ssh('sh %s/uninstall-settings.sh' % DIR)
    with tempfile.TemporaryDirectory() as work:
        work = Path(work)
        original, patched = work/'original', work/'patched'
        qs_original, qs_patched = work/'qs-original', work/'qs-patched'
        original.mkdir(); qs_original.mkdir()
        for name in ('main.js', 'appinfo.json'):
            (original/name).write_bytes(fetch('%s/%s' % (APP, name)))
        for name in QS_FILES:
            (qs_original/name).write_bytes(fetch('%s/%s' % (QS, name)))
        subprocess.run([sys.executable, str(HERE/'patch_settings.py'), str(original), str(patched)], check=True)
        subprocess.run([sys.executable, str(HERE/'patch_quicksettings.py'), str(qs_original), str(qs_patched)], check=True)
        app_digest = hashlib.sha256((original/'main.js').read_bytes()).hexdigest()
        qs_digest = hashlib.sha256((qs_original/'SoundOutServiceModel.qml').read_bytes()).hexdigest()
        loaders = ssh("cd %s && for f in index*.html; do echo $(grep -cF 'typeof App){' $f) $f; done" % APP)
        bad = [line for line in loaders.splitlines() if not line.startswith('1 ')]
        if bad or not loaders.strip():
            raise SystemExit('loader check not found exactly once in: %s' % (bad or 'no index pages'))
        if check:
            print('check only: both patches apply (%d index pages)' % len(loaders.splitlines()))
            return
        settings = DIR+'/settings'
        copy = settings+'/app'
        ssh('rm -rf %s && mkdir -p %s && cp -a %s %s && cp -a %s %s/QuickSettings' % (settings, settings, APP, copy, QS, settings), timeout=300)
        ssh('cat > %s/main.js' % copy, (patched/'main.js').read_bytes())
        ssh('cat > %s/appinfo.json' % copy, (patched/'appinfo.json').read_bytes())
        # sed basic regex: parentheses and braces are literal.
        ssh("cd %s && sed -i 's/%s/%s/' index*.html" % (copy, LOADER_OLD, LOADER_NEW))
        remaining = ssh("cd %s && grep -lF 'typeof App){' index*.html | wc -l; grep -lF 'if(!0){' index*.html | wc -l" % copy).split()
        if remaining != ['0', str(len(loaders.splitlines()))]:
            raise SystemExit('loader patch incomplete: %s' % remaining)
        for name in QS_FILES:
            ssh('cat > %s/QuickSettings/%s' % (settings, name), (qs_patched/name).read_bytes())
        ssh('cat > %s/app.sha256' % settings, app_digest.encode())
        ssh('cat > %s/QuickSettings.sha256' % settings, qs_digest.encode())
        ssh('cat > %s/ctl.sh && chmod 755 %s/ctl.sh' % (DIR, DIR), (HERE/'ctl.sh').read_bytes())
        ssh('cat > %s/uninstall-settings.sh && chmod 755 %s/uninstall-settings.sh' % (DIR, DIR), (HERE/'uninstall-settings.sh').read_bytes())
        ssh('cat > /var/lib/webosbrew/init.d/69-airpods-settings && chmod 755 /var/lib/webosbrew/init.d/69-airpods-settings', (HERE/'69-airpods-settings').read_bytes())
        ssh('sh /var/lib/webosbrew/init.d/69-airpods-settings')
        mounts = ssh('cat /proc/mounts')
        for target in (APP, QS):
            if (' %s ' % target) not in mounts:
                raise SystemExit('%s did not mount; its fingerprint may not match' % target)
        print('Installed. Settings > Sound > Sound Out has the AirPods rows now.')
        print('Quick Settings shows the AirPods pills once the system UI reloads it (a TV restart at the latest).')
        print('To remove: run %s/uninstall-settings.sh on the TV' % DIR)


if __name__ == '__main__':
    main()
