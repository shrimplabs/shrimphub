extends GutTest

## Test suite for project context packet functionality.
## Tests service initialization, primary request path, and game state reporting.

var _main: Node

func before_each() -> void:
	var main_script = load("res://main.gd")
	_main = main_script.new()
	add_child(_main)

func after_each() -> void:
	_main.free()

func test_service_initialized_signal_exists() -> void:
	assert_true(_main.has_signal("service_initialized"), "should have service_initialized signal")

func test_request_processed_signal_exists() -> void:
	assert_true(_main.has_signal("request_processed"), "should have request_processed signal")

func test_entities_spawned_signal_exists() -> void:
	assert_true(_main.has_signal("entities_spawned"), "should have entities_spawned signal")

func test_spawn_entity_returns_node() -> void:
	var entity = _main.spawn_entity("TestEntity")
	assert_not_null(entity, "spawn_entity should return Node")
	assert_eq(entity.name, "TestEntity", "entity should have correct name")
	entity.free()

func test_process_request_increments_counter() -> void:
	var initial_count = _main.get_game_state().get("request_counter", 0)
	var result = _main.process_request("/test/path")
	assert_true(result is Dictionary, "should return Dictionary")
	assert_true(result.has("request_id"), "should have request_id")
	assert_true(result.has("status"), "should have status")

func test_get_game_state_returns_dictionary() -> void:
	var state = _main.get_game_state()
	assert_true(state is Dictionary, "get_game_state should return Dictionary")
	assert_true(state.has("service_ready"), "state should have service_ready")
	assert_true(state.has("spawned_count"), "state should have spawned_count")
	assert_true(state.has("processing_parallel"), "state should have processing_parallel")

func test_spawn_entities_parallel_returns_count() -> void:
	var entities = _main.spawn_entities_parallel(["A", "B", "C"])
	assert_eq(entities.size(), 3, "should spawn 3 entities")
	var state = _main.get_game_state()
	assert_eq(state.get("spawned_count"), 3, "spawned count should be 3")

func test_service_processes_request_path() -> void:
	var result = _main.process_request("/api/v1/users")
	var path_result = result.get("result", "")
	assert_true(path_result.contains("api"), "result should contain path segments")
	assert_true(path_result.contains("v1"), "result should contain v1")
	assert_true(path_result.contains("users"), "result should contain users")

func test_spawned_entities_tracked_in_state() -> void:
	_main.spawn_entity("E1")
	_main.spawn_entity("E2")
	var state = _main.get_game_state()
	var spawned = state.get("spawned_entities", [])
	assert_true(spawned.has("E1"), "state should track E1")
	assert_true(spawned.has("E2"), "state should track E2")
