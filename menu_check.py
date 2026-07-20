"""Try clicking start via real coords (window center for the START button)."""
import sys, time
sys.path.insert(0, '/Users/costas/Documents/Projects/paraxenia/swarm-controller')
from swarm.qa_tools import launch_game, _state_server_send, kill_game

info = launch_game('/Users/costas/workspace/void-patrol-adaptive-flat-art-run11')
print('launch:', info)
port = info['state_port']

# Try press_button first
print('press_button StartButton...')
r = _state_server_send({'command': 'press_button', 'path': '/root/Main/Menu/Center/VBox/StartButton'}, port=port, timeout=2)
print('press result:', r)

time.sleep(2.0)
r = _state_server_send({'command': 'state'}, port=port, timeout=2)
print('after press state:', r.get('game_state', {}).get('scene_name'), r.get('game_state', {}).get('scene'))

# Also do a real mouse click at window center
print('real mouse click at center...')
r = _state_server_send({'command': 'click_at', 'x': 640, 'y': 400}, port=port, timeout=2)
print('click result:', r)
time.sleep(2.0)
r = _state_server_send({'command': 'state'}, port=port, timeout=2)
print('after click state:', r.get('game_state', {}).get('scene_name'), r.get('game_state', {}).get('scene'))

# Find button bounds via a11y
print('a11y tree...')
r = _state_server_send({'command': 'a11y_tree'}, port=port, timeout=2)
elems = r.get('elements', [])
for e in elems:
    label = e.get('label', '')
    if label:
        print(f"  {e.get('role')} '{label}' bounds={e.get('bounds')} path={e.get('path')}")

try:
    pid = info.get('pid')
    if pid:
        import os, signal
        try: os.kill(pid, signal.SIGTERM)
        except: pass
except Exception as e:
    print('cleanup err:', e)
