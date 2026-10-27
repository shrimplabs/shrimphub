# autoload/test_harness.gd
# Synchronous checkpoint protocol for AI-driven testing.
# Only active when launched with --test-harness flag.
#
# TWO PHASES:
#
# 1. NAVIGATION PHASE (before game calls checkpoint)
#    The harness accepts connections in _process() and handles navigation commands:
#      {"type": "goto_scene",    "path": "res://scenes/game.tscn"}
#      {"type": "press_button",  "id": "ButtonName"}
#      {"type": "input_action",  "action": "ui_accept"}
#      {"type": "wait"}
#    Each responds with: {"status": "navigating", "scene": "<current_scene_name>"}
#    Use this to get through menus before gameplay starts.
#
# 2. GAME PHASE (game calls checkpoint() at each stable state)
#    The game sends state JSON, agent sends action JSON back.
#    Same port, same harness_step() tool — the agent doesn't need to know which phase.
#
# Usage (in your game's stable-state handler):
#
#   if TestHarness.ENABLED:
#       var action = await TestHarness.checkpoint({
#           "event": "board_stable",
#           "score": score,
#           "moves_remaining": moves,
#       })
#       if action.get("type") == "end_test":
#           get_tree().quit()
#
# Register in project.godot:
#   [autoload]
#   TestHarness="res://autoload/test_harness.gd"
#
# Launch with:
#   godot --headless --path /path/to/project -- --test-harness

extends Node

const DEFAULT_PORT := 11010

var ENABLED: bool = false
var _port: int = DEFAULT_PORT

var _server: TCPServer = null
var _in_checkpoint: bool = false  # true while checkpoint() owns the next connection


func _ready() -> void:
	ENABLED = "--test-harness" in OS.get_cmdline_user_args()
	if not ENABLED:
		return
	process_mode = Node.PROCESS_MODE_ALWAYS
	# Read port from command-line user args: -- --harness-port 11013
	# Falls back to DEFAULT_PORT if not specified (backward-compatible).
	var user_args := OS.get_cmdline_user_args()
	for i in range(user_args.size() - 1):
		if user_args[i] == "--harness-port":
			_port = int(user_args[i + 1])
			break
	_server = TCPServer.new()
	var err = _server.listen(_port)
	if err != OK:
		push_error("[TestHarness] Failed to listen on port %d: %d" % [_port, err])
		_server = null
		return
	print("[TestHarness] Listening on port %d" % _port)


func _exit_tree() -> void:
	if _server:
		_server.stop()
		_server = null


func is_enabled() -> bool:
	return ENABLED


func _process(_delta: float) -> void:
	# Navigation phase: handle connections that arrive outside of checkpoint()
	if not ENABLED or _server == null or _in_checkpoint:
		return
	if not _server.is_connection_available():
		return

	var peer: StreamPeerTCP = _server.take_connection()
	peer.set_no_delay(true)

	# Read the command (give it a few frames to arrive)
	var buf := PackedByteArray()
	var frames := 0
	while frames < 10:
		peer.poll()
		var available := peer.get_available_bytes()
		if available > 0:
			var result = peer.get_data(available)
			if result[0] == OK:
				buf.append_array(result[1])
			break
		elif peer.get_status() != StreamPeerTCP.STATUS_CONNECTED:
			break
		await get_tree().process_frame
		frames += 1

	var cmd: Dictionary = {}
	if buf.size() > 0:
		var parsed = JSON.parse_string(buf.get_string_from_utf8().strip_edges())
		if parsed is Dictionary:
			cmd = parsed

	_handle_nav_command(peer, cmd)
	peer.disconnect_from_host()


func _handle_nav_command(peer: StreamPeerTCP, cmd: Dictionary) -> void:
	var cmd_type := str(cmd.get("type", "wait"))
	var scene_name := _current_scene_name()

	match cmd_type:
		"goto_scene":
			var path := str(cmd.get("path", ""))
			if path.is_empty():
				_respond(peer, {"error": "goto_scene requires a path"})
				return
			get_tree().change_scene_to_file(path)
			_respond(peer, {"status": "navigating", "action": "goto_scene", "path": path})

		"press_button":
			var id := str(cmd.get("id", ""))
			if id.is_empty():
				_respond(peer, {"error": "press_button requires an id"})
				return
			var button = _find_node(get_tree().root, id)
			if button == null:
				_respond(peer, {"error": "button not found: " + id, "scene": scene_name})
				return
			if not button.has_signal("pressed"):
				_respond(peer, {"error": "node has no pressed signal: " + id})
				return
			button.emit_signal("pressed")
			_respond(peer, {"status": "navigating", "action": "press_button", "id": id, "scene": scene_name})

		"input_action":
			var action := str(cmd.get("action", "ui_accept"))
			var evt := InputEventAction.new()
			evt.action = action
			evt.pressed = true
			Input.parse_input_event(evt)
			_respond(peer, {"status": "navigating", "action": "input_action", "action_name": action, "scene": scene_name})

		"screenshot":
			var img = get_viewport().get_texture().get_image()
			if img == null:
				_respond(peer, {"error": "could not capture screenshot"})
				return
			var png_bytes = img.save_png_to_buffer()
			var b64 = Marshalls.raw_to_base64(png_bytes)
			_respond(peer, {"status": "navigating", "image_base64": b64})

		_:  # "wait" or unknown — just return current scene
			_respond(peer, {"status": "navigating", "scene": scene_name})


func _respond(peer: StreamPeerTCP, data: Dictionary) -> void:
	peer.put_data((JSON.stringify(data) + "\n").to_utf8_buffer())


func _current_scene_name() -> String:
	var scene = get_tree().current_scene
	if scene == null:
		return "unknown"
	return scene.scene_file_path.get_file().get_basename()


func _find_node(root: Node, id: String) -> Node:
	if root.name == id:
		return root
	if root.has_meta("qa_label") and root.get_meta("qa_label") == id:
		return root
	for child in root.get_children():
		var found = _find_node(child, id)
		if found:
			return found
	return null


func checkpoint(state: Dictionary) -> Dictionary:
	## Called by game logic at a stable point. Sends state to the controller,
	## waits for an action response, returns it.
	if not ENABLED or _server == null:
		return {}

	_in_checkpoint = true
	get_tree().paused = true

	# Wait for controller to connect
	while not _server.is_connection_available():
		await get_tree().process_frame

	var peer: StreamPeerTCP = _server.take_connection()
	peer.set_no_delay(true)

	# Send state
	peer.put_data((JSON.stringify(state) + "\n").to_utf8_buffer())

	# Read action response
	var action := {}
	var buf := PackedByteArray()
	while true:
		peer.poll()
		var available := peer.get_available_bytes()
		if available > 0:
			var result = peer.get_data(available)
			if result[0] == OK:
				buf.append_array(result[1])
				var text := buf.get_string_from_utf8()
				if "\n" in text:
					var parsed = JSON.parse_string(text.strip_edges())
					if parsed is Dictionary:
						action = parsed
					break
		elif peer.get_status() != StreamPeerTCP.STATUS_CONNECTED:
			break
		else:
			await get_tree().process_frame

	peer.disconnect_from_host()
	get_tree().paused = false
	_in_checkpoint = false
	return action
