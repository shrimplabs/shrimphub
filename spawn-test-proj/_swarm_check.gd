extends SceneTree

func _init():
	var packed: Resource = load("res://main.tscn")
	if packed == null:
		print("SCENE ERROR: Cannot load main.tscn")
		quit(1)
	var instance: Node = packed.instantiate()
	if instance == null:
		print("SCENE ERROR: Cannot instantiate main.tscn")
		quit(1)
	instance.free()
	print("Main scene OK")
	quit(0)
