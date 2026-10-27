# autoload/analytics_reporter.gd
# Passive telemetry autoload for shipped games.
#
# Tier 1 (always on): session_start, scene_entered/exited (auto-wired via
# SceneTree.tree_changed), player_died, player_retry, level_complete, victory,
# quit, custom. Public API at the bottom of this file.
#
# Tier 2 (opt-in, per-game): position_sample every POSITION_SAMPLE_INTERVAL
# seconds. Flip TIER2_ENABLED to true to enable.
#
# Privacy: ephemeral session UUID generated in _ready(), stored only in memory,
# never written to disk. If a `user://telemetry_optout` flag file exists, every
# public method and internal helper early-returns silently.
#
# Transport: fire-and-forget HTTP POST via HTTPRequest node. Failures are
# silently dropped (no retry, no queue drain).
#
# Register in project.godot:
#   [autoload]
#   AnalyticsReporter="*res://autoload/analytics_reporter.gd"

extends Node

# --- Configuration (edit per-game) -----------------------------------------

const TELEMETRY_URL := "http://telemetry.example.com/api/telemetry"
const TIER2_ENABLED := false
const POSITION_SAMPLE_INTERVAL := 2.0
const GAME_NAME := "raccoon-city"
const GAME_VERSION := "1.0.0"

# --- Internal state --------------------------------------------------------

var _session_id: String = ""
var _session_start_time_msec: int = 0
var _disabled: bool = false
var _http: HTTPRequest = null
var _last_scene_name: String = ""
var _last_scene_entered_msec: int = 0
var _position_timer: float = 0.0
var _platform: String = ""

# --- Public read-only helpers (for tests / introspection) ------------------

func is_disabled() -> bool:
	return _disabled

func get_session_id() -> String:
	return _session_id


func _ready() -> void:
	process_mode = Node.PROCESS_MODE_ALWAYS
	_session_id = _generate_uuid()
	_session_start_time_msec = Time.get_ticks_msec()
	_platform = _detect_platform()

	# Opt-out: presence of user://telemetry_optout disables everything silently.
	if FileAccess.file_exists("user://telemetry_optout"):
		_disabled = true
		return

	# Fire-and-forget HTTPRequest node.
	_http = HTTPRequest.new()
	_http.request_completed.connect(_on_request_completed)
	add_child(_http)

	# Auto-wire Tier 1 scene change detection via SceneTree.tree_changed.
	var tree := get_tree()
	if tree != null:
		tree.tree_changed.connect(_on_tree_changed)

	# Emit the session_start event so the server has a known anchor for t=0.
	_emit_event("session_start", "", {"game_version": GAME_VERSION})


func _exit_tree() -> void:
	# Best-effort quit event so servers know the session ended.
	if not _disabled:
		var tree := get_tree()
		var current_scene := ""
		if tree != null and tree.current_scene != null:
			current_scene = tree.current_scene.name
		quit_game(current_scene, _elapsed_seconds())
	if _http != null:
		_http.cancel_request()


# --- Tier 1 auto-wiring ----------------------------------------------------

func _on_tree_changed() -> void:
	if _disabled:
		return
	var tree := get_tree()
	if tree == null:
		return
	var current := tree.current_scene
	var current_name := ""
	if current != null:
		current_name = current.name
	if current_name == _last_scene_name:
		return
	# The previous scene just left.
	if _last_scene_name != "":
		var duration_s := 0.0
		if _last_scene_entered_msec > 0:
			duration_s = (Time.get_ticks_msec() - _last_scene_entered_msec) / 1000.0
		scene_exited(_last_scene_name, duration_s)
	_last_scene_name = current_name
	_last_scene_entered_msec = Time.get_ticks_msec()
	if current_name != "":
		scene_entered(current_name, "")


# --- Tier 2 (opt-in) -------------------------------------------------------

func _process(delta: float) -> void:
	if _disabled or not TIER2_ENABLED:
		return
	_position_timer += delta
	if _position_timer < POSITION_SAMPLE_INTERVAL:
		return
	_position_timer = 0.0
	var tree := get_tree()
	if tree == null or tree.current_scene == null:
		return
	var scene_name := tree.current_scene.name
	# Pick the first 2D node that looks like the player (name contains "player"
	# or is a CharacterBody2D). If none found, fall back to viewport center.
	var player := _find_player_node(tree.current_scene)
	var pos: Vector2 = Vector2.ZERO
	if player != null:
		pos = player.global_position
	else:
		pos = get_viewport().get_visible_rect().size * 0.5
	var data := {"x": int(pos.x), "y": int(pos.y)}
	_emit_event("position_sample", scene_name, data)


func _find_player_node(root: Node) -> Node:
	if root is CharacterBody2D:
		return root
	if String(root.name).to_lower().contains("player"):
		return root
	for child in root.get_children():
		var found := _find_player_node(child)
		if found != null:
			return found
	return null


# --- Emit helper ----------------------------------------------------------

func _emit_event(event: String, scene: String, data: Dictionary) -> void:
	if _disabled:
		return
	if _http == null:
		return
	var payload := {
			"session_id": _session_id,
		"game": GAME_NAME,
		"platform": _platform,
		"event": event,
		"scene": scene,
		"t": _elapsed_seconds(),
		"data": data,
	}
	var body := JSON.stringify(payload)
	var headers := ["Content-Type: application/json"]
	# Fire-and-forget: errors are ignored in _on_request_completed.
	_http.request(TELEMETRY_URL, headers, HTTPClient.METHOD_POST, body)


func _on_request_completed(_result: int, _response_code: int, _headers: PackedStringArray, _body: PackedByteArray) -> void:
	# Fire-and-forget: failures silently dropped (no retry, no queue).
	pass


func _elapsed_seconds() -> float:
	if _session_start_time_msec == 0:
		return 0.0
	return (Time.get_ticks_msec() - _session_start_time_msec) / 1000.0


func _detect_platform() -> String:
	if OS.has_feature("web"):
		return "web"
	if OS.has_feature("windows"):
		return "windows"
	if OS.has_feature("macos"):
		return "macos"
	if OS.has_feature("linuxbsd") or OS.has_feature("linux"):
		return "linux"
	if OS.has_feature("android"):
		return "android"
	if OS.has_feature("ios"):
		return "ios"
	return "unknown"


func _generate_uuid() -> String:
	# 32-char lowercase hex (UUID without dashes). Random bytes via Crypto.
	var bytes := PackedByteArray()
	bytes.resize(16)
	for i in 16:
		bytes[i] = randi() % 256
	return bytes.hex_encode()


# --- Public API ------------------------------------------------------------

func scene_entered(scene_name: String, from_scene: String = "") -> void:
	if _disabled:
		return
	_emit_event("scene_entered", scene_name, {"from_scene": from_scene})


func scene_exited(scene_name: String, duration_s: float) -> void:
	if _disabled:
		return
	_emit_event("scene_exited", scene_name, {"duration_s": duration_s})


func player_died(cause: String = "unknown") -> void:
	if _disabled:
		return
	var scene_name := ""
	var tree := get_tree()
	if tree != null and tree.current_scene != null:
		scene_name = tree.current_scene.name
	_emit_event("player_died", scene_name, {"cause": cause, "scene": scene_name})


func player_retry(attempt: int) -> void:
	if _disabled:
		return
	_emit_event("player_retry", _last_scene_name, {"attempt": attempt})


func level_complete(level: String, duration_s: float, attempts: int) -> void:
	if _disabled:
		return
	_emit_event("level_complete", level, {"level": level, "duration_s": duration_s, "attempts": attempts})


func victory(total_duration_s: float, deaths: int) -> void:
	if _disabled:
		return
	_emit_event("victory", _last_scene_name, {"total_duration_s": total_duration_s, "deaths": deaths})


func quit_game(scene: String, playtime_s: float) -> void:
	if _disabled:
		return
	_emit_event("quit", scene, {"scene": scene, "playtime_s": playtime_s})


func custom_event(key: String, value) -> void:
	if _disabled:
		return
	_emit_event("custom", _last_scene_name, {"key": key, "value": value})
