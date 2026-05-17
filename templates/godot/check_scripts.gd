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
