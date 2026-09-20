# Player instructions

After the player is ready, call its manager on Godot's main thread:

```csharp
player.Instructions.WalkForward(5); // meters
player.Instructions.Rotate(new Vector3(0, 90, 0));
player.Instructions.Stop();
```

Walking requires a positive finite distance in meters (one world unit = one meter).
Its physics timer lasts `meters / Speed`, capturing speed when requested, and stops
automatically even if blocked. Collisions can reduce actual distance traveled. The
final tick is scaled to avoid overshooting. The dev button requests 5 meters. Rotation uses
relative degrees: positive Y turns right, negative Y turns left. X and Z must be
zero. Walking follows the current heading during a turn. The exported player
speeds default to 5 units/second and 90 degrees/second.

Requests return `InstructionRequestResult`. Repeated walking returns
`AlreadyRunning`; rotation while another rotation is active returns `Busy`.
Zero rotation completes immediately. Stop cancels both actions, clears horizontal
movement immediately, and leaves gravity running.

Walking and rotation both derive from `InstructionSustained`. Rotation inherits
through `Instruction<Vector3>` and runs across physics ticks until its requested
angle is reached or stop cancels it; walking inherits through `Instruction<double>`
and runs until its timer expires or stop cancels it.

`ActiveInstructions` exposes status, elapsed time, and typed instruction progress.
`LastFinishedInstruction` retains the latest completion, including stop.
The player ticks the manager before applying movement, so callers must not tick
it separately. The manager has no gameplay queue; the HTTP game API must
marshal requests to the main thread through its own inbox.

## Picking up items

Call `player.Instructions.GrabItem()` on the main physics tick. It returns `Busy`
while walking or rotating, `HandsFull` if an item is already held, `NoItemInReach`
when no eligible item is found, or `PickedUp` on success. Completed attempts are
available through `LastFinishedInstruction` as an `InstructionGrabItem` with a
`Result`. The dev UI's **Pick up** button runs this on the next physics tick and
prints the result to Output; it does not wait for movement to finish.

Both player scenes include `PickupOrigin` and `HoldPoint` markers. Items must use
the `Pickable` script on a `RigidBody3D` with a collision shape and mesh. Selection
uses item origins within `PickupReach` (default 5 units) in the forward hemisphere,
ordered by distance and then scene path. A ray from `PickupOrigin` rejects items
behind other physics bodies; `PickupObstacleMask` controls which layers block it.
The held item attaches to `HoldPoint` and is exposed by `Player.HeldItem`.
Stop cancels movement but does not release the held item.

## Dropping items

Call `player.Instructions.DropItem()` on the main physics tick, or use the **Drop**
dev button. It returns `Busy` during movement, `HandsEmpty` with no held item,
`DropBlocked` if release is obstructed, or `Dropped` on success. It releases at the
current hand position with no throw velocity, restoring the item's original
parent, collision settings, and freeze state. Ordinary unfrozen items fall under
gravity. A ray checks for walls between the player and item, and every enabled
item collision shape is checked for overlap, including with the player. Configure
`DropObstacleMask` to choose blocking layers. Blocked drops keep the item held.
The latest attempt is recorded as an `InstructionDropItem` with its result.

Run the standalone behavioral checks with:

```sh
dotnet run --project tests/Instructions/Instructions.csproj
```

If only a newer .NET runtime is installed, prefix that command with
`DOTNET_ROLL_FORWARD=Major`. These checks use a fake player; collision response,
gravity, and visual turning still require verification inside Godot.

## Door interaction

Send `{"command":"interact"}` to the game API. `InstructionManager.Interact()`
runs on the physics tick and requires idle movement. It targets the nearest door
collider point within `InteractionReach` (5 world units), in the forward
hemisphere and unobstructed under `InteractionObstacleMask`. Scene path breaks
equal-distance ties. The player and its held item are excluded from visibility
checks. No visible candidate returns `no_interactable_in_reach`.

The door scene owns its 90-degree, 0.6-second hinge motion and moving collision.
`opening` and `closing` mean motion has started; the immediate instruction is
complete, while the door continues moving. A moving door returns `busy`.
Stop affects player instructions only. Held items do not prevent interaction.
Angle, duration, and local hinge position are configurable on the door scene.
Doors start closed and may push bodies in their path. The development UI’s
**Interact** button invokes the same instruction on the next physics tick.
Reach and visibility use the closest point on the rotated, scaled box collider,
so tall doors remain reachable from ground level.

Run the door physics and live API checks after building the game:

```sh
VOD_API_BIND=127.0.0.1 VOD_API_PORT=13091 godot-mono --headless --path . --script res://tests/Doors/check.gd --log-file /tmp/door-check.log
```

### Pressure plate doors

`PressurePlate` is a fixed body with a shallow `Sensor` Area3D above its surface.
Characters and unheld rigid bodies count as weight; static scenery and held items
do not. Any remaining weight keeps the plate active.

Assign a plate's exported `TargetDoor` in the Inspector. For an ordinary door this
enables `PlateControlled` and binds its `ActivationPlate` automatically. Existing
door-side `ActivationPlate` assignments remain supported. The second room's
`PressurePlate` targets `DoorLocked`.
Explicit node references keep pairs local without global group names. An unassigned
or removed plate keeps the controlled door closed. Regular doors still toggle manually.

Plate-controlled doors open automatically while weighted and close when cleared,
reversing smoothly if the weight changes mid-swing. Manual/API interaction returns
`plate_controlled` and cannot override the plate. Closing doors may push bodies.

Run `res://tests/Doors/check_plate.gd` with Godot headless to check the actual room's
object, character, multiple weights, pickup/removal, reversal, and missing-plate behavior.

### Orange/blue door pairs

Add a `DoorPair` node and assign its exported `OrangeDoor` and `BlueDoor` references
to two distinct doors. Each door must belong to only one controller. Orange starts
fully open and blue fully closed. Assign each plate's `TargetDoor` to the door it
should select. The world connects `PressurePlate2` to blue; orange is the unweighted default.

A fresh press selects that door while its plate stays weighted. Releasing it selects
the other weighted plate, or restores orange open / blue closed if neither is weighted.
When both plates are weighted, the latest press wins. Sustained weight does not
repeatedly select a door. If both
plates are newly pressed in the same physics tick, the last plate processed wins
(orange in the current scene). The controller closes the other door completely
before opening the selected one, including when selection changes mid-animation.
Doors open in `MotionDuration` (0.6 seconds) and close in `ClosingDuration` (1.2 seconds),
both exported in the Inspector. Door colors are authored in material `albedo_color` fields.
Manual interaction returns `plate_controlled`. During a swap both doors may be closed,
but they are never open together. Separate controllers keep separate pairs independent.

Run `res://tests/Doors/check_pair.gd` with Godot headless to verify the world wiring,
initial poses, plate selection, release/reset, simultaneous weight, and animation interlock.

### Small steps

While walking on the floor, the player can automatically step up to `MaxStepHeight`
(default 0.35 world units). Full-body sweeps check overhead clearance, the forward
path, and a walkable landing. The capsule-radius probe finds the top of a step;
normal walking still determines horizontal speed. This lets the character walk
onto the pressure plate, while taller walls and low ceilings block progress.
Run `res://tests/Doors/check_steps.gd` for the world plate and obstacle checks.
