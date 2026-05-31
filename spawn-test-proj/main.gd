extends Node

@onready var _status_label: Label = $CanvasLayer/VBox/StatusLabel
@onready var _output_log: Label = $CanvasLayer/VBox/OutputLog
@onready var _start_btn: Button = $CanvasLayer/VBox/ControlPanel/StartButton
@onready var _spawn_btn: Button = $CanvasLayer/VBox/ControlPanel/SpawnButton
@onready var _stop_btn: Button = $CanvasLayer/VBox/ControlPanel/StopButton

var _service_running := false
var _entities_spawned := 0
var _test_mode := false  # Set by tests to skip UI dependencies

## Parallel spawn state
signal entities_spawned(count: int, names: Array)
var _processing_parallel := false
var _pending_spawn_count := 0
var _spawned_entities: Array = []

func _ready() -> void:
	if _start_btn != null:
		_start_btn.pressed.connect(_on_start_pressed)
		_spawn_btn.pressed.connect(_on_spawn_pressed)
		_stop_btn.pressed.connect(_on_stop_pressed)
		_update_ui("Ready. Press Start to begin.")
	if not _test_mode:
		_on_start_pressed()

func _on_start_pressed() -> void:
	if _service_running:
		_update_ui("Service already running.")
		return
	var svc = get_node_or_null("/root/SpawnService")
	if svc == null:
		_update_ui("SpawnService autoload not found")
		return
	var err = svc.start()
	if err == OK:
		_service_running = true
		_update_ui("Service started. PID=" + str(svc.pid))
	else:
		_update_ui("Failed to start service: " + str(err))

func _on_spawn_pressed() -> void:
	if not _service_running:
		_update_ui("Service not running.")
		return
	var svc = get_node_or_null("/root/SpawnService")
	if svc == null:
		_update_ui("SpawnService autoload not found")
		return
	var result = svc.spawn_entity()
	if result:
		_entities_spawned += 1
		_update_ui("Entity spawned. Total: " + str(_entities_spawned))
	else:
		_update_ui("Spawn request failed.")

func _on_stop_pressed() -> void:
	if not _service_running:
		_update_ui("Service not running.")
		return
	var svc = get_node_or_null("/root/SpawnService")
	if svc != null:
		svc.stop()
	_service_running = false
	_update_ui("Service stopped.")

func _update_ui(msg: String) -> void:
	if _status_label != null:
		_status_label.text = msg
	if _output_log != null:
		_output_log.text = "Entities spawned: " + str(_entities_spawned)

## Parallel spawn functions
func spawn_entities_parallel(names: Array) -> Array:
	_processing_parallel = true
	_pending_spawn_count = names.size()
	var spawned: Array = []
	for name in names:
		var entity = _spawn_entity_internal(name)
		spawned.append(entity)
		_spawned_entities.append(name)
	_processing_parallel = false
	_pending_spawn_count = 0
	entities_spawned.emit(spawned.size(), names)
	return spawned

func spawn_entities_parallel_with_delay(names: Array, delay: float) -> void:
	_processing_parallel = true
	_pending_spawn_count = names.size()
	await get_tree().create_timer(delay).timeout
	var spawned: Array = []
	for name in names:
		var entity = _spawn_entity_internal(name)
		spawned.append(entity)
		_spawned_entities.append(name)
	_processing_parallel = false
	_pending_spawn_count = 0
	entities_spawned.emit(spawned.size(), names)

func _spawn_entity_internal(name: String) -> Node:
	var entity = Node.new()
	entity.name = name
	add_child(entity)
	_entities_spawned += 1
	return entity

func get_spawned_count() -> int:
	return _entities_spawned

func is_processing_parallel() -> bool:
	return _processing_parallel

func get_pending_spawn_count() -> int:
	return _pending_spawn_count

func get_game_state() -> Dictionary:
	return {
		"service_running": _service_running,
		"entities_spawned": _entities_spawned,
		"status": _status_label.text
	}
