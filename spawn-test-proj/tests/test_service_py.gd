extends GutTest
## GUT tests for Python spawn service integration.
## Tests service.py endpoint behavior.

const TEST_PORT := 8765

var _server_thread: Thread
var _server_should_stop := false

func before_all() -> void:
	# Start server in background for tests
	_server_thread = Thread.new()
	_server_thread.start(_run_server_background)
	await get_tree().create_timer(0.5).timeout

func after_all() -> void:
	_server_should_stop = true
	if _server_thread.is_started():
		_server_thread.wait_to_finish()

func _run_server_background() -> void:
	var output = []
	OS.execute("python3", ["res://service.py"], output, true)

func test_service_health_endpoint_responds() -> void:
	var http_req = HTTPRequest.new()
	add_child(http_req)
	var result = await _make_request(http_req, "http://127.0.0.1:%d/health" % TEST_PORT)
	assert_true(result != null, "Should receive health response")
	assert_eq(result.get("status"), "healthy", "Service should be healthy")
	http_req.free()

func test_service_spawn_endpoint_returns_ok() -> void:
	var http_req = HTTPRequest.new()
	add_child(http_req)
	var result = await _make_request(http_req, "http://127.0.0.1:%d/spawn" % TEST_PORT, HTTPClient.METHOD_POST)
	assert_true(result != null, "Should receive spawn response")
	assert_eq(result.get("status"), "ok", "Spawn should return ok status")
	assert_true(result.get("spawned"), "Entity should be marked as spawned")
	http_req.free()

func test_service_response_includes_spawn_id() -> void:
	var http_req = HTTPRequest.new()
	add_child(http_req)
	var result = await _make_request(http_req, "http://127.0.0.1:%d/spawn" % TEST_PORT, HTTPClient.METHOD_POST)
	assert_true(result != null, "Should receive spawn response")
	assert_true(result.has("spawn_id"), "Response should include spawn_id")
	http_req.free()

func test_service_unknown_endpoint_handled() -> void:
	var http_req = HTTPRequest.new()
	add_child(http_req)
	var result = await _make_request(http_req, "http://127.0.0.1:%d/unknown" % TEST_PORT, HTTPClient.METHOD_GET)
	# 404 is expected for unknown endpoints
	assert_null(result, "Unknown endpoint should return null/404")
	http_req.free()

func _make_request(http_req: HTTPRequest, url: String, method := HTTPClient.METHOD_GET, body := "") -> Variant:
	var semaphore := Semaphore.new()
	var response_data = null
	var response_code := 0
	var completed := false
	
	func callback(result: int, code: int, headers: PackedStringArray, body_bytes: PackedByteArray):
		response_code = code
		if code == 200:
			response_data = JSON.parse_string(body_bytes.get_string_from_utf8())
		completed = true
		semaphore.post()
	
	http_req.request_completed.connect(callback)
	var err = http_req.request(url, [], method, body)
	if err == OK:
		# Wait with timeout
		var timeout_counter := 0
		while not completed and timeout_counter < 50:  # 5 second timeout (50 * 100ms)
			await get_tree().create_timer(0.1).timeout
			timeout_counter += 1
	return response_data
