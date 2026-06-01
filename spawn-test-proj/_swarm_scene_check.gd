extends SceneTree
func _init():
    var errors = []
    _scan("res://", errors)
    if errors.size() > 0:
        for e in errors: print("SCENE ERROR: " + e)
        quit(1)
    else: print("All scenes OK"); quit(0)
func _scan(path: String, errors: Array) -> void:
    var dir = DirAccess.open(path)
    if dir == null: return
    dir.list_dir_begin()
    var f = dir.get_next()
    while f != "":
        if dir.current_is_dir() and not f.begins_with(".") and f != "addons":
            _scan(path + f + "/", errors)
        elif f.ends_with(".tscn"):
            var s = load(path + f)
            if s == null: errors.append("Failed to load scene: " + path + f)
        f = dir.get_next()
