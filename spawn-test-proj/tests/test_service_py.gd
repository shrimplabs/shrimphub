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

func test_service_spawn_endpoint_creates_entity() -> void:
	var http_req = HTTPRequest.new()
	add_child(http_req)
	var result = await _make_request(http_req, "http://127.0.0.1:%d/spawn" % TEST_PORT, HTTPClient.METHOD_POST)
	assert_true(result != null, "Should receive spawn response")
	assert_eq(result.get("status"), "ok", "Spawn should return ok status")
	assert_true(result.get("spawned"), "Entity should be marked as spawned")
	assert_true(result.has("spawn_id"), "Response should include spawn_id")
	http_req.free()

func test_service_unknown_endpoint_returns_404() -> void:
	var http_req = HTTPRequest.new()
	add_child(http_req)
	var result = await _make_request(http_req, "http://127.0.0.1:%d/unknown" % TEST_PORT, HTTPClient.METHOD_GET)
	# 404 is expected for unknown endpoints
	assert_null(result, "Unknown endpoint should return null/404")
	http_req.free()

func _make_request(http_req: HTTPRequest, url: String, method := HTTPClient.METHOD_GET, body := "") -> Dictionary:
	var semaphore := Semaphore.new()
	var response_data = null
	var response_code = 0
	
	var callback = func(result: int, code: int, headers: PackedStringArray, body: PackedByteArray):
		response_code = code
		if code == 200:
			response_data = JSON.parse_string(body.get_string_from_utf8())
		semaphore.post()
	
	http_req.request_completed.connect(callback)
	var err = http_req.request(url, [], method, body)
	if err == OK:
		semaphore.wait()
	return response_data
