extends GutTest

## Test suite for parallel-spawn-test-proj-0-1780272655.
## Project context: Python profile, build mode, service + primary request path.
## Covers: SpawnService autoload lifecycle, HTTP endpoints, game state reporting.

var _spawn_service: Node

func before_each() -> void:
	_passed_tests = 0
	_total_tests = 0
	# SpawnService is an autoload -- access via its singleton name
	# Verify the autoload exists and is registered
	assert_not_null(SpawnService, "SpawnService autoload must be registered")

func after_each() -> void:
	_passed_tests = 0
	_total_tests = 0

var _passed_tests: int = 0
var _total_tests: int = 0

# ─── SpawnService lifecycle ───────────────────────────────────────────────────────

func test_spawn_service_is_registered_autoload() -> void:
	# Autoloads are accessible by their registered name as global singletons
	assert_not_null(SpawnService, "SpawnService must be accessible as a global singleton")

func test_spawn_service_has_start_method() -> void:
	assert_true(SpawnService.has_method("start"), "SpawnService should have start() method")

func test_spawn_service_has_stop_method() -> void:
	assert_true(SpawnService.has_method("stop"), "SpawnService should have stop() method")

func test_spawn_service_has_spawn_entity_method() -> void:
	assert_true(SpawnService.has_method("spawn_entity"), "SpawnService should have spawn_entity() method")

func test_spawn_service_initial_state_not_running() -> void:
	assert_false(SpawnService.is_running(), "SpawnService should not be running initially")

func test_spawn_service_initial_pid_is_invalid() -> void:
	assert_eq(SpawnService.get_pid(), -1, "SpawnService PID should be -1 when not started")

func test_spawn_service_has_service_started_signal() -> void:
	assert_true(SpawnService.has_signal("service_started"), "SpawnService should have service_started signal")

func test_spawn_service_has_service_stopped_signal() -> void:
	assert_true(SpawnService.has_signal("service_stopped"), "SpawnService should have service_stopped signal")

# ─── Main scene and game state ──────────────────────────────────────────

func test_main_script_loads() -> void:
	var main_script = load("res://main.gd")
	assert_not_null(main_script, "main.gd should load successfully")

func test_main_scene_loads() -> void:
	var main_scene = load("res://main.tscn")
	assert_not_null(main_scene, "main.tscn should load successfully")

func test_main_scene_instantiates() -> void:
	var main_scene = load("res://main.tscn")
	var inst = main_scene.instantiate()
	assert_not_null(inst, "main scene should instantiate")
	add_child(inst)
	await get_tree().process_frame
	inst.queue_free()

func test_main_has_get_game_state() -> void:
	var main_script = load("res://main.gd")
	var main_inst = main_script.new()
	add_child(main_inst)
	assert_true(main_inst.has_method("get_game_state"), "main should have get_game_state() method")
	main_inst.queue_free()

func test_main_get_game_state_returns_dict() -> void:
	var main_script = load("res://main.gd")
	var main_inst = main_script.new()
	add_child(main_inst)
	var state = main_inst.get_game_state()
	assert_true(state is Dictionary, "get_game_state should return Dictionary")
	assert_true(state.has("service_ready"), "state should have service_ready")
	assert_true(state.has("spawned_count"), "state should have spawned_count")
	assert_true(state.has("processing_parallel"), "state should have processing_parallel")
	main_inst.queue_free()

func test_main_has_process_request() -> void:
	var main_script = load("res://main.gd")
	var main_inst = main_script.new()
	add_child(main_inst)
	assert_true(main_inst.has_method("process_request"), "main should have process_request() method")
	main_inst.queue_free()

func test_main_has_spawn_entity() -> void:
	var main_script = load("res://main.gd")
	var main_inst = main_script.new()
	add_child(main_inst)
	assert_true(main_inst.has_method("spawn_entity"), "main should have spawn_entity() method")
	main_inst.queue_free()

func test_main_has_spawn_entities_parallel() -> void:
	var main_script = load("res://main.gd")
	var main_inst = main_script.new()
	add_child(main_inst)
	assert_true(main_inst.has_method("spawn_entities_parallel"), "main should have spawn_entities_parallel() method")
	main_inst.queue_free()

func test_main_process_request_returns_dict() -> void:
	var main_script = load("res://main.gd")
	var main_inst = main_script.new()
	add_child(main_inst)
	var result = main_inst.process_request("api/v1/test")
	assert_true(result is Dictionary, "process_request should return Dictionary")
	assert_true(result.has("request_id"), "result should have request_id")
	assert_true(result.has("status"), "result should have status")
	assert_true(result.has("result"), "result should have result")
	main_inst.queue_free()

func test_main_process_request_path_segments() -> void:
	var main_script = load("res://main.gd")
	var main_inst = main_script.new()
	add_child(main_inst)
	var result = main_inst.process_request("api/v1/users")
	assert_eq(result["result"], "[api][v1][users]", "path segments should be formatted")
	main_inst.queue_free()

func test_main_spawn_entity_increments_count() -> void:
	var main_script = load("res://main.gd")
	var main_inst = main_script.new()
	add_child(main_inst)
	var before = main_inst.get_spawned_count()
	var entity = main_inst.spawn_entity("TestEntity")
	assert_not_null(entity, "spawn_entity should return Node")
	assert_eq(entity.name, "TestEntity", "entity name should match")
	assert_eq(main_inst.get_spawned_count(), before + 1, "spawned count should increment")
	main_inst.queue_free()

func test_main_spawn_entities_parallel_returns_array() -> void:
	var main_script = load("res://main.gd")
	var main_inst = main_script.new()
	add_child(main_inst)
	var result = main_inst.spawn_entities_parallel(["A", "B", "C"])
	assert_true(result is Array, "spawn_entities_parallel should return Array")
	assert_eq(result.size(), 3, "should spawn 3 entities")
	main_inst.queue_free()

func test_main_game_state_reflects_spawn_count() -> void:
	var main_script = load("res://main.gd")
	var main_inst = main_script.new()
	add_child(main_inst)
	var before = main_inst.get_spawned_count()
	main_inst.spawn_entity("E1")
	main_inst.spawn_entity("E2")
	var state = main_inst.get_game_state()
	assert_eq(state["spawned_count"], before + 2, "state should reflect 2 more spawned entities")
	main_inst.queue_free()

# ─── Service Python script exists ──────────────────────────────────────────

func test_service_script_exists() -> void:
	var service_path = ProjectSettings.globalize_path("res://service.py")
	assert_true(FileAccess.file_exists(service_path), "service.py should exist at res://service.py")

func test_project_context_yaml_exists() -> void:
	var closure_path = ProjectSettings.globalize_path("res://PROJECT_CLOSURE.md")
	assert_true(FileAccess.file_exists(closure_path), "PROJECT_CLOSURE.md should exist")
