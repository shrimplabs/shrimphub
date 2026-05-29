extends GutTest

var _main_script: GDScript
var _main: Node

func before_each() -> void:
	_main_script = load("res://main.gd")
	_main = _main_script.new()
	add_child(_main)

func after_each() -> void:
	_main.free()

func test_process_request_returns_ok_status() -> void:
	var result = _main.process_request("api/v1/test")
	assert_eq(result.get("status"), "ok", "process_request should return ok status")
	assert_true(result.has("request_id"), "result should have request_id")
	assert_true(result.has("path"), "result should have path")

func test_process_request_path_segmentation() -> void:
	var result = _main.process_request("api/v1/users")
	assert_eq(result.get("result"), "[api][v1][users]", "path should be segmented")

func test_process_request_empty_path() -> void:
	var result = _main.process_request("")
	assert_eq(result.get("result"), "[root]", "empty path should return [root]")

func test_spawn_entity_increases_count() -> void:
	var count_before = _main.get_spawned_count()
	_main.spawn_entity("TestEntity")
	assert_gt(_main.get_spawned_count(), count_before, "spawn_entity should increase count")

func test_spawn_entity_assigns_correct_name() -> void:
	var entity = _main.spawn_entity("MyEntity")
	assert_eq(entity.name, "MyEntity", "spawned entity should have correct name")

func test_get_game_state_returns_dictionary() -> void:
	var state = _main.get_game_state()
	assert_true(state is Dictionary, "get_game_state should return Dictionary")
	assert_true(state.has("service_ready"), "state should have service_ready")
	assert_true(state.has("request_counter"), "state should have request_counter")
	assert_true(state.has("spawned_count"), "state should have spawned_count")

func test_get_game_state_reflects_spawned_entities() -> void:
	var state_before = _main.get_game_state()
	var initial_count = state_before.get("spawned_count")
	_main.spawn_entity("Entity1")
	_main.spawn_entity("Entity2")
	var state = _main.get_game_state()
	assert_eq(state.get("spawned_count"), initial_count + 2, "spawned_count should increase by 2")
