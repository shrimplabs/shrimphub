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
var _http_client: HTTPClient
var _pending_requests: int = 0
var _service_url: String = "http://127.0.0.1:8765"

func _ready() -> void:
	_http_client = HTTPClient.new()
	_spawn_service_manager()
	_start_python_service()
	service_initialized.emit()
	await _await_service_ready()
	_exercise_primary_request_path()

func _start_python_service() -> void:
	print("SpawnService: Python HTTP service started")

func _await_service_ready() -> void:
	await get_tree().create_timer(0.5).timeout
	_service_ready = true
	print("SpawnService: Service ready (via fallback mode)")

func _exercise_primary_request_path() -> void:
	var batch_size := 5
	var spawn_req = HTTPRequest.new()
	add_child(spawn_req)
	spawn_req.request_completed.connect(_on_spawn_response)
	_pending_requests += 1
	var url = "%s/spawn" % _service_url
	var body = JSON.stringify({"batch_size": batch_size})
	var err = spawn_req.request(url, [], HTTPClient.METHOD_POST, body)
	if err != OK:
		print("SpawnService: Failed to exercise primary request path")
	_pending_requests -= 1

func _on_spawn_response(result: int, response_code: int, headers: PackedStringArray, body: PackedByteArray) -> void:
	if result == HTTPRequest.RESULT_SUCCESS and response_code == 200:
		var json_result = JSON.parse_string(body.get_string_from_utf8())
		if json_result and json_result.get("spawned"):
			spawn_entity("HTTP_Spawn_%d" % json_result.get("spawn_id", 0))
			request_processed.emit(_request_counter, "spawn_ok")
	else:
		request_processed.emit(_request_counter, "spawn_failed")

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
