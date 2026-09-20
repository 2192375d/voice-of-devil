extends SceneTree
var failures := 0
var checks := 0
var player
var controls

func _initialize():
    call_deferred("run")

func frames(count):
    for i in range(count):
        await physics_frame
        await process_frame

func check(value, description):
    checks += 1
    if not value:
        failures += 1
        push_error(description)

func box(parent, position, size):
    var body = StaticBody3D.new()
    var collision = CollisionShape3D.new()
    var shape = BoxShape3D.new()
    shape.size = size
    collision.shape = shape
    body.add_child(collision)
    body.position = position
    parent.add_child(body)
    return body

func walk(count):
    controls.get_node("WalkForward").pressed.emit()
    await frames(count)
    controls.get_node("Stop").pressed.emit()
    await frames(10)

func place(position):
    player.position = position
    player.rotation = Vector3.ZERO
    player.velocity = Vector3.ZERO
    await frames(30)

func run():
    var world = load("res://world/world.tscn").instantiate()
    root.add_child(world)
    player = world.get_node("Player")
    controls = world.get_node("CanvasLayer/InstructionControls")
    var plate = world.get_node("WorldObjects/PressurePlate")
    var door = world.get_node("WorldObjects/DoorLocked")
    await place(plate.global_position + Vector3(0, 1.1, 3))
    await walk(36)
    await frames(45)
    check(plate.IsPressed and player.is_on_floor(), "character walks onto actual pressure plate")
    check(door.IsOpen, "walking onto plate opens paired door")
    await walk(48)
    await frames(45)
    check(not plate.IsPressed and not door.IsOpen, "walking off plate closes door")

    box(world, Vector3(0, 9.5, 0), Vector3(20, 1, 20))
    var obstacle = box(world, Vector3(0, 10.15, -1), Vector3(3, 0.3, 2))
    await place(Vector3(0, 11.05, 2))
    await walk(36)
    print("Step landing: ", player.position, "; grounded: ", player.is_on_floor())
    check(player.position.z < -0.5 and player.position.y > 11.25, "climbs a 0.3-unit step")
    obstacle.queue_free()
    await frames(5)
    obstacle = box(world, Vector3(0, 10.5, -1), Vector3(3, 1, 2))
    await place(Vector3(0, 11.05, 2))
    await walk(48)
    check(player.position.z > 0.3 and player.position.y < 11.1, "cannot climb a tall obstacle")
    obstacle.queue_free()
    await frames(5)
    box(world, Vector3(0, 10.15, -1), Vector3(3, 0.3, 2))
    box(world, Vector3(0, 12.3, 0), Vector3(3, 0.4, 8))
    await place(Vector3(0, 11.05, 2))
    await walk(48)
    check(player.position.z > 0, "low ceiling blocks step-up")
    print("Step checks: %d checks, %d failures" % [checks, failures])
    quit(1 if failures else 0)
