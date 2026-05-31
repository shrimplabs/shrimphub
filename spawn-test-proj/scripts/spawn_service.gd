extends Node

# SpawnService -- Autoload singleton managing the Python HTTP service lifecycle.
# Endpoints: GET /ping, GET /health, POST /spawn

const SERVICE_SCRIPT := "service.py"
const SERVICE_PORT := 18080
const SERVICE_HOST := "127.0.0.1"

var pid: int = -1
var _running := false

signal service_started(pid: int)
signal service_stopped()

func _notification(what: int) -> void:
	if what == NOTIFICATION_PREDELETE:
		stop()

func start() -> Error:
	if _running:
		return ERR_INVALID_PARAMETER
	var svc_path: String = _find_service_script()
	if svc_path.is_empty():
		push_error("SpawnService: service.py not found")
		return ERR_FILE_NOT_FOUND
	var args := PackedStringArray([svc_path, str(SERVICE_PORT), SERVICE_HOST])
	pid = OS.create_process("python3", args, false)
	if pid == -1:
		push_error("SpawnService: failed to start service")
		return FAILED
	_running = true
	await get_tree().create_timer(1.5).timeout
	service_started.emit(pid)
	return OK

func stop() -> void:
	if pid != -1 and pid > 0:
		OS.kill(pid)
		pid = -1
	_running = false
	service_stopped.emit()

func spawn_entity() -> bool:
	if not _running:
		push_warning("SpawnService: service not running")
		return false
	var http := HTTPRequest.new()
	add_child(http)
	http.request_completed.connect(_on_spawn_response.bind(http))
	var url := "http://" + SERVICE_HOST + ":" + str(SERVICE_PORT) + "/spawn"
	var err = http.request(url, [], HTTPClient.METHOD_POST, '{}')
	if err != OK:
		http.queue_free()
		return false
	return true

func _on_spawn_response(_result: int, _code: int, _hdrs: PackedStringArray, _body: PackedByteArray, http: HTTPRequest) -> void:
	http.queue_free()

func _find_service_script() -> String:
	var bases: Array[String] = [
		ProjectSettings.globalize_path("res://"),
		ProjectSettings.globalize_path("res://scripts/"),
	]
	for base: String in bases:
		var full_path: String = base + SERVICE_SCRIPT
		if FileAccess.file_exists(full_path):
			return full_path
	return ""
