extends SceneTree

func _initialize():
    call_deferred("run")

func settle():
    for i in range(5):
        await physics_frame
        await process_frame

func run():
    var world = load("res://world/world.tscn").instantiate()
    root.add_child(world)
    var player = world.get_node("Player")
    var door = world.get_node("WorldObjects/Door")
    var button = world.get_node("CanvasLayer/InstructionControls/Interact")
    player.set_physics_process(false)
    await settle()
    var center = door.get_node("CollisionShape3D").global_position
    player.position = Vector3(center.x, 1.2, center.z - 1.5)
    player.rotation = Vector3(0, PI, 0)
    player.set_physics_process(true)
    for i in range(120):
        await physics_frame
    player.set_physics_process(false)
    print("Standing position: ", player.position, "; on floor: ", player.is_on_floor())
    await settle()
    button.pressed.emit()
    await settle()
    var opened = door.IsMoving
    print("Ground-level button started opening: ", opened)
    for i in range(85):
        await physics_frame
    opened = opened and door.IsOpen
    print("Door fully opened: ", door.IsOpen)
    # Follow the open collider to test closing without relying on the swing direction.
    center = door.get_node("CollisionShape3D").global_position
    player.position = Vector3(center.x, 1.000937, center.z - 1.5)
    await settle()
    button.pressed.emit()
    await settle()
    var closing = door.IsMoving
    for i in range(85):
        await physics_frame
    print("Button closing started: ", closing, "; fully closed: ", not door.IsOpen)
    quit(0 if opened and closing and not door.IsOpen else 1)
