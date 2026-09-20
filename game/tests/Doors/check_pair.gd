extends SceneTree

var checks := 0
var failures := 0
var orange
var blue

func _initialize():
    call_deferred("run")

func check(value, description):
    checks += 1
    if not value:
        failures += 1
        push_error(description)

func frames(count):
    for i in range(count):
        await physics_frame
        await process_frame
        if not orange.IsClosed and not blue.IsClosed:
            check(false, "both doors must never be away from their closed pose together")

func run():
    var world = load("res://world/world.tscn").instantiate()
    root.add_child(world)
    orange = world.get_node("WorldObjects/DoorOrange")
    blue = world.get_node("WorldObjects/DoorBlue")
    var blue_plate = world.get_node("WorldObjects/PressurePlate2")
    # Optional orange plate fixture; the authored room currently has only blue.
    var orange_plate = blue_plate.duplicate()
    orange_plate.TargetDoor = orange
    orange_plate.position = Vector3(0, 5, 0)
    world.add_child(orange_plate)
    var player = world.get_node("Player")
    player.set_physics_process(false)
    var away = player.global_position
    check(blue.get_node("Door_003").material_override.albedo_color.is_equal_approx(Color(0.05, 0.3, 1)), "blue door has blue albedo")
    check(orange.get_node("Door_003").material_override.albedo_color.is_equal_approx(Color(1, 0.43333334, 0)), "orange door has orange albedo")
    check(orange.IsOpen and blue.IsClosed, "orange starts open and blue closed immediately")
    check(blue_plate.TargetDoor == blue and orange_plate.TargetDoor == orange, "exported plate references are wired")
    player.global_position = blue_plate.global_position + Vector3(0, 1.2, 0)
    await frames(130)
    check(blue_plate.IsPressed and blue.IsOpen and orange.IsClosed, "blue plate switches pair")
    player.global_position = away
    await frames(45)
    check(blue.IsMoving and not blue.IsClosed and orange.IsClosed, "closing takes longer than opening and keeps orange closed")
    await frames(85)
    check(orange.IsOpen and blue.IsClosed, "release closes blue and restores orange")
    orange.TryInteract()
    blue.TryInteract()
    await frames(50)
    check(orange.IsOpen and blue.IsClosed, "manual interaction cannot override plates")
    player.global_position = orange_plate.global_position + Vector3(0, 1.2, 0)
    await frames(130)
    check(orange.IsOpen and blue.IsClosed, "orange plate switches back")
    # One held-down plate must not override a fresh press of the other.
    var weight = CharacterBody3D.new()
    var collision = CollisionShape3D.new()
    collision.shape = SphereShape3D.new()
    weight.add_child(collision)
    world.add_child(weight)
    weight.global_position = blue_plate.global_position + Vector3(0, 0.5, 0)
    await frames(130)
    check(orange_plate.IsPressed and blue_plate.IsPressed and blue.IsOpen, "latest press wins while both remain weighted")
    await frames(130)
    check(blue.IsOpen and orange.IsClosed, "sustained pressure does not oscillate")
    weight.queue_free()
    await frames(130)
    check(orange.IsOpen and blue.IsClosed, "removing blue weight restores still-weighted orange")
    player.global_position = away
    await frames(10)
    player.global_position = orange_plate.global_position + Vector3(0, 1.2, 0)
    await frames(10)
    player.global_position = blue_plate.global_position + Vector3(0, 1.2, 0)
    await frames(130)
    check(blue.IsOpen and orange.IsClosed, "new selection during motion settles safely")
    player.global_position = away
    await frames(130)
    check(orange.IsOpen and blue.IsClosed, "clearing both plates restores default after switching")
    player.global_position = blue_plate.global_position + Vector3(0, 1.2, 0)
    await frames(12)
    player.global_position = away
    await frames(130)
    check(orange.IsOpen and blue.IsClosed, "release mid-transition restores default safely")
    player.global_position = blue_plate.global_position + Vector3(0, 1.2, 0)
    await frames(130)
    blue_plate.queue_free()
    await frames(130)
    check(orange.IsOpen and blue.IsClosed, "removing active plate restores default")
    check(not world.get_node("WorldObjects/DoorLocked").IsOpen, "original weighted door remains independent")
    print("Door pair checks: %d checks, %d failures" % [checks, failures])
    quit(1 if failures else 0)
