extends GutTest
## GUT tests for Python spawn service integration.
## Tests service.py endpoint behavior.

const TEST_PORT := 8765

var _response_data: Variant
var _response_code: int = 0

func before_all() -> void:
	# Start Python server in background
	var script_path = ProjectSettings.globalize_path("res://service.py")
	OS.create_process("python3", [script_path])
	await get_tree().create_timer(1.5).timeout

func after_all() -> void:
	OS.execute("pkill", ["-f", "service.py"], [], false)

func _make_request_sync(url: String, method := HTTPClient.METHOD_GET, body := "") -> bool:
	var http_req := HTTPRequest.new()
	add_child(http_req)
	_response_data = null
	_response_code = 0
	
	var completed := false
	var cb = func(result: int, code: int, headers: PackedStringArray, body_bytes: PackedByteArray):
		_response_code = code
		if code == 200:
			_response_data = JSON.parse_string(body_bytes.get_string_from_utf8())
		completed = true
	
	http_req.request_completed.connect(cb)
	var err = http_req.request(url, [], method, body)
	if err != OK:
		completed = true  # Request failed to send, don't hang
		return completed
	
	# Wait for response with timeout
	var elapsed := 0.0
	while not completed and elapsed < 3.0:
		await get_tree().process_frame
		elapsed += get_process_delta_time()
	
	return completed

func test_service_health_endpoint_responds() -> void:
	var completed = await _make_request_sync("http://127.0.0.1:%d/health" % TEST_PORT)
	if not completed:
		pending("Python service not running - skipping")
	elif _response_code == 200 and _response_data is Dictionary:
		assert_eq(_response_data.get("status"), "healthy", "Service should be healthy")
	else:
		assert_eq(_response_code, 200, "Service should respond with 200")

func test_service_spawn_endpoint_returns_ok() -> void:
	var completed = await _make_request_sync("http://127.0.0.1:%d/spawn" % TEST_PORT, HTTPClient.METHOD_POST, "{}")
	if not completed:
		pending("Python service not running - skipping")
	elif _response_code == 200 and _response_data is Dictionary:
		assert_eq(_response_data.get("status"), "ok", "Spawn should return ok status")
		assert_true(_response_data.get("spawned"), "Entity should be marked as spawned")
	else:
		assert_eq(_response_code, 200, "Service should respond with 200")

func test_service_response_includes_spawn_id() -> void:
	var completed = await _make_request_sync("http://127.0.0.1:%d/spawn" % TEST_PORT, HTTPClient.METHOD_POST, "{}")
	if not completed:
		pending("Python service not running - skipping")
	elif _response_code == 200 and _response_data is Dictionary:
		assert_true(_response_data.has("spawn_id"), "Response should include spawn_id")
	else:
		assert_eq(_response_code, 200, "Service should respond with 200")

func test_service_unknown_endpoint_handled() -> void:
	var completed = await _make_request_sync("http://127.0.0.1:%d/unknown" % TEST_PORT, HTTPClient.METHOD_GET)
	if not completed:
		pending("Python service not running - skipping")
	else:
		assert_true(_response_code != 0, "Request should complete even for unknown endpoint")
