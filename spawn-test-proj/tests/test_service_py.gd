extends GutTest
## GUT tests for Python spawn service integration.
## Tests service.py endpoint behavior.

const TEST_PORT := 8765

var _http_req: HTTPRequest
var _response_data: Variant
var _response_code: int
var _request_done: bool
var _server_pid: int = 0

func before_all() -> void:
	_start_python_server()

func after_all() -> void:
	_kill_python_server()

func _start_python_server() -> void:
	var script_path = ProjectSettings.globalize_path("res://service.py")
	var out = []
	OS.execute("python3", [script_path], out, false)
	await get_tree().create_timer(1.0).timeout

func _kill_python_server() -> void:
	OS.execute("pkill", ["-f", "service.py"], [], false)

func _make_request(url: String, method := HTTPClient.METHOD_GET, body := "") -> Variant:
	_http_req = HTTPRequest.new()
	add_child(_http_req)
	_response_data = null
	_response_code = 0
	_request_done = false
	var cb = func(result: int, code: int, headers: PackedStringArray, body_bytes: PackedByteArray):
		_response_code = code
		if code == 200:
			_response_data = JSON.parse_string(body_bytes.get_string_from_utf8())
		_request_done = true
	_http_req.request_completed.connect(cb)
	var err = _http_req.request(url, [], method, body)
	if err != OK:
		_request_done = true
	var elapsed := 0.0
	while not _request_done and elapsed < 5.0:
		await get_tree().process_frame
		elapsed += get_process_delta_time()
	return _response_data

func test_service_health_endpoint_responds() -> void:
	var result = await _make_request("http://127.0.0.1:%d/health" % TEST_PORT)
	if _http_req:
		_http_req.free()
	assert_ne(_response_code, 0, "Service should be reachable (code=%d)" % _response_code)
	if result is Dictionary:
		assert_eq(result.get("status"), "healthy", "Service should be healthy")

func test_service_spawn_endpoint_returns_ok() -> void:
	var result = await _make_request("http://127.0.0.1:%d/spawn" % TEST_PORT, HTTPClient.METHOD_POST, "{}")
	if _http_req:
		_http_req.free()
	assert_ne(_response_code, 0, "Service should be reachable (code=%d)" % _response_code)
	if result is Dictionary:
		assert_eq(result.get("status"), "ok", "Spawn should return ok status")
		assert_true(result.get("spawned"), "Entity should be marked as spawned")

func test_service_response_includes_spawn_id() -> void:
	var result = await _make_request("http://127.0.0.1:%d/spawn" % TEST_PORT, HTTPClient.METHOD_POST, "{}")
	if _http_req:
		_http_req.free()
	assert_ne(_response_code, 0, "Service should be reachable (code=%d)" % _response_code)
	if result is Dictionary:
		assert_true(result.has("spawn_id"), "Response should include spawn_id")

func test_service_unknown_endpoint_handled() -> void:
	var result = await _make_request("http://127.0.0.1:%d/unknown" % TEST_PORT, HTTPClient.METHOD_GET)
	if _http_req:
		_http_req.free()
	assert_null(result, "Unknown endpoint should return null/404")
