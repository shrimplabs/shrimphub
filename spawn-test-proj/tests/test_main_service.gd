extends SceneTree

func _init():
	# Minimal test harness for main.gd
	var errors = []
	var passed = 0
	var failed = 0
	
	# Test 1: Load main script
	var main_script = load("res://main.gd")
	if main_script == null:
		errors.append("FAIL: Cannot load main.gd")
	else:
		passed += 1
		print("PASS: main.gd loaded")
		
		# Test 2: Create instance
		var main = main_script.new()
		if main.has_method("process_request"):
			passed += 1
			print("PASS: process_request method exists")
		else:
			errors.append("FAIL: process_request method missing")
			
		# Test 3: Test process_request
		if main.has_method("process_request"):
			var result = main.process_request("api/v1/test")
			if result.get("status") == "ok":
				passed += 1
				print("PASS: process_request returns ok status")
			else:
				errors.append("FAIL: process_request did not return ok")
			
		# Test 4: Test spawn_entity
		if main.has_method("spawn_entity"):
			var count_before = main.get_spawned_count()
			main.spawn_entity("TestEntity")
			if main.get_spawned_count() > count_before:
				passed += 1
				print("PASS: spawn_entity works")
			else:
				errors.append("FAIL: spawn_entity did not increase count")
		
		# Test 5: Test get_game_state
		if main.has_method("get_game_state"):
			var state = main.get_game_state()
			if state is Dictionary:
				passed += 1
				print("PASS: get_game_state returns Dictionary")
			else:
				errors.append("FAIL: get_game_state did not return Dictionary")
		
		main.free()
	
	print("\n--- Results ---")
	print("Passed: " + str(passed))
	print("Failed: " + str(errors.size()))
	for e in errors:
		print(e)
	
	if errors.size() > 0:
		quit(1)
	else:
		quit(0)
