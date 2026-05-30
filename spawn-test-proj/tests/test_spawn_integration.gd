extends GutTest
## Integration tests for the spawn feature.
## Tests the primary request path: service init → process_request → spawn_entity.

var _main: Node

func before_each() -> void:
	var script: GDScript = load("res://main.gd")
	_main = script.new()
	add_child(_main)

func after_each() -> void:
	_main.free()

func test_service_initialized_signal_is_defined() -> void:
	assert_true(_main.has_signal("service_initialized"), "Should have service_initialized signal")

func test_request_processed_signal_is_defined() -> void:
	assert_true(_main.has_signal("request_processed"), "Should have request_processed signal")

func test_multiple_entities_can_be_spawned() -> void:
	var initial: int = _main.get_spawned_count()
	_main.spawn_entity("Entity_A")
	_main.spawn_entity("Entity_B")
	_main.spawn_entity("Entity_C")
	assert_eq(_main.get_spawned_count(), initial + 3, "Three entities should be spawned")

func test_spawned_entities_have_unique_names() -> void:
	var e1: Node = _main.spawn_entity("Alpha")
	var e2: Node = _main.spawn_entity("Beta")
	assert_ne(e1.name, e2.name, "Entities should have unique names")

func test_process_request_increments_counter() -> void:
	var state_before: Dictionary = _main.get_game_state()
	var before: int = state_before["request_counter"]
	_main.process_request("test/path")
	var state_after: Dictionary = _main.get_game_state()
	assert_eq(state_after["request_counter"], before + 1, "Counter should increment")

func test_process_request_returns_result_string() -> void:
	var result: Dictionary = _main.process_request("foo/bar/baz")
	assert_eq(result.get("result"), "[foo][bar][baz]", "Path should be segmented into brackets")

func test_process_request_returns_request_id() -> void:
	var result: Dictionary = _main.process_request("test")
	assert_true(result.has("request_id"), "Result should contain request_id")
	assert_true(result["request_id"] is int, "request_id should be an integer")

func test_game_state_contains_all_required_fields() -> void:
	var state: Dictionary = _main.get_game_state()
	assert_true(state.has("service_ready"), "state must have service_ready")
	assert_true(state.has("request_counter"), "state must have request_counter")
	assert_true(state.has("spawned_entities"), "state must have spawned_entities array")
	assert_true(state.has("spawned_count"), "state must have spawned_count")
	assert_true(state["spawned_entities"] is Array, "spawned_entities should be an Array")

func test_game_state_spawned_entities_list_matches_count() -> void:
	_main.spawn_entity("X")
	_main.spawn_entity("Y")
	var state: Dictionary = _main.get_game_state()
	assert_eq(state["spawned_count"], state["spawned_entities"].size(), "Count should match array length")

func test_game_state_service_ready_is_true() -> void:
	var state: Dictionary = _main.get_game_state()
	assert_eq(state["service_ready"], true, "service_ready should be true after init")
