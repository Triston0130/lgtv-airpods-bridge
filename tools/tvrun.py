"""Run a shell command on the TV over the configured SSH key, with a caller-chosen timeout.

usage: tvrun.py TIMEOUT_SECONDS 'COMMAND'
"""
import subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent)); import tvssh as tv
r = subprocess.run(tv.SSH + [sys.argv[2]], capture_output=True, timeout=float(sys.argv[1]))
sys.stdout.write(r.stdout.decode(errors='replace'))
sys.stdout.write(''.join(l for l in r.stderr.decode(errors='replace').splitlines(True) if not l.startswith('**')))
