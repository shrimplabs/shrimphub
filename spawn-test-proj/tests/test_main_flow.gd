extends GutTest

## Feature test: primary request path end-to-end.
## Task: parallel-spawn-test-proj-0-1786193160

var _main: Node

func before_each() -> void:
	_main = load("res://main.gd").new()
	add_child(_main)

func after_each() -> void:
	_main.free()

func test_main_has_required_signals() -> void:
	assert_true(_main.has_signal("service_initialized"), "service_initialized signal")
	assert_true(_main.has_signal("request_processed"), "request_processed signal")
	assert_true(_main.has_signal("entities_spawned"), "entities_spawned signal")

func test_main_has_required_methods() -> void:
	assert_true(_main.has_method("process_request"), "process_request method")
	assert_true(_main.has_method("spawn_entity"), "spawn_entity method")
	assert_true(_main.has_method("spawn_entities_parallel"), "spawn_entities_parallel method")
	assert_true(_main.has_method("get_game_state"), "get_game_state method")

func test_get_game_state_complete() -> void:
	var state = _main.get_game_state()
	assert_true(state is Dictionary, "state is Dictionary")
	assert_true(state.has("service_ready"), "has service_ready")
	assert_true(state.has("request_counter"), "has request_counter")
	assert_true(state.has("spawned_count"), "has spawned_count")
	assert_true(state.has("spawned_entities"), "has spawned_entities")
	assert_true(state.has("processing_parallel"), "has processing_parallel")
	assert_true(state.has("pending_spawn_count"), "has pending_spawn_count")

func test_process_request_returns_complete_dict() -> void:
	var result = _main.process_request("api/v1/users")
	assert_true(result is Dictionary, "result is Dictionary")
	assert_true(result.has("request_id"), "has request_id")
	assert_true(result.has("path"), "has path")
	assert_true(result.has("segments"), "has segments")
	assert_true(result.has("result"), "has result")
	assert_true(result.has("status"), "has status")
	assert_eq(result["result"], "[api][v1][users]", "segments formatted")

func test_process_request_spawn_increments_counter() -> void:
	var before = _main._request_counter
	_main.process_request("path1")
	_main.process_request("path2")
	assert_eq(_main._request_counter, before + 2, "counter incremented by 2")

func test_spawn_entity_increases_count() -> void:
	var before = _main.get_spawned_count()
	_main.spawn_entity("EntityAlpha")
	assert_eq(_main.get_spawned_count(), before + 1, "count incremented")

func test_spawn_entities_parallel_creates_all() -> void:
	var before = _main.get_spawned_count()
	var result = _main.spawn_entities_parallel(["P1", "P2", "P3"])
	assert_eq(result.size(), 3, "3 entities in result")
	assert_eq(_main.get_spawned_count(), before + 3, "count incremented by 3")

func test_spawn_entities_parallel_empty_array() -> void:
	var before = _main.get_spawned_count()
	_main.spawn_entities_parallel([])
	assert_eq(_main.get_spawned_count(), before, "count unchanged for empty")
	assert_false(_main.is_processing_parallel(), "parallel flag not set")

func test_game_state_spawned_entities_is_array() -> void:
	var state = _main.get_game_state()
	assert_true(state["spawned_entities"] is Array, "spawned_entities is Array")

func _on_request_processed_signal(rid: int, result: String) -> void:
	_cap_request_ids.append(rid)
	_cap_request_results.append(result)

var _cap_request_ids: Array = []
var _cap_request_results: Array = []

func test_process_request_emits_signal() -> void:
	_cap_request_ids.clear()
	_cap_request_results.clear()
	_main.request_processed.connect(_on_request_processed_signal)
	_main.process_request("a/b")
	assert_eq(_cap_request_ids.size(), 1, "signal fired once")
	assert_eq(_cap_request_results[0], "[a][b]", "result captured")

func _on_entities_spawned_signal(c: int, names: Array) -> void:
	_cap_signal_count = c
	_cap_signal_names = names

var _cap_signal_count: int = -1
var _cap_signal_names: Array = []

func test_spawn_entities_parallel_emits_signal() -> void:
	_cap_signal_count = -1
	_cap_signal_names.clear()
	_main.entities_spawned.connect(_on_entities_spawned_signal)
	_main.spawn_entities_parallel(["X", "Y"])
	assert_eq(_cap_signal_count, 2, "count captured")
	assert_eq(_cap_signal_names.size(), 2, "names captured")
