extends GutTest

## Feature test: parallel-spawn-test-proj-0-1780273161
## Covers: SpawnService lifecycle + game state integration.

func before_each() -> void:
	assert_not_null(SpawnService, "SpawnService autoload must be registered")

# ─── Service lifecycle ────────────────────────────────────────────

func test_service_starts_and_reports_running() -> void:
	SpawnService.stop()
	assert_false(SpawnService.is_running(), "Should not be running before start")
	var err = SpawnService.start()
	assert_eq(err, OK, "start() should return OK")
	await get_tree().create_timer(2.0).timeout
	assert_true(SpawnService.is_running(), "Service should be running after start")
	assert_gt(SpawnService.get_pid(), 0, "PID should be positive")
	SpawnService.stop()

func test_service_stop_clears_pid() -> void:
	SpawnService.start()
	await get_tree().create_timer(2.0).timeout
	assert_gt(SpawnService.get_pid(), 0, "PID should be set")
	SpawnService.stop()
	assert_false(SpawnService.is_running(), "Should not be running after stop")

# ─── Game state ─────────────────────────────────────────

func test_main_game_state_structure() -> void:
	var main_script = load("res://main.gd")
	var inst = main_script.new()
	add_child(inst)
	var state = inst.get_game_state()
	assert_true(state is Dictionary, "game state must be Dictionary")
	assert_has(state.keys(), "service_ready", "state must have service_ready")
	assert_has(state.keys(), "spawned_count", "state must have spawned_count")
	assert_has(state.keys(), "processing_parallel", "state must have processing_parallel")
	inst.queue_free()

func test_parallel_spawn_updates_game_state() -> void:
	var main_script = load("res://main.gd")
	var inst = main_script.new()
	add_child(inst)
	var before = inst.get_spawned_count()
	var spawned = inst.spawn_entities_parallel(["P1", "P2", "P3"])
	assert_eq(spawned.size(), 3, "three entities spawned")
	var state = inst.get_game_state()
	assert_eq(state["spawned_count"], before + 3, "state reflects parallel spawn")
	assert_false(state["processing_parallel"], "parallel flag cleared after spawn")
	inst.queue_free()

func test_multiple_parallel_spawns_accumulate() -> void:
	var main_script = load("res://main.gd")
	var inst = main_script.new()
	add_child(inst)
	inst.spawn_entities_parallel(["A", "B"])
	inst.spawn_entities_parallel(["C"])
	var state = inst.get_game_state()
	assert_eq(state["spawned_count"], 3, "two waves accumulate to 3")
	assert_has(state["spawned_entities"], "A", "entity A in list")
	assert_has(state["spawned_entities"], "B", "entity B in list")
	assert_has(state["spawned_entities"], "C", "entity C in list")
	inst.queue_free()

func test_empty_parallel_spawn_returns_empty() -> void:
	var main_script = load("res://main.gd")
	var inst = main_script.new()
	add_child(inst)
	var result = inst.spawn_entities_parallel([])
	assert_eq(result.size(), 0, "empty array returns empty")
	assert_false(inst.is_processing_parallel(), "flag not set for empty")
	inst.queue_free()

# ─── Process request path ───────────────────────────────────

func test_process_request_formats_deep_path() -> void:
	var main_script = load("res://main.gd")
	var inst = main_script.new()
	add_child(inst)
	var r = inst.process_request("a/b/c/d/e")
	assert_eq(r["result"], "[a][b][c][d][e]", "deep path segments formatted")
	assert_eq(r["status"], "ok", "status is ok")
	inst.queue_free()
