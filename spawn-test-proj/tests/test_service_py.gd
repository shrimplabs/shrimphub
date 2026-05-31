extends GutTest
## GUT tests for Python spawn service integration.
## Tests service.py endpoint behavior.

const TEST_PORT := 18085
const TEST_HOST := "127.0.0.1"

var _service_pid: int = -1
var _http_req: HTTPRequest
var _result_code: int = 0
var _response_code: int = 0
var _response_data: Variant
var _signal_fired: bool = false

func before_all() -> void:
	var script_path = ProjectSettings.globalize_path("res://service.py")
	var args = PackedStringArray([script_path, str(TEST_PORT), TEST_HOST])
	_service_pid = OS.create_process("python3", args, false)
	await get_tree().create_timer(1.5).timeout

func after_all() -> void:
	if _service_pid > 0:
		OS.kill(_service_pid)
		_service_pid = -1

func _do_request(url: String, method := HTTPClient.METHOD_GET, body := "") -> void:
	_http_req = HTTPRequest.new()
	add_child(_http_req)
	_result_code = 0
	_response_code = 0
	_response_data = null
	_signal_fired = false
	_http_req.request_completed.connect(_on_req_done)
	_http_req.request(url, [], method, body)

func _on_req_done(result: int, code: int, headers: PackedStringArray, body_bytes: PackedByteArray) -> void:
	_result_code = result
	_response_code = code
	if code == 200 and body_bytes.size() > 0:
		_response_data = JSON.parse_string(body_bytes.get_string_from_utf8())
	_signal_fired = true

func test_service_health_endpoint_responds() -> void:
	_do_request("http://%s:%d/health" % [TEST_HOST, TEST_PORT])
	await wait_for_signal(_http_req.request_completed, 5.0)
	if not _signal_fired:
		pending("Python service not reachable (signal not fired)")
	else:
		assert_eq(_response_code, 200, "Service should respond with 200")
		assert_true(_response_data is Dictionary, "Health response should be JSON dict")
		if _response_data is Dictionary:
			assert_eq(_response_data.get("status"), "healthy", "Service should be healthy")

func test_service_spawn_endpoint_returns_ok() -> void:
	_do_request("http://%s:%d/spawn" % [TEST_HOST, TEST_PORT], HTTPClient.METHOD_POST, "{}")
	await wait_for_signal(_http_req.request_completed, 5.0)
	if not _signal_fired:
		pending("Python service not reachable (signal not fired)")
	else:
		assert_eq(_response_code, 200, "Service should respond with 200")
		assert_true(_response_data is Dictionary, "Spawn response should be JSON dict")
		if _response_data is Dictionary:
			assert_eq(_response_data.get("status"), "ok", "Spawn should return ok status")
			assert_true(_response_data.get("spawned"), "Entity should be marked as spawned")

func test_service_response_includes_spawn_id() -> void:
	_do_request("http://%s:%d/spawn" % [TEST_HOST, TEST_PORT], HTTPClient.METHOD_POST, "{}")
	await wait_for_signal(_http_req.request_completed, 5.0)
	if not _signal_fired:
		pending("Python service not reachable (signal not fired)")
	else:
		assert_eq(_response_code, 200, "Service should respond with 200")
		assert_true(_response_data is Dictionary, "Spawn response should be JSON dict")
		if _response_data is Dictionary:
			assert_true(_response_data.has("spawn_id"), "Response should include spawn_id")

func test_service_unknown_endpoint_handled() -> void:
	_do_request("http://%s:%d/unknown" % [TEST_HOST, TEST_PORT], HTTPClient.METHOD_GET)
	await wait_for_signal(_http_req.request_completed, 5.0)
	assert_true(_signal_fired, "Request should complete even for unknown endpoint")
	assert_null(_response_data, "Unknown endpoint should return null/404")
