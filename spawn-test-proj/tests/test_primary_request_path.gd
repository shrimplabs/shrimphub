extends GutTest

## Test suite for the primary request path of spawn-test-proj.
## Covers: service boot, HTTP request exercise, entity spawning, and game state.

var _main: Node
var _main_script: GDScript
var _request_ids: Array
var _request_results: Array

func before_each() -> void:
	_main_script = load("res://main.gd")
	_main = _main_script.new()
	add_child(_main)
	_request_ids.clear()
	_request_results.clear()

func after_each() -> void:
	if _main:
		_main.free()

func _on_request_processed(rid: int, result: String) -> void:
	_request_ids.append(rid)
	_request_results.append(result)

# ── Service lifecycle ────────────────────────────────────────────────────────

func test_service_initialized_signal_exists() -> void:
	assert_true(_main.has_signal("service_initialized"),
		"main should have service_initialized signal")

func test_request_processed_signal_exists() -> void:
	assert_true(_main.has_signal("request_processed"),
		"main should have request_processed signal")

func test_entities_spawned_signal_exists() -> void:
	assert_true(_main.has_signal("entities_spawned"),
		"main should have entities_spawned signal")

# ── process_request ──────────────────────────────────────────────────────────

func test_process_request_returns_dict() -> void:
	var result = _main.process_request("api/v1/entity")
	assert_true(result is Dictionary, "process_request should return Dictionary")
	assert_true(result.has("request_id"), "result should have request_id")
	assert_true(result.has("path"), "result should have path")
	assert_true(result.has("result"), "result should have result")
	assert_true(result.has("status"), "result should have status")

func test_process_request_increments_counter() -> void:
	var initial = _main._request_counter
	_main.process_request("test/path")
	assert_eq(_main._request_counter, initial + 1, "request counter should increment")

func test_process_request_path_segmentation() -> void:
	var result = _main.process_request("a/b/c")
	assert_eq(result["result"], "[a][b][c]", "path segments should be bracketed")

func test_process_request_empty_path() -> void:
	var result = _main.process_request("")
	assert_eq(result["result"], "[root]", "empty path should return [root]")

func test_process_request_emits_signal() -> void:
	_main.request_processed.connect(_on_request_processed)
	_main.process_request("signal/test")
	assert_true(_request_ids.size() > 0, "request_processed signal should fire")
	assert_eq(_request_results[0], "[signal][test]", "result should be bracketed segments")

# ── spawn_entity ─────────────────────────────────────────────────────────────

func test_spawn_entity_returns_node() -> void:
	var entity = _main.spawn_entity("TestNode")
	assert_not_null(entity, "spawn_entity should return a Node")
	assert_eq(entity.name, "TestNode", "entity name should match")
	entity.free()

func test_spawn_entity_increments_count() -> void:
	var before = _main.get_spawned_count()
	var entity = _main.spawn_entity("IncTest")
	assert_eq(_main.get_spawned_count(), before + 1, "spawned count should increase by 1")
	entity.free()

func test_spawn_entity_multiple_unique_names() -> void:
	var e1 = _main.spawn_entity("Entity_A")
	var e2 = _main.spawn_entity("Entity_B")
	assert_ne(e1.name, e2.name, "entity names should be unique")
	e1.free()
	e2.free()

# ── spawn_entities_parallel ───────────────────────────────────────────────────

func test_spawn_entities_parallel_returns_array() -> void:
	var result = _main.spawn_entities_parallel(["P1", "P2", "P3"])
	assert_true(result is Array, "should return Array")
	assert_eq(result.size(), 3, "should have 3 items")

func test_spawn_entities_parallel_increases_total_count() -> void:
	var before = _main.get_spawned_count()
	_main.spawn_entities_parallel(["X", "Y"])
	assert_eq(_main.get_spawned_count(), before + 2, "total count should increase by 2")

func _on_entities_spawned(c: int, n: Array) -> void:
	_captured_count = c
	_captured_names = n

var _captured_count: int = -1
var _captured_names: Array = []

func test_spawn_entities_parallel_emits_count() -> void:
	_captured_count = -1
	_captured_names.clear()
	_main.entities_spawned.connect(_on_entities_spawned)
	var result = _main.spawn_entities_parallel(["A", "B", "C"])
	assert_eq(_captured_count, result.size(), "signal should report correct count")
	assert_eq(_captured_names.size(), result.size(), "signal should carry same number of names")

func test_spawn_entities_parallel_empty_array() -> void:
	var before = _main.get_spawned_count()
	_main.spawn_entities_parallel([])
	assert_eq(_main.get_spawned_count(), before, "count should not change for empty input")

# ── game state ────────────────────────────────────────────────────────────────

func test_get_game_state_returns_dict() -> void:
	var state = _main.get_game_state()
	assert_true(state is Dictionary, "get_game_state should return Dictionary")

func test_game_state_contains_service_ready() -> void:
	var state = _main.get_game_state()
	assert_true(state.has("service_ready"), "state should have service_ready field")

func test_game_state_contains_spawned_entities() -> void:
	var state = _main.get_game_state()
	assert_true(state.has("spawned_entities"), "state should have spawned_entities field")

func test_game_state_contains_spawned_count() -> void:
	var state = _main.get_game_state()
	assert_true(state.has("spawned_count"), "state should have spawned_count field")

func test_game_state_spawned_entities_is_array() -> void:
	var state = _main.get_game_state()
	assert_true(state["spawned_entities"] is Array, "spawned_entities should be Array")

func test_game_state_reflects_spawned_count() -> void:
	var before = _main.get_spawned_count()
	_main.spawn_entity("StateTest")
	_main.spawn_entity("StateTest2")
	var state = _main.get_game_state()
	assert_eq(state["spawned_count"], _main.get_spawned_count(),
		"state spawned_count should match get_spawned_count()")
	assert_eq(state["spawned_count"], before + 2,
		"state should reflect +2 spawned entities relative to before")

func test_game_state_includes_parallel_fields() -> void:
	var state = _main.get_game_state()
	assert_true(state.has("processing_parallel"), "state should have processing_parallel")
	assert_true(state.has("pending_spawn_count"), "state should have pending_spawn_count")

# ── request counter ──────────────────────────────────────────────────────────

func test_multiple_requests_increment_counter() -> void:
	var start = _main._request_counter
	_main.process_request("r1")
	_main.process_request("r2")
	_main.process_request("r3")
	assert_eq(_main._request_counter, start + 3, "counter should increment by 3")
