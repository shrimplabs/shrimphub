extends GutTest

# Smoke test installed by the swarm bootstrap: the QA harness autoloads must
# be live, or QA / harness_qa / the playthrough bot cannot read game state.

func test_state_server_autoload_is_loaded():
	assert_not_null(get_node_or_null("/root/StateServer"), "StateServer autoload missing")

func test_test_harness_autoload_is_loaded():
	assert_not_null(get_node_or_null("/root/TestHarness"), "TestHarness autoload missing")
