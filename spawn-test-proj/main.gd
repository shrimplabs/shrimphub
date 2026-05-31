extends Node

signal service_initialized
signal request_processed(request_id: int, result: String)
signal entities_spawned(count: int, names: Array)

var _service_running := false
var _request_counter := 0
var _entities_spawned := 0
var _spawned_entities_names: Array = []
var _processing_parallel := false
var _pending_spawn_count := 0
var _spawned_entities: Dictionary = {}
var _status_label: Label = null

func _ready() -> void:
	_service_running = true
	service_initialized.emit()

func process_request(path: String) -> Dictionary:
	_request_counter += 1
	var segments := path.strip_edges().split("/", false)
	if segments.is_empty():
		segments = ["root"]
	var result_str := _format_segments(segments)
	var comma_result := _join_segments(segments)
	var result := {
		"request_id": _request_counter,
		"status": "ok",
		"path": path,
		"segments": segments,
		"result": result_str
	}
	request_processed.emit(_request_counter, result_str)
	return result

func _join_segments(arr: Array) -> String:
	var out := ""
	for s in arr:
		if out.is_empty():
			out = str(s)
		else:
			out += "," + str(s)
	return out

func _format_segments(arr: Array) -> String:
	var out := ""
	for s in arr:
		out += "[" + str(s) + "]"
	return out

func spawn_entity(entity_name: String = "") -> Node:
	if entity_name.is_empty():
		entity_name = "Entity_" + str(_entities_spawned + 1)
	var entity = Node.new()
	entity.name = entity_name
	add_child(entity)
	_entities_spawned += 1
	_spawned_entities_names.append(entity_name)
	_spawned_entities[entity_name] = entity
	return entity

func spawn_entities_parallel(names: Array) -> Array:
	_processing_parallel = true
	_pending_spawn_count = names.size()
	var spawned: Array = []
	for name in names:
		spawned.append(spawn_entity(name))
	_processing_parallel = false
	_pending_spawn_count = 0
	entities_spawned.emit(spawned.size(), names)
	return spawned

func spawn_entities_parallel_with_delay(names: Array, delay: float) -> void:
	_processing_parallel = true
	_pending_spawn_count = names.size()
	if get_tree() != null:
		await get_tree().create_timer(delay).timeout
	var spawned: Array = []
	for name in names:
		spawned.append(spawn_entity(name))
	_processing_parallel = false
	_pending_spawn_count = 0
	entities_spawned.emit(spawned.size(), names)

func get_spawned_count() -> int:
	return _entities_spawned

func is_processing_parallel() -> bool:
	return _processing_parallel

func get_pending_spawn_count() -> int:
	return _pending_spawn_count

func get_game_state() -> Dictionary:
	var status_text := ""
	if _status_label != null:
		status_text = _status_label.text
	return {
		"service_ready": _service_running,
		"request_counter": _request_counter,
		"spawned_count": _entities_spawned,
		"spawned_entities": _spawned_entities_names,
		"status": status_text,
		"processing_parallel": _processing_parallel,
		"pending_spawn_count": _pending_spawn_count
	}
