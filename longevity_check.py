"""Quick test: just leave game alone and see how long godot survives."""
import sys, time
sys.path.insert(0, '/Users/costas/Documents/Projects/paraxenia/swarm-controller')
from swarm.qa_tools import launch_game, _state_server_send, kill_game

info = launch_game('/Users/costas/workspace/void-patrol-adaptive-flat-art-run11')
print('launch:', info)
port = info['state_port']
pid = info.get('pid')

# Press start
_state_server_send({'command': 'press_button', 'path': '/root/Main/MainMenu/StartButton'}, port=port, timeout=2)
time.sleep(1.0)

for i in range(40):
    time.sleep(5.0)
    try:
        r = _state_server_send({'command': 'state'}, port=port, timeout=2)
        gs = r.get('game_state', {})
        alive = bool(gs) and not r.get('error')
        print(f't={i*5+5:3d}s alive={alive} scene={gs.get("scene")} state={gs.get("state")} wave={gs.get("current_wave")} score={gs.get("score")}')
        if not alive:
            print('Connection failed at t=', i*5+5)
            break
    except Exception as e:
        print(f't={i*5+5:3d}s EXC: {e}')
        break

try:
    kill_game(pid)
except Exception as e:
    print('kill err:', e)
