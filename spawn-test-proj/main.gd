extends Node
## Primary service implementing the main request path for spawn-test-proj.

signal service_initialized
signal request_processed(request_id: int, result: String)
signal entities_spawned(count: int, names: Array)

var _request_counter: int = 0
var _spawned_entities: Array = []
var _service_ready: bool = false
var _parallel_spawn_queue: Array = []
var _is_processing_parallel: bool = false

func _ready() -> void:
	_spawn_service_manager()
	_service_ready = true
	service_initialized.emit()

func _spawn_service_manager() -> void:
	var manager := Node.new()
	manager.name = "ServiceManager"
	add_child(manager)
	_spawned_entities.append(manager)

func process_request(request_path: String) -> Dictionary:
	_request_counter += 1
	var result := _execute_request_path(request_path)
	request_processed.emit(_request_counter, result)
	return {
		"request_id": _request_counter,
		"path": request_path,
		"result": result,
		"status": "ok"
	}

func _execute_request_path(path: String) -> String:
	var segments := path.split("/", false)
	var output := ""
	for segment in segments:
		output += "[" + segment + "]"
	return output if output != "" else "[root]"

func spawn_entity(entity_name: String) -> Node:
	var entity := Node.new()
	entity.name = entity_name
	add_child(entity)
	_spawned_entities.append(entity)
	return entity

func spawn_entities_parallel(entity_names: Array) -> Array:
	var spawned: Array = []
	for name in entity_names:
		var entity := Node.new()
		entity.name = name if name else "Entity"
		add_child(entity)
		_spawned_entities.append(entity)
		spawned.append(entity)
	entities_spawned.emit(spawned.size(), entity_names)
	return spawned

func spawn_entities_parallel_with_delay(entity_names: Array, delay_ms: float) -> void:
	_is_processing_parallel = true
	_parallel_spawn_queue = entity_names.duplicate()
	var delay_seconds := delay_ms / 1000.0
	await get_tree().create_timer(delay_seconds).timeout
	spawn_entities_parallel(_parallel_spawn_queue)
	_is_processing_parallel = false
	_parallel_spawn_queue.clear()

func is_processing_parallel() -> bool:
	return _is_processing_parallel

func get_pending_spawn_count() -> int:
	return _parallel_spawn_queue.size()

func get_spawned_count() -> int:
	return _spawned_entities.size()

func get_game_state() -> Dictionary:
	var spawned_names: Array = []
	for entity in _spawned_entities:
		spawned_names.append(entity.name)
	return {
		"service_ready": _service_ready,
		"request_counter": _request_counter,
		"spawned_entities": spawned_names,
		"spawned_count": _spawned_entities.size(),
		"processing_parallel": _is_processing_parallel,
		"pending_spawn_count": _parallel_spawn_queue.size()
	}
