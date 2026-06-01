## StateServer \u2014 lightweight TCP state endpoint for QA agents.
##
## Listens on port 11009 by default, or --state-port N if passed via cmdline user args. Supports commands:
##   {"command":"state"}                                    \u2192 JSON snapshot of live game state
##   {"command":"screenshot_b64"}                          \u2192 {"image_base64":"<png base64>"}
##   {"command":"input","type":"click","x":N,"y":N}        \u2192 inject mouse click at game coords
##   {"command":"input","type":"action","action":"ui_accept"} \u2192 inject Godot action
##   {"command":"press_button","id":"LabelOrNodeName"}     \u2192 find button by name or qa_label, fire it
##   {"command":"a11y_tree"}                               \u2192 flat list of interactive UI elements
##
## State response shape (scene_tree is now a full node hierarchy, not type counts):
##   {
##     "timestamp": 1712345678.0,
##     "scene_tree": {                    # recursive node tree
##       "name": "Main", "type": "Node2D", "path": "/root/Main",
##       "visible": true, "position": [0, 0],
##       "children": [
##         {"name": "PlayButton", "type": "Button", "qa_label": "play",
##          "visible": true, "position": [400, 300], "bounds": [375, 285, 50, 30]},
##         ...
##       ]
##     },
##     "game_state": {}                   # from get_game_state() on parent, if implemented
##   }
##
## Button labels: nodes with set_meta("qa_label", "play") can be found by label string.
## This allows press_button{"id":"play"} to work regardless of internal node names.
##
## For backwards compatibility, a bare connection (no command sent) also returns state.
##
## If the parent node implements get_game_state() -> Dictionary, that is called.
## Otherwise only the scene tree hierarchy is returned.
##
## Usage: add as a child node of the root game scene (e.g. Main).

extends Node

const DEFAULT_PORT := 11009

## Maximum JSON payload size in bytes before truncation (1 MB default).
## Screenshots bypass this limit and are sent directly.
const MAX_JSON_SIZE := 1048576

var _port: int = DEFAULT_PORT
var _server: TCPServer = null
var _peers: Array[StreamPeerTCP] = []
## Peers currently being handled by an async _dispatch coroutine (held alive until done)
var _async_peers: Array[StreamPeerTCP] = []

## Pending screenshot_b64 capture (resolved after frame_post_draw)

## Signal emitted when a tile is cleared (tile_type: int, points: int)
## tile_type uses TileType enum values (0-4 for elements, 10-13 for special)
signal tile_cleared(tile_type: int, points: int)


func _ready() -> void:
	# Always process even if the scene tree is paused (e.g. during godot-rl training steps)
	process_mode = Node.PROCESS_MODE_ALWAYS
	# Read port from command-line user args: -- --state-port 11012
	# Falls back to STATE_PORT env var, then DEFAULT_PORT (backward-compatible).
	var user_args := OS.get_cmdline_user_args()
	for i in range(user_args.size() - 1):
		if user_args[i] == "--state-port":
			_port = int(user_args[i + 1])
			break
	# Env var overrides default but loses to explicit cmdline arg
	if _port == DEFAULT_PORT:
		var env_port := OS.get_environment("STATE_PORT")
		if env_port != "":
			_port = int(env_port)
	_server = TCPServer.new()
	var err = _server.listen(_port)
	if err == OK:
		print("StateServer: listening on port %d" % _port)
	else:
		push_warning("StateServer: could not bind port %d (err=%d) \u2014 QA state reads unavailable" % [_port, err])
		_server = null


func _exit_tree() -> void:
	# Disconnect all active peers
	for peer in _peers:
		if is_instance_valid(peer):
			peer.disconnect_from_host()
	_peers.clear()
	
	# Stop the server
	if _server:
		_server.stop()
		_server = null


func _process(_delta: float) -> void:
	if _server == null:
		return

	# Accept new connections
	while _server.is_connection_available():
		var peer: StreamPeerTCP = _server.take_connection()
		peer.set_no_delay(true)
		_peers.append(peer)

	# Service pending peers
	var still_active: Array[StreamPeerTCP] = []
	for peer in _peers:
		if not is_instance_valid(peer):
			continue
		peer.poll()
		var status = peer.get_status()
		if status == StreamPeerTCP.STATUS_CONNECTED:
			# Give client up to ~1 frame to send a command; if nothing arrives, send state (compat)
			var line = _read_line_or_null(peer)
			if line == null:
				# No data yet \u2014 send state immediately for backwards compat (old bare-connect clients)
				_send_json(peer, _get_state())
				peer.disconnect_from_host()
			elif line.is_empty():
				peer.disconnect_from_host()
			else:
				_dispatch(peer, line)
		elif status == StreamPeerTCP.STATUS_CONNECTING:
			still_active.append(peer)
		# STATUS_NONE / STATUS_ERROR \u2014 drop silently
	_peers = still_active


func _dispatch(peer: StreamPeerTCP, raw: String) -> void:
	# Hold a strong reference so the peer isn't dropped while async work runs
	_async_peers.append(peer)
	var data: Dictionary = {}
	var trimmed = raw.strip_edges()
	if trimmed.begins_with("{"):
		var json = JSON.new()
		if json.parse(trimmed) == OK and json.get_data() is Dictionary:
			data = json.get_data()
	var command = str(data.get("command", trimmed))

	match command:
		"state":
			_send_json(peer, _get_state())
			peer.disconnect_from_host()
		"screenshot_b64", "screenshot_base64":
			# Grab viewport image directly — don't wait for frame_post_draw which
			# doesn't fire reliably on macOS when the window is in the background.
			await _handle_screenshot_b64(peer)
		"input":
			await _handle_input(peer, data)
		"press_button":
			_handle_press_button(peer, data)
		"play_macro":
			# Execute a timed sequence of input actions server-side with no round-trips.
			# {"command":"play_macro","actions":[{"type":"hold","action":"move_right","duration":2.0},{"type":"wait","seconds":0.5},...]}
			await _handle_play_macro(peer, data)
		"a11y_tree":
			_handle_a11y_tree(peer)
		_:
			_send_json(peer, {"error": "unknown command: " + command})
			peer.disconnect_from_host()

	# Release the async hold
	_async_peers.erase(peer)


func _handle_screenshot_b64(peer: StreamPeerTCP) -> void:
	# Wait one frame so the viewport texture is populated, then grab it directly.
	# Using process_frame instead of frame_post_draw — the latter doesn't fire
	# reliably on macOS when the window is in the background.
	RenderingServer.force_draw(false)
	await get_tree().process_frame
	var viewport = get_viewport()
	if viewport == null:
		_send_json(peer, {"error": "no viewport"})
		peer.disconnect_from_host()
		return
	var image = viewport.get_texture().get_image()
	if image == null or image.is_empty():
		# Fallback: try forcing another draw and waiting one more frame
		RenderingServer.force_draw(false)
		await get_tree().process_frame
		image = viewport.get_texture().get_image()
	if image == null or image.is_empty():
		_send_json(peer, {"error": "could not get image from viewport"})
		peer.disconnect_from_host()
		return
	var png_bytes = image.save_png_to_buffer()
	var b64 = Marshalls.raw_to_base64(png_bytes)
	# Send directly (bypass _send_json truncation — screenshots are legitimately large)
	var json_str = JSON.stringify({"image_base64": b64})
	peer.put_data((json_str + "\n").to_utf8_buffer())
	peer.disconnect_from_host()


func _handle_play_macro(peer: StreamPeerTCP, data: Dictionary) -> void:
	# Execute a sequence of input actions with delays, entirely server-side.
	var actions = data.get("actions", [])
	_send_ok(peer)
	for act in actions:
		var atype = str(act.get("type", ""))
		match atype:
			"wait":
				await get_tree().create_timer(float(act.get("seconds", 0.1))).timeout
			"hold":
				var action = str(act.get("action", "ui_accept"))
				var dur = float(act.get("duration", 0.5))
				var pe = InputEventAction.new()
				pe.action = action; pe.pressed = true
				Input.parse_input_event(pe)
				await get_tree().create_timer(dur).timeout
				var re = InputEventAction.new()
				re.action = action; re.pressed = false
				Input.parse_input_event(re)
			"key":
				var kc = _key_name_to_keycode(str(act.get("key", "")).to_upper())
				var dur = float(act.get("duration", 0.1))
				if kc != KEY_NONE:
					var kd = InputEventKey.new()
					kd.keycode = kc; kd.pressed = true; kd.echo = false
					Input.parse_input_event(kd)
					await get_tree().create_timer(dur).timeout
					var ku = InputEventKey.new()
					ku.keycode = kc; ku.pressed = false; ku.echo = false
					Input.parse_input_event(ku)
			"key_combo":
				var keys_raw = act.get("keys", [])
				var kcs := []
				for k in keys_raw:
					var kc = _key_name_to_keycode(str(k).to_upper())
					if kc != KEY_NONE: kcs.append(kc)
				for kc in kcs:
					var kd = InputEventKey.new()
					kd.keycode = kc; kd.pressed = true; kd.echo = false
					Input.parse_input_event(kd)
				await get_tree().create_timer(0.1).timeout
				for kc in kcs:
					var ku = InputEventKey.new()
					ku.keycode = kc; ku.pressed = false; ku.echo = false
					Input.parse_input_event(ku)
			"click":
				_inject_click(float(act.get("x", 0)), float(act.get("y", 0)))
			"right_click":
				var x = float(act.get("x", 0)); var y = float(act.get("y", 0))
				var viewport := get_viewport(); var pos := Vector2(x, y)
				_move_mouse(x, y)
				var de = InputEventMouseButton.new()
				de.position = pos; de.global_position = pos
				de.button_index = MOUSE_BUTTON_RIGHT; de.button_mask = MOUSE_BUTTON_MASK_RIGHT; de.pressed = true
				_push_input_event(viewport, de)
				var ue = InputEventMouseButton.new()
				ue.position = pos; ue.global_position = pos
				ue.button_index = MOUSE_BUTTON_RIGHT; ue.button_mask = 0; ue.pressed = false
				_push_input_event(viewport, ue)
			"drag":
				await _inject_drag(float(act.get("x1",0)), float(act.get("y1",0)), float(act.get("x2",0)), float(act.get("y2",0)), float(act.get("duration",0.3)))
			"action":
				var ae = _make_input_event_action(str(act.get("action", "ui_accept")))
				Input.parse_input_event(ae)


func _handle_input(peer: StreamPeerTCP, data: Dictionary) -> void:
	var input_type = str(data.get("type", ""))
	match input_type:
		"move":
			var x = float(data.get("x", 0))
			var y = float(data.get("y", 0))
			_move_mouse(x, y)
			_send_ok(peer)
		"click":
			var x = float(data.get("x", 0))
			var y = float(data.get("y", 0))
			_inject_click(x, y)
			_send_ok(peer)
		"drag":
			# Drag: {"command":"input","type":"drag","x1":N,"y1":N,"x2":N,"y2":N,"duration":0.3}
			var x1 = float(data.get("x1", 0))
			var y1 = float(data.get("y1", 0))
			var x2 = float(data.get("x2", 0))
			var y2 = float(data.get("y2", 0))
			var duration = float(data.get("duration", 0.3))
			_send_ok(peer)
			await _inject_drag(x1, y1, x2, y2, duration)
		"scroll":
			# Scroll wheel: {"command":"input","type":"scroll","x":N,"y":N,"delta":3}
			# delta > 0 = scroll up/forward, delta < 0 = scroll down/back
			var x = float(data.get("x", 0))
			var y = float(data.get("y", 0))
			var delta = float(data.get("delta", 3))
			var viewport := get_viewport()
			var pos := Vector2(x, y)
			var scroll_evt := InputEventMouseButton.new()
			scroll_evt.position = pos
			scroll_evt.global_position = pos
			scroll_evt.button_index = MOUSE_BUTTON_WHEEL_UP if delta > 0 else MOUSE_BUTTON_WHEEL_DOWN
			scroll_evt.pressed = true
			scroll_evt.factor = abs(delta)
			_push_input_event(viewport, scroll_evt)
			_send_ok(peer)
		"right_click":
			# Right click: {"command":"input","type":"right_click","x":N,"y":N}
			var x = float(data.get("x", 0))
			var y = float(data.get("y", 0))
			var viewport := get_viewport()
			var pos := Vector2(x, y)
			_move_mouse(x, y)
			var down_evt := InputEventMouseButton.new()
			down_evt.position = pos
			down_evt.global_position = pos
			down_evt.button_index = MOUSE_BUTTON_RIGHT
			down_evt.button_mask = MOUSE_BUTTON_MASK_RIGHT
			down_evt.pressed = true
			_push_input_event(viewport, down_evt)
			var up_evt := InputEventMouseButton.new()
			up_evt.position = pos
			up_evt.global_position = pos
			up_evt.button_index = MOUSE_BUTTON_RIGHT
			up_evt.button_mask = 0
			up_evt.pressed = false
			_push_input_event(viewport, up_evt)
			_send_ok(peer)
		"double_click":
			# Double click: {"command":"input","type":"double_click","x":N,"y":N}
			var x = float(data.get("x", 0))
			var y = float(data.get("y", 0))
			_inject_click(x, y)
			await get_tree().create_timer(0.1).timeout
			_inject_click(x, y)
			_send_ok(peer)
		"key_combo":
			# Simultaneous keys: {"command":"input","type":"key_combo","keys":["shift","w"]}
			# Presses all keys simultaneously, holds 0.1s, releases all.
			var keys_raw = data.get("keys", [])
			var keycodes := []
			for k in keys_raw:
				var kc = _key_name_to_keycode(str(k).to_upper())
				if kc != KEY_NONE:
					keycodes.append(kc)
			_send_ok(peer)
			for kc in keycodes:
				var kdown = InputEventKey.new()
				kdown.keycode = kc
				kdown.pressed = true
				kdown.echo = false
				Input.parse_input_event(kdown)
			await get_tree().create_timer(0.1).timeout
			for kc in keycodes:
				var kup = InputEventKey.new()
				kup.keycode = kc
				kup.pressed = false
				kup.echo = false
				Input.parse_input_event(kup)
		"action":
			var action = str(data.get("action", "ui_accept"))
			Input.parse_input_event(_make_input_event_action(action))
			_send_ok(peer)
		"hold":
			# Hold an action for N seconds: {"command":"input","type":"hold","action":"move_right","duration":2.0}
			var action = str(data.get("action", "ui_accept"))
			var duration = float(data.get("duration", 0.5))
			_send_ok(peer)
			var press_evt = InputEventAction.new()
			press_evt.action = action
			press_evt.pressed = true
			Input.parse_input_event(press_evt)
			await get_tree().create_timer(duration).timeout
			var release_evt = InputEventAction.new()
			release_evt.action = action
			release_evt.pressed = false
			Input.parse_input_event(release_evt)
		"key":
			# Physical key by name: {"command":"input","type":"key","key":"w","duration":2.0}
			var key_name = str(data.get("key", "")).to_upper()
			var duration = float(data.get("duration", 0.1))
			var keycode = _key_name_to_keycode(key_name)
			if keycode == KEY_NONE:
				_send_json(peer, {"error": "unknown key: " + key_name})
				peer.disconnect_from_host()
				return
			_send_ok(peer)
			var kdown = InputEventKey.new()
			kdown.keycode = keycode
			kdown.pressed = true
			kdown.echo = false
			Input.parse_input_event(kdown)
			await get_tree().create_timer(duration).timeout
			var kup = InputEventKey.new()
			kup.keycode = keycode
			kup.pressed = false
			kup.echo = false
			Input.parse_input_event(kup)
		"type":
			# Legacy bare {"command":"input"} \u2014 treat as action ui_accept
			Input.parse_input_event(_make_input_event_action("ui_accept"))
			_send_ok(peer)
		_:
			_send_json(peer, {"error": "unknown input type: " + input_type})
			peer.disconnect_from_host()


func _handle_press_button(peer: StreamPeerTCP, data: Dictionary) -> void:
	# Convenience: press_button{"id":"ButtonPath"} emits the button's pressed signal
	# directly. This is more reliable for Control-based UI than toggling state.
	var button_id = str(data.get("id", ""))
	if button_id.is_empty():
		_send_json(peer, {"error": "missing button id"})
		peer.disconnect_from_host()
		return

	var button = _find_node_by_name(get_tree().root, button_id)
	if button == null:
		_send_json(peer, {"error": "button not found: " + button_id})
		peer.disconnect_from_host()
		return

	if not button.has_signal("pressed"):
		_send_json(peer, {"error": "node is not a button: " + button_id})
		peer.disconnect_from_host()
		return

	button.emit_signal("pressed")
	_send_ok(peer)


func _find_node_by_name(root: Node, path: String) -> Node:
	# Supports absolute paths, node name, qa_label metadata, or visible text.
	if path.begins_with("/"):
		return root.get_node_or_null(path)
	var needle := path.strip_edges().to_lower()
	# Breadth-first search: match by node name or qa_label metadata
	var queue: Array[Node] = [root]
	while not queue.is_empty():
		var node: Node = queue.pop_front()
		if str(node.name) == path:
			return node
		if node.has_meta("qa_label") and str(node.get_meta("qa_label")).to_lower() == needle:
			return node
		if node is Control:
			var text_val = node.get("text")
			if text_val is String and text_val.strip_edges().to_lower() == needle:
				return node
			if text_val is String and needle in text_val.strip_edges().to_lower():
				return node
		if str(node.name).to_lower() == needle:
			return node
		for child in node.get_children():
			queue.append(child)
	return null


func _make_input_event_action(action: String) -> InputEvent:
	var evt = InputEventAction.new()
	evt.action = action
	evt.pressed = true
	return evt


func _key_name_to_keycode(key_name: String) -> Key:
	# Map common key name strings to Godot Key enum values.
	var map: Dictionary = {
		"W": KEY_W, "A": KEY_A, "S": KEY_S, "D": KEY_D,
		"UP": KEY_UP, "DOWN": KEY_DOWN, "LEFT": KEY_LEFT, "RIGHT": KEY_RIGHT,
		"SPACE": KEY_SPACE, "ENTER": KEY_ENTER, "RETURN": KEY_ENTER,
		"ESCAPE": KEY_ESCAPE, "ESC": KEY_ESCAPE,
		"SHIFT": KEY_SHIFT, "CTRL": KEY_CTRL, "ALT": KEY_ALT,
		"TAB": KEY_TAB, "BACKSPACE": KEY_BACKSPACE,
		"Q": KEY_Q, "E": KEY_E, "R": KEY_R, "F": KEY_F,
		"G": KEY_G, "H": KEY_H, "I": KEY_I, "J": KEY_J,
		"K": KEY_K, "L": KEY_L, "M": KEY_M, "N": KEY_N,
		"O": KEY_O, "P": KEY_P, "T": KEY_T, "U": KEY_U,
		"V": KEY_V, "X": KEY_X, "Y": KEY_Y, "Z": KEY_Z,
		"1": KEY_1, "2": KEY_2, "3": KEY_3, "4": KEY_4, "5": KEY_5,
		"6": KEY_6, "7": KEY_7, "8": KEY_8, "9": KEY_9, "0": KEY_0,
		"F1": KEY_F1, "F2": KEY_F2, "F3": KEY_F3, "F4": KEY_F4,
		"F5": KEY_F5, "F6": KEY_F6, "F7": KEY_F7, "F8": KEY_F8,
	}
	return map.get(key_name, KEY_NONE)


func _inject_click(x: float, y: float) -> void:
	var viewport := get_viewport()
	var pos := Vector2(x, y)

	_move_mouse(x, y)

	var down_evt := InputEventMouseButton.new()
	down_evt.position = pos
	down_evt.global_position = pos
	down_evt.button_index = MOUSE_BUTTON_LEFT
	down_evt.button_mask = MOUSE_BUTTON_MASK_LEFT
	down_evt.pressed = true
	_push_input_event(viewport, down_evt)

	var up_evt := InputEventMouseButton.new()
	up_evt.position = pos
	up_evt.global_position = pos
	up_evt.button_index = MOUSE_BUTTON_LEFT
	up_evt.button_mask = 0
	up_evt.pressed = false
	_push_input_event(viewport, up_evt)


func _inject_drag(x1: float, y1: float, x2: float, y2: float, duration: float) -> void:
	var viewport := get_viewport()
	var p1 := Vector2(x1, y1)
	var p2 := Vector2(x2, y2)
	_move_mouse(x1, y1)
	var down_evt := InputEventMouseButton.new()
	down_evt.position = p1
	down_evt.global_position = p1
	down_evt.button_index = MOUSE_BUTTON_LEFT
	down_evt.button_mask = MOUSE_BUTTON_MASK_LEFT
	down_evt.pressed = true
	_push_input_event(viewport, down_evt)
	# Interpolate mouse movement across duration
	var steps: int = max(1, int(duration * 30.0))
	for i in range(1, steps + 1):
		var t = float(i) / float(steps)
		var mx = lerp(x1, x2, t)
		var my = lerp(y1, y2, t)
		_move_mouse(mx, my)
		var move_evt := InputEventMouseMotion.new()
		move_evt.position = Vector2(mx, my)
		move_evt.global_position = Vector2(mx, my)
		move_evt.button_mask = MOUSE_BUTTON_MASK_LEFT
		_push_input_event(viewport, move_evt)
		await get_tree().process_frame
	_move_mouse(x2, y2)
	var up_evt := InputEventMouseButton.new()
	up_evt.position = p2
	up_evt.global_position = p2
	up_evt.button_index = MOUSE_BUTTON_LEFT
	up_evt.button_mask = 0
	up_evt.pressed = false
	_push_input_event(viewport, up_evt)


func _move_mouse(x: float, y: float) -> void:
	var viewport := get_viewport()
	var pos := Vector2(x, y)

	# Do NOT call DisplayServer.warp_mouse() — that moves the user's real system
	# cursor and steals focus from other applications. viewport.warp_mouse() +
	# Input.parse_input_event is sufficient for all in-game logic since Godot
	# games read mouse position from InputEvent, not DisplayServer.
	if viewport != null:
		viewport.warp_mouse(pos)

	var move_evt := InputEventMouseMotion.new()
	move_evt.position = pos
	move_evt.global_position = pos
	move_evt.relative = Vector2.ZERO
	move_evt.velocity = Vector2.ZERO
	move_evt.button_mask = 0
	_push_input_event(viewport, move_evt)


func _push_input_event(viewport: Viewport, event: InputEvent) -> void:
	if viewport != null:
		viewport.push_input(event, true)
	Input.parse_input_event(event)
	Input.flush_buffered_events()


func _read_line_or_null(peer: StreamPeerTCP) -> Variant:
	peer.poll()
	var available = peer.get_available_bytes()
	if available == 0:
		return null
	var data = peer.get_data(available)
	if data[0] != OK:
		return null
	return data[1].get_string_from_utf8()


func _send_json(peer: StreamPeerTCP, data: Dictionary) -> void:
	var json_str = JSON.stringify(data)
	var truncated := false
	
	# Truncate if the JSON output exceeds MAX_JSON_SIZE to avoid TCP buffer drops.
	# This also manages context window size for LLM agents reading the state.
	if json_str.to_utf8_buffer().size() > MAX_JSON_SIZE:
		var truncated_str = json_str.substr(0, MAX_JSON_SIZE - len("...\"_truncated\":true}"))
		# Find last comma to avoid malformed JSON when truncating mid-key/value
		var last_comma = truncated_str.rfind(",")
		if last_comma > 0:
			truncated_str = truncated_str.substr(0, last_comma) + ",\"_truncated\":true}"
		else:
			truncated_str = '{"_truncated":true}'
		json_str = truncated_str
		truncated = true
	
	if truncated:
		push_warning("StateServer: JSON payload truncated to %d bytes" % MAX_JSON_SIZE)
	
	var bytes = (json_str + "\n").to_utf8_buffer()
	peer.put_data(bytes)


func _send_ok(peer: StreamPeerTCP) -> void:
	_send_json(peer, {"ok": true})


func _get_state() -> Dictionary:
	var state: Dictionary = {
		"timestamp": Time.get_unix_time_from_system(),
		"scene_tree": {},
		"game_state": {},
	}

	# Walk scene tree — full hierarchy (names, paths, types, visibility, positions, qa_labels)
	state["scene_tree"] = _export_node(get_tree().root)

	# Find the main scene node and call get_game_state() on it.
	# StateServer is an autoload so get_parent() is the Window root, not Main.
	# Traverse root children to find the first node with get_game_state().
	state["game_state"] = {}
	for child in get_tree().root.get_children():
		if child != self and child.has_method("get_game_state"):
			state["game_state"] = child.get_game_state()
			break

	# Mouse mode -- critical for detecting title screen / UI input blockage.
	# "visible"=mouse free, "captured"=FPS mode, "hidden"=invisible but free, "confined"=locked to window
	var mouse_mode_names := {
		Input.MOUSE_MODE_VISIBLE: "visible",
		Input.MOUSE_MODE_HIDDEN: "hidden",
		Input.MOUSE_MODE_CAPTURED: "captured",
		Input.MOUSE_MODE_CONFINED: "confined",
		Input.MOUSE_MODE_CONFINED_HIDDEN: "confined_hidden",
	}
	state["mouse_mode"] = mouse_mode_names.get(Input.mouse_mode, "unknown")

	return state


## Returns a recursive dictionary describing a node and all its children.
## Includes: name, type, path, visible (CanvasItem), position, bounds (Control), qa_label.
## Games can tag nodes with set_meta("qa_label", "play") for stable label-based lookup.
func _export_node(node: Node) -> Dictionary:
	var info: Dictionary = {
		"name": node.name,
		"type": node.get_class(),
		"path": str(node.get_path()),
	}
	if node.has_meta("qa_label"):
		info["qa_label"] = node.get_meta("qa_label")
	if node is CanvasItem:
		info["visible"] = node.visible
	if node is Node2D:
		info["position"] = [node.global_position.x, node.global_position.y]
	if node is Control:
		info["position"] = [node.position.x, node.position.y]
		var r: Rect2 = node.get_global_rect()
		info["bounds"] = [r.position.x, r.position.y, r.size.x, r.size.y]
	var children: Array = []
	for child in node.get_children():
		children.append(_export_node(child))
	if children.size() > 0:
		info["children"] = children
	return info


## Returns a flat array of all visible interactive UI elements in the scene tree.
## Each element: {role, label, path, bounds [x,y,w,h], visible}.
## Roles: button, label, input, progressbar, slider, listbox, widget.
## Labels: qa_label metadata takes priority over node.text, fallback to node.name.
func _handle_a11y_tree(peer: StreamPeerTCP) -> void:
	var result: Array = []
	_collect_a11y_nodes(get_tree().root, result)
	_send_json(peer, {"a11y_tree": result})
	peer.disconnect_from_host()


func _collect_a11y_nodes(node: Node, result: Array) -> void:
	if node is Control and node.visible:
		result.append(_build_a11y_element(node))
	for child in node.get_children():
		_collect_a11y_nodes(child, result)


func _build_a11y_element(node: Control) -> Dictionary:
	var rect: Rect2 = node.get_global_rect()
	return {
		"role":   _a11y_role(node),
		"label":  _a11y_label(node),
		"path":   str(node.get_path()),
		"bounds": [rect.position.x, rect.position.y, rect.size.x, rect.size.y],
		"visible": node.visible,
	}


func _a11y_role(node: Control) -> String:
	if node is BaseButton:   return "button"
	if node is Label or node is RichTextLabel: return "label"
	if node is LineEdit or node is TextEdit:   return "input"
	if node is ProgressBar:  return "progressbar"
	if node is Slider:       return "slider"
	if node is ItemList or node is OptionButton: return "listbox"
	return "widget"


func _a11y_label(node: Control) -> String:
	if node.has_meta("qa_label"):
		return str(node.get_meta("qa_label"))
	var text_val = node.get("text")
	if text_val is String and not text_val.is_empty():
		return text_val
	return node.name
