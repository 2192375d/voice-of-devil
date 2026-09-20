extends SceneTree

var checks := 0
var failures := 0

func _initialize():
    call_deferred("run")

func check(value: bool, description: String):
    checks += 1
    if not value:
        failures += 1
        push_error(description)

func frames(count: int):
    for i in range(count):
        await physics_frame
        await process_frame

func run():
    var world = load("res://world/world.tscn").instantiate()
    root.add_child(world)
    var plate = world.get_node("WorldObjects/PressurePlate")
    var door = world.get_node("WorldObjects/DoorLocked")
    var ordinary = world.get_node("WorldObjects/Door")
    var item = world.get_node("WorldObjects/Pickable")
    var player = world.get_node("Player")
    player.set_physics_process(false)
    await frames(10)
    var closed = door.transform
    check(door.ActivationPlate == plate, "world door references its pressure plate")
    check(not plate.IsPressed and not door.IsOpen, "empty plate keeps door closed")
    door.TryInteract()
    await frames(45)
    check(door.transform == closed, "manual interaction cannot bypass empty plate")
    item.global_position = plate.global_position + Vector3(0, 2, 0)
    item.linear_velocity = Vector3.ZERO
    item.sleeping = false
    await frames(120)
    check(plate.IsPressed and door.IsOpen, "actual room object settles on plate and opens door")
    check(not ordinary.IsOpen, "unpaired door is unaffected")
    door.TryInteract()
    await frames(45)
    check(door.IsOpen, "manual interaction cannot close weighted door")
    # A second weight keeps the plate active when the original object is removed.
    var second = load("res://world/pickable.tscn").instantiate()
    second.scale = Vector3.ONE
    world.add_child(second)
    second.global_position = plate.global_position + Vector3(0.9, 2, 0)
    await frames(90)
    check(item.TryPickUp(player.get_node("HoldPoint")), "object can be picked up from plate")
    await frames(45)
    check(plate.IsPressed and door.IsOpen, "remaining weight holds door open")
    second.queue_free()
    await frames(85)
    check(not plate.IsPressed and door.transform == closed, "removing final weight closes door exactly")
    # Characters also supply weight; held items alone must not activate the sensor.
    player.global_position = plate.global_position + Vector3(0, 1.2, 0)
    await frames(85)
    check(plate.IsPressed and door.IsOpen, "character on plate opens door")
    player.global_position += Vector3(5, 0, 0)
    item.global_position = plate.global_position + Vector3(0, 0.3, 0)
    await frames(85)
    check(not plate.IsPressed and not door.IsOpen, "held object does not weigh down plate")
    # Reverse mid-swing, then clear a freed plate reference safely.
    player.global_position = plate.global_position + Vector3(0, 1.2, 0)
    await frames(12)
    check(door.IsMoving, "plate begins opening")
    player.global_position += Vector3(5, 0, 0)
    await frames(85)
    check(door.transform == closed, "removing weight during opening reverses to closed")
    player.global_position = plate.global_position + Vector3(0, 1.2, 0)
    await frames(85)
    plate.queue_free()
    await frames(85)
    check(door.transform == closed, "missing plate fails closed")
    print("Pressure plate world checks: %d checks, %d failures" % [checks, failures])
    quit(1 if failures else 0)
