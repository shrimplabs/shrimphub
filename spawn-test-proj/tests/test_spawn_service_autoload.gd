extends GutTest

const SERVICE_PORT := 18080
const SERVICE_HOST := "127.0.0.1"
var _service_pid: int = -1

func before_each() -> void:
	SpawnService.stop()
	await get_tree().process_frame

func after_each() -> void:
	SpawnService.stop()
	await get_tree().process_frame

func _http_get(req_path: String) -> Dictionary:
	var http := HTTPClient.new()
	var err: Error = http.connect_to_host(SERVICE_HOST, SERVICE_PORT)
	if err != OK:
		return {"error": "connect failed"}
	while http.get_status() == HTTPClient.STATUS_CONNECTING:
		http.poll()
		await get_tree().process_frame
	if http.get_status() != HTTPClient.STATUS_CONNECTED:
		return {"error": "not connected"}
	err = http.request(HTTPClient.METHOD_GET, req_path, [], "")
	if err != OK:
		return {"error": "request failed"}
	while http.get_status() == HTTPClient.STATUS_REQUESTING:
		http.poll()
		await get_tree().process_frame
	var code: int = http.get_response_code()
	var body := PackedByteArray()
	while http.get_status() == HTTPClient.STATUS_BODY:
		http.poll()
		var chunk = http.read_response_body_chunk()
		if chunk.is_empty():
			break
		body.append_array(chunk)
	http.close()
	return {"code": code, "body": body.get_string_from_utf8()}

func _http_post(req_path: String, body: String) -> Dictionary:
	var http := HTTPClient.new()
	var err: Error = http.connect_to_host(SERVICE_HOST, SERVICE_PORT)
	if err != OK:
		return {"error": "connect failed"}
	while http.get_status() == HTTPClient.STATUS_CONNECTING:
		http.poll()
		await get_tree().process_frame
	if http.get_status() != HTTPClient.STATUS_CONNECTED:
		return {"error": "not connected"}
	err = http.request(HTTPClient.METHOD_POST, req_path, [], body)
	if err != OK:
		return {"error": "request failed"}
	while http.get_status() == HTTPClient.STATUS_REQUESTING:
		http.poll()
		await get_tree().process_frame
	var code: int = http.get_response_code()
	var resp_body := PackedByteArray()
	while http.get_status() == HTTPClient.STATUS_BODY:
		http.poll()
		var chunk = http.read_response_body_chunk()
		if chunk.is_empty():
			break
		resp_body.append_array(chunk)
	http.close()
	return {"code": code, "body": resp_body.get_string_from_utf8()}

func test_service_starts_and_is_running() -> void:
	assert_false(SpawnService.is_running(), "not running before start")
	var result: Error = await SpawnService.start()
	assert_eq(result, OK, "start() returns OK")
	await get_tree().create_timer(2.0).timeout
	assert_true(SpawnService.is_running(), "service reports running")
	assert_gt(SpawnService.get_pid(), 0, "pid is positive")

func test_service_stops() -> void:
	assert_eq(await SpawnService.start(), OK, "start() succeeds")
	await get_tree().create_timer(2.0).timeout
	SpawnService.stop()
	await get_tree().process_frame
	assert_false(SpawnService.is_running(), "service reports stopped")
	assert_eq(SpawnService.get_pid(), -1, "pid reset to -1")

func test_double_start_returns_error() -> void:
	assert_eq(await SpawnService.start(), OK, "first start OK")
	await get_tree().create_timer(2.0).timeout
	var result := await SpawnService.start()
	assert_eq(result, ERR_INVALID_PARAMETER, "second start returns ERR_INVALID_PARAMETER")

func test_get_ping_returns_200() -> void:
	assert_eq(await SpawnService.start(), OK, "start() must succeed")
	await get_tree().create_timer(3.0).timeout
	var resp := await _http_get("/ping")
	assert_false(resp.has("error"), "no connection error: " + str(resp))
	assert_eq(resp.get("code"), 200, "GET /ping returns 200")
	var body_str = resp.get("body", "")
	var data = JSON.parse_string(body_str)
	assert_true(data != null, "body parses as JSON: " + body_str)
	assert_eq(data.get("ok"), true, "body.ok == true")

func test_get_health_returns_healthy() -> void:
	assert_eq(await SpawnService.start(), OK, "start() must succeed")
	await get_tree().create_timer(2.0).timeout
	var resp := await _http_get("/health")
	assert_false(resp.has("error"), "no connection error")
	assert_eq(resp.get("code"), 200, "GET /health returns 200")
	var data = JSON.parse_string(resp.get("body", ""))
	assert_true(data != null, "body parses as JSON")
	assert_eq(data.get("status"), "healthy", "body.status == healthy")

func test_post_spawn_returns_ok() -> void:
	assert_eq(await SpawnService.start(), OK, "start() must succeed")
	await get_tree().create_timer(2.0).timeout
	var resp := await _http_post("/spawn", "{}")
	assert_false(resp.has("error"), "no connection error")
	assert_eq(resp.get("code"), 200, "POST /spawn returns 200")
	var data = JSON.parse_string(resp.get("body", ""))
	assert_true(data != null, "body parses as JSON")
	assert_eq(data.get("status"), "ok", "body.status == ok")
	assert_eq(data.get("spawned"), true, "body.spawned == true")
