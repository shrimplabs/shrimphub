import os
import subprocess
import sys

cmd = [
    '/opt/homebrew/bin/godot',
    '--headless',
    '--path', os.path.join(os.path.dirname(os.path.abspath(__file__))),
    '--script', 'res://addons/gut/gut_cmdln.gd',
    '--',
    '-gdir=res://tests',
    '-gexit'
]

r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
print(r.stdout[-6000:] if len(r.stdout) > 6000 else r.stdout)
if r.stderr:
    print(r.stderr[-2000:] if len(r.stderr) > 2000 else r.stderr)
print('EXIT', r.returncode)
sys.exit(r.returncode)
