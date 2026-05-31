extends GutTest

const PORT := 18081
const HOST := "127.0.0.1"
var _service_pid: int = -1

func before_each() -> void:
    await _stop_service()

func after_each() -> void:
    await _stop_service()

func _start_service() -> bool:
    var script_path: String = _service_path()
    if script_path.is_empty():
        push_error("service.py not found")
        return false
    var args := PackedStringArray([script_path, str(PORT), HOST])
    _service_pid = OS.create_process("python3", args, false)
    if _service_pid <= 0:
        push_error("Failed to start service.py")
        return false
    await get_tree().create_timer(1.5).timeout
    return true

func _stop_service() -> void:
    if _service_pid > 0:
        OS.kill(_service_pid)
        _service_pid = -1
    await get_tree().create_timer(0.2).timeout

func _service_path() -> String:
    var paths := [
        ProjectSettings.globalize_path("res://service.py"),
        ProjectSettings.globalize_path("res://scripts/service.py"),
    ]
    for p in paths:
        if FileAccess.file_exists(p):
            return p
    return ""

func _http_get(req_path: String) -> Dictionary:
    var http := HTTPClient.new()
    var err: Error = http.connect_to_host(HOST, PORT)
    if err != OK:
        return {"error": "connect failed: " + str(err)}
    while http.get_status() == HTTPClient.STATUS_CONNECTING:
        http.poll()
        await get_tree().process_frame
    if http.get_status() != HTTPClient.STATUS_CONNECTED:
        return {"error": "status: " + str(http.get_status())}
    err = http.request(HTTPClient.METHOD_GET, req_path)
    if err != OK:
        return {"error": "request failed: " + str(err)}
    while http.get_status() == HTTPClient.STATUS_REQUESTING:
        http.poll()
        await get_tree().process_frame
    var code: int = http.get_response_code()
    var body_bytes: PackedByteArray = http.read_response_body()
    http.close()
    return {"code": code, "body": body_bytes.get_string_from_utf8()}

func _http_post(req_path: String, body: String) -> Dictionary:
    var http := HTTPClient.new()
    var err: Error = http.connect_to_host(HOST, PORT)
    if err != OK:
        return {"error": "connect failed: " + str(err)}
    while http.get_status() == HTTPClient.STATUS_CONNECTING:
        http.poll()
        await get_tree().process_frame
    if http.get_status() != HTTPClient.STATUS_CONNECTED:
        return {"error": "status: " + str(http.get_status())}
    err = http.request(HTTPClient.METHOD_POST, req_path, [], body)
    if err != OK:
        return {"error": "request failed: " + str(err)}
    while http.get_status() == HTTPClient.STATUS_REQUESTING:
        http.poll()
        await get_tree().process_frame
    var code: int = http.get_response_code()
    var resp_body: PackedByteArray = http.read_response_body()
    http.close()
    return {"code": code, "body": resp_body.get_string_from_utf8()}

func test_service_starts() -> void:
    var result = await _start_service()
    assert_true(result, "service.py starts without error")
    assert_gt(_service_pid, 0, "service PID is positive")

func test_get_ping_returns_200() -> void:
    assert_true(await _start_service(), "service must start first")
    var resp: Dictionary = await _http_get("/ping")
    assert_false(resp.has("error"), "no connection error")
    assert_eq(resp.get("code"), 200, "GET /ping returns 200")
    var data = JSON.parse_string(resp.get("body", ""))
    assert_true(data != null, "body parses as JSON")
    assert_eq(data.get("ok"), true, "body contains ok:true")

func test_get_health_returns_200() -> void:
    assert_true(await _start_service(), "service must start first")
    var resp: Dictionary = await _http_get("/health")
    assert_false(resp.has("error"), "no connection error")
    assert_eq(resp.get("code"), 200, "GET /health returns 200")
    var data = JSON.parse_string(resp.get("body", ""))
    assert_true(data != null, "body parses as JSON")
    assert_eq(data.get("running"), true, "body contains running:true")

func test_post_spawn_returns_spawned_id() -> void:
    assert_true(await _start_service(), "service must start first")
    var resp: Dictionary = await _http_post("/spawn", "{}")
    assert_false(resp.has("error"), "no connection error")
    assert_eq(resp.get("code"), 200, "POST /spawn returns 200")
    var data = JSON.parse_string(resp.get("body", ""))
    assert_true(data != null, "body parses as JSON")
    assert_eq(data.get("spawned"), true, "body contains spawned:true")
    assert_gt(data.get("id", 0), 0, "entity id is positive")

func test_spawn_increments_id() -> void:
    assert_true(await _start_service(), "service must start first")
    var r1: Dictionary = await _http_post("/spawn", "{}")
    var r2: Dictionary = await _http_post("/spawn", "{}")
    assert_true(r1.has("code") and r2.has("code"), "both requests succeeded")
    var d1 = JSON.parse_string(r1.get("body", ""))
    var d2 = JSON.parse_string(r2.get("body", ""))
    assert_gt(d2.get("id", 0), d1.get("id", 0), "second spawn id > first spawn id")
