"""SSH access to a rooted LG TV, read from config.json beside this file.

config.json:
    {"host": "192.168.1.50", "user": "root", "key": "~/.ssh/lgtv_ed25519", "known_hosts": "~/.ssh/known_hosts"}

"known_hosts" is optional. Host keys are always checked (StrictHostKeyChecking=yes), so connect once by hand first.
"""
import json
from pathlib import Path

CONFIG = Path(__file__).resolve().parent/'config.json'
if not CONFIG.exists():
    raise SystemExit('Missing %s; copy config.example.json and fill in your TV address and SSH key.' % CONFIG)
_config = json.loads(CONFIG.read_text())

HOST = _config['host']
SSH = ['ssh', '-i', str(Path(_config['key']).expanduser()), '-o', 'StrictHostKeyChecking=yes',
       '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10']
if _config.get('known_hosts'):
    SSH += ['-o', 'UserKnownHostsFile=' + str(Path(_config['known_hosts']).expanduser())]
SSH.append('%s@%s' % (_config.get('user', 'root'), HOST))
