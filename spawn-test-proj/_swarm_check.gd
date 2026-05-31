extends SceneTree

func _init():
	var errors = []
	# Load autoloads first so their class_names are registered
	var autoloads = [
		"res://scripts/spawn_service.gd",
		"res://scripts/service_manager.gd",
		"res://addons/gut/gut.gd",
	]
	for p in autoloads:
		var s = load(p)
		if s == null:
			errors.append("Autoload failed: " + p)
	# Scan remaining scripts, skip addons/ and tests/
	_scan("res://", errors, 0, autoloads)
	if errors.size() > 0:
		for e in errors:
			print("SCRIPT ERROR: " + e)
		quit(1)
	else:
		print("All scripts OK")
		quit(0)

func _scan(path: String, errors: Array, depth: int, skip: Array) -> void:
	var dir = DirAccess.open(path)
	if dir == null:
		return
	dir.list_dir_begin()
	var f = dir.get_next()
	while f != "":
		var full = path + f
		if dir.current_is_dir():
			if not f.begins_with(".") and f != "addons" and f != "tests":
				_scan(full + "/", errors, depth + 1, skip)
		elif f.ends_with(".gd"):
			if full not in skip:
				var s = load(full)
				if s == null:
					errors.append("Failed to load: " + full)
		f = dir.get_next()
