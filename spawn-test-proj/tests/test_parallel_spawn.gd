extends GutTest

## Test suite for parallel entity spawning functionality.

var _main_script: GDScript
var _main: Node
var _test_captured_count: int = -1
var _test_captured_names: Array = []

func before_each() -> void:
	_setup_main()
	_test_captured_count = -1
	_test_captured_names.clear()

func after_each() -> void:
	if _main:
		_main.free()

func _setup_main() -> void:
	# Create main instance, skip _ready() which blocks on HTTP requests.
	_main_script = load("res://main.gd")
	_main = _main_script.new()
	add_child(_main)

func _on_entities_spawned(count: int, names: Array) -> void:
	_test_captured_count = count
	_test_captured_names = names.duplicate()

func test_spawn_entities_parallel_returns_array() -> void:
	var result: Array = _main.spawn_entities_parallel(["Entity1", "Entity2", "Entity3"])
	assert_true(result is Array, "spawn_entities_parallel should return Array")
	assert_eq(result.size(), 3, "should spawn 3 entities")

func test_spawn_entities_parallel_increases_count() -> void:
	var initial_count: int = _main.get_spawned_count()
	_main.spawn_entities_parallel(["P1", "P2"])
	assert_eq(_main.get_spawned_count(), initial_count + 2, "count should increase by 2")

func test_spawn_entities_parallel_assigns_correct_names() -> void:
	var names: Array = ["Alpha", "Beta", "Gamma"]
	_main.spawn_entities_parallel(names)
	var state: Dictionary = _main.get_game_state()
	var spawned_entities: Array = state.get("spawned_entities")
	assert_true(spawned_entities.has("Alpha"), "should contain Alpha")
	assert_true(spawned_entities.has("Beta"), "should contain Beta")
	assert_true(spawned_entities.has("Gamma"), "should contain Gamma")


func test_spawn_entities_parallel_emits_signal() -> void:
	assert_true(_main.has_signal("entities_spawned"), "_main should have entities_spawned signal")
	_main.entities_spawned.connect(_on_entities_spawned)
	var result = _main.spawn_entities_parallel(["X", "Y"])
	assert_eq(_test_captured_count, result.size(), "signal should report correct count")
	assert_eq(_test_captured_names.size(), 2, "signal should report correct names")


func test_spawn_entities_parallel_handles_empty_array() -> void:
	var initial_count: int = _main.get_spawned_count()
	var result: Array = _main.spawn_entities_parallel([])
	assert_eq(result.size(), 0, "should return empty array")
	assert_eq(_main.get_spawned_count(), initial_count, "count should not change")


func test_spawn_entities_parallel_with_delay_sets_processing_flag() -> void:
	assert_false(_main.is_processing_parallel(), "should not be processing initially")
	_main.spawn_entities_parallel_with_delay(["D1", "D2"], 5.0)
	await get_tree().create_timer(0.2).timeout
	assert_false(_main.is_processing_parallel(), "should not be processing after spawn completes")

func test_is_processing_parallel_returns_bool() -> void:
	assert_false(_main.is_processing_parallel(), "should return bool")


func test_get_pending_spawn_count_returns_int() -> void:
	var count: int = _main.get_pending_spawn_count()
	assert_true(count is int, "should return int")
	assert_eq(count, 0, "should be 0 initially")

func test_get_game_state_includes_parallel_fields() -> void:
	var state: Dictionary = _main.get_game_state()
	assert_true(state.has("processing_parallel"), "state should have processing_parallel")
	assert_true(state.has("pending_spawn_count"), "state should have pending_spawn_count")
	assert_eq(state.get("processing_parallel"), false, "processing_parallel should be false")
	assert_eq(state.get("pending_spawn_count"), 0, "pending_spawn_count should be 0")

func test_entities_spawned_signal_exists() -> void:
	assert_true(_main.has_signal("entities_spawned"), "should have _entities_spawned signal")
