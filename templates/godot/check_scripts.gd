extends SceneTree

func _initialize():
	var autoload_names: Array[String] = _read_autoloads()

	# Pass 1 (repeated 3x): load all scripts that declare class_name so their types
	# are globally registered before pass 2. Repeated because a class_name script
	# can depend on another class_name script (e.g. Grid uses Tetromino) — three
	# iterations handle chains up to three levels deep.
	for _i in range(3):
		_load_class_names("res://")

	# Pass 2: load every script and report any that still fail to compile.
	var errors: Array[String] = []
	_scan("res://", autoload_names, errors)

	# Pass 3: verify every declared [connection] in every .tscn actually binds.
	# Catches e.g. `to="Main"` (root's own name) instead of `to="."` (the
	# correct self-reference) -- Godot resolves relative NodePaths by looking
	# for a CHILD with that name, so `to="<RootName>"` silently fails to bind
	# with zero console output. A dead signal connection is invisible to
	# every other check in this file (scripts still compile fine) and to
	# QA agents that click via StateServer's press_button, which calls
	# emit_signal() directly and bypasses this exact failure mode.
	_check_connections("res://", errors)

	if errors.size() > 0:
		for e in errors: print("ERROR: " + e)
		quit(1)
	else:
		print("All scripts OK")
		quit(0)

func _read_autoloads() -> Array[String]:
	var names: Array[String] = []
	var f = FileAccess.open("res://project.godot", FileAccess.READ)
	if f == null: return names
	var in_autoload = false
	while not f.eof_reached():
		var line = f.get_line().strip_edges()
		if line == "[autoload]":
			in_autoload = true
		elif line.begins_with("[") and line.ends_with("]"):
			in_autoload = false
		elif in_autoload and "=" in line:
			names.append(line.split("=")[0].strip_edges())
	f.close()
	return names

func _load_class_names(path: String) -> void:
	var dir = DirAccess.open(path)
	if dir == null: return
	dir.list_dir_begin()
	var f = dir.get_next()
	while f != "":
		if dir.current_is_dir() and not f.begins_with(".") and f != "addons" and f != "test" and f != "tests":
			_load_class_names(path + f + "/")
		elif f.ends_with(".gd"):
			var full_path = path + f
			var src = FileAccess.get_file_as_string(full_path)
			if "class_name " in src:
				load(full_path)
		f = dir.get_next()

func _scan(path: String, autoload_names: Array[String], errors: Array[String]) -> void:
	var dir = DirAccess.open(path)
	if dir == null: return
	dir.list_dir_begin()
	var f = dir.get_next()
	while f != "":
		if dir.current_is_dir() and not f.begins_with(".") and f != "addons" and f != "test" and f != "tests":
			_scan(path + f + "/", autoload_names, errors)
		elif f.ends_with(".gd"):
			var full_path = path + f
			var s = load(full_path)
			if s == null:
				if not _references_autoload(full_path, autoload_names):
					errors.append("Failed to load: " + full_path)
		f = dir.get_next()

func _references_autoload(path: String, autoload_names: Array[String]) -> bool:
	if autoload_names.is_empty():
		return false
	var source = FileAccess.get_file_as_string(path)
	if source.is_empty():
		return false
	for aname in autoload_names:
		if aname in source:
			return true
	return false

func _check_connections(path: String, errors: Array[String]) -> void:
	var dir = DirAccess.open(path)
	if dir == null: return
	dir.list_dir_begin()
	var f = dir.get_next()
	while f != "":
		if dir.current_is_dir() and not f.begins_with(".") and f != "addons":
			_check_connections(path + f + "/", errors)
		elif f.ends_with(".tscn"):
			_check_scene_connections(path + f, errors)
		f = dir.get_next()

func _check_scene_connections(scene_path: String, errors: Array[String]) -> void:
	var raw := FileAccess.get_file_as_string(scene_path)
	if raw.is_empty():
		return
	var declared := _parse_connections(raw)
	if declared.is_empty():
		return

	var packed = load(scene_path)
	if packed == null or not (packed is PackedScene):
		# Load failure is already reported (or explained) by the script-compile
		# pass above -- don't double-report here.
		return

	var root = packed.instantiate()
	if root == null:
		return

	for conn in declared:
		var from_path: String = conn["from"]
		var from_node = root if from_path == "." else root.get_node_or_null(from_path)
		if from_node == null:
			errors.append("%s: connection declares from=\"%s\" but that node does not exist" % [scene_path, from_path])
			continue

		var bound := false
		for entry in from_node.get_signal_connection_list(conn["signal"]):
			var callable: Callable = entry["callable"]
			if callable.get_method() == conn["method"]:
				bound = true
				break

		if not bound:
			errors.append(
				"%s: [connection signal=\"%s\" from=\"%s\" to=\"%s\" method=\"%s\"] is declared but NOT bound at runtime -- if to=\"%s\" is the scene root's own name, use to=\".\" instead (relative NodePath resolution looks for a CHILD named \"%s\", not the node itself, so this silently drops the connection with no engine error)"
				% [scene_path, conn["signal"], from_path, conn["to"], conn["method"], conn["to"], conn["to"]]
			)

	root.free()

func _parse_connections(raw: String) -> Array[Dictionary]:
	var results: Array[Dictionary] = []
	var regex := RegEx.new()
	regex.compile("\\[connection signal=\"([^\"]+)\" from=\"([^\"]+)\" to=\"([^\"]+)\" method=\"([^\"]+)\"\\]")
	for m in regex.search_all(raw):
		results.append({
			"signal": m.get_string(1),
			"from": m.get_string(2),
			"to": m.get_string(3),
			"method": m.get_string(4),
		})
	return results
