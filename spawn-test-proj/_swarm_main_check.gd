extends SceneTree
func _init():
    var packed = load("res://main.tscn")
    if packed == null: print("SCENE ERROR: Cannot load main scene"); quit(1)
    var instance = packed.instantiate()
    if instance == null: print("SCENE ERROR: Cannot instantiate main scene"); quit(1)
    instance.free(); print("Main scene OK"); quit(0)
