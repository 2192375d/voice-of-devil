extends SceneTree
# Run with VOD_API_BIND=127.0.0.1 VOD_API_PORT=13091 godot-mono --headless \
# --path game --script res://tests/Doors/check.gd --log-file /tmp/door-check.log

var failures := 0
var checks := 0
var http: HTTPRequest
var world: Node3D
var player: CharacterBody3D
var door: AnimatableBody3D

func _initialize():
    call_deferred("run")

func check(value: bool, description: String):
    checks += 1
    if not value:
        failures += 1
        push_error(description)

func settle():
    await physics_frame
    await process_frame
    await physics_frame

func command(action: String) -> String:
    var port = OS.get_environment("VOD_API_PORT")
    var error = http.request("http://127.0.0.1:%s/api/v1/commands" % port,
        ["Content-Type: application/json"], HTTPClient.METHOD_POST,
        JSON.stringify({"command": action}))
    if error != OK:
        check(false, "HTTP request failed")
        return "http_error"
    var reply = await http.request_completed
    var body = JSON.parse_string(reply[3].get_string_from_utf8())
    return body.result.status

func finish_motion():
    for i in range(85):
        await physics_frame
    await settle()

func run():
    http = HTTPRequest.new()
    http.timeout = 5
    root.add_child(http)
    world = Node3D.new()
    root.add_child(world)
    player = load("res://world/AI/player.tscn").instantiate()
    world.add_child(player)
    player.set_physics_process(false)
    door = load("res://world/Door.tscn").instantiate()
    door.name = "DoorA"
    door.position = Vector3(0, 0, -1.5)
    door.rotation.y = PI / 2
    world.add_child(door)
    await settle()
    var closed = door.transform
    var hinge = door.global_transform * door.HingePosition
    var initial_center = door.get_node("CollisionShape3D").global_position
    check(await command("interact") == "opening", "API starts opening nearby door")
    check(await command("interact") == "busy", "moving door rejects another interaction")
    await command("stop")
    await finish_motion()
    check(door.IsOpen and not door.IsMoving, "stop does not cancel door motion")
    check((door.global_transform * door.HingePosition).is_equal_approx(hinge), "hinge stays fixed")
    check(not door.get_node("CollisionShape3D").global_position.is_equal_approx(initial_center), "collider follows hinge")
    var center = door.get_node("CollisionShape3D").global_position
    var normal = door.global_basis.x.normalized()
    var hit = world.get_world_3d().direct_space_state.intersect_ray(
        PhysicsRayQueryParameters3D.create(center + normal, center - normal))
    check(not hit.is_empty() and hit.collider == door, "physics collision follows open door")
    check(await command("interact") == "closing", "API starts closing")
    await finish_motion()
    check(not door.IsOpen and door.transform.is_equal_approx(closed), "closing restores exact transform")

    player.position.z = 5
    await settle()
    check(await command("interact") == "no_interactable_in_reach", "reject distant door")
    player.position = Vector3.ZERO
    player.rotation.y = PI
    await settle()
    check(await command("interact") == "no_interactable_in_reach", "reject door behind player")
    player.rotation.y = 0
    var wall = StaticBody3D.new()
    var wall_shape = CollisionShape3D.new()
    var box = BoxShape3D.new()
    box.size = Vector3(3, 3, 0.1)
    wall_shape.shape = box
    wall.add_child(wall_shape)
    wall.position = Vector3(0, 1, -0.75)
    world.add_child(wall)
    await settle()
    check(await command("interact") == "no_interactable_in_reach", "reject obstructed door")
    wall.queue_free()
    await settle()
    check(await command("walk_forward") == "started", "walking starts")
    check(await command("interact") == "busy", "movement prevents interaction")
    await command("stop")

    # Separate scaled and rotated instance: motion must preserve its authored hinge and scale.
    var scaled = load("res://world/Door.tscn").instantiate()
    scaled.position = Vector3(20, 0, 0)
    scaled.rotation.y = PI / 2
    scaled.scale = Vector3.ONE * 5
    world.add_child(scaled)
    var scaled_closed = scaled.transform
    var scaled_hinge = scaled.global_transform * scaled.HingePosition
    for cycle in range(3):
        scaled.TryInteract()
        await finish_motion()
        check((scaled.global_transform * scaled.HingePosition).is_equal_approx(scaled_hinge), "scaled hinge fixed")
        check(scaled.basis.get_scale().is_equal_approx(Vector3.ONE * 5), "instance scale preserved")
        scaled.TryInteract()
        await finish_motion()
        check(scaled.transform == scaled_closed, "repeated cycles have no drift")

    player.position = Vector3(20, 1, -1.5)
    player.rotation.y = PI
    await settle()
    check(await command("interact") == "opening", "ground-level API reaches scaled door surface")
    await finish_motion()
    var target = scaled.GetInteractionPosition(player.get_node("PickupOrigin").global_position)
    player.position = Vector3(target.x, 1, target.z - 1)
    await settle()
    check(await command("interact") == "closing", "ground-level API closes scaled rotated door")
    await finish_motion()
    player.position = Vector3.ZERO
    player.rotation.y = 0

    # Exact-distance candidates on either side of the center line have clear sight lines.
    door.rotation = Vector3.ZERO
    door.position = Vector3(-0.7, 0, -1.5)
    var other = load("res://world/Door.tscn").instantiate()
    other.name = "DoorB"
    other.rotation.y = 0
    other.position = Vector3(0.7, 0, -1.5)
    world.add_child(other)
    # Compensate the collider's local offset so center distances tie exactly.
    door.position -= door.basis * door.get_node("CollisionShape3D").position
    other.position -= other.basis * other.get_node("CollisionShape3D").position
    await settle()
    check(await command("interact") == "opening", "multiple candidates accepted")
    check(door.IsMoving and not other.IsMoving, "scene path breaks equal-distance ties")
    print("Door physics/API checks: %d checks, %d failures" % [checks, failures])
    quit(1 if failures else 0)
