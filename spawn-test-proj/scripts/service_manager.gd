extends Node

## ServiceManager - Autoload singleton for managing game services.
## Handles service lifecycle, health checks, and centralized service state.

signal service_ready
signal service_error(message: String)

var _is_service_ready := false
var _service_uptime := 0.0

func _ready() -> void:
	_start_service()

func _process(delta: float) -> void:
	if _is_service_ready:
		_service_uptime += delta

func _start_service() -> void:
	_is_service_ready = true
	service_ready.emit()
	print("ServiceManager: Service started successfully")

func is_service_ready() -> bool:
	return _is_service_ready

func get_service_uptime() -> float:
	return _service_uptime

func get_game_state() -> Dictionary:
	return {
		"service_ready": _is_service_ready,
		"service_uptime": _service_uptime
	}
