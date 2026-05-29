extends GutTest

## Test suite for ServiceManager autoload singleton.

func test_service_manager_exists() -> void:
	assert_not_null(ServiceManager, "ServiceManager autoload should exist")

func test_service_ready_returns_true() -> void:
	assert_true(ServiceManager.is_service_ready(), "Service should be ready after init")

func test_service_uptime_is_nonnegative() -> void:
	var uptime = ServiceManager.get_service_uptime()
	assert_gte(uptime, 0.0, "Uptime should be non-negative")

func test_get_game_state_returns_dict() -> void:
	var state = ServiceManager.get_game_state()
	assert_true(state is Dictionary, "get_game_state should return Dictionary")
	assert_true(state.has("service_ready"), "State should have service_ready key")
	assert_true(state.has("service_uptime"), "State should have service_uptime key")
	assert_eq(state["service_ready"], true, "service_ready should be true")

func test_service_ready_signal_exists() -> void:
	assert_true(ServiceManager.has_signal("service_ready"), "Should have service_ready signal")

func test_service_error_signal_exists() -> void:
	assert_true(ServiceManager.has_signal("service_error"), "Should have service_error signal")
