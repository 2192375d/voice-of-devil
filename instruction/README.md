# Player instructions

After the player is ready, call its manager on Godot's main thread:

```csharp
player.Instructions.WalkForward();
player.Instructions.Rotate(new Vector3(0, 90, 0));
player.Instructions.Stop();
```

Walking continues until stop, including when blocked by collisions. Rotation uses
relative degrees: positive Y turns right, negative Y turns left. X and Z must be
zero. Walking follows the current heading during a turn. The exported player
speeds default to 5 units/second and 90 degrees/second.

Requests return `InstructionRequestResult`. Repeated walking returns
`AlreadyRunning`; rotation while another rotation is active returns `Busy`.
Zero rotation completes immediately. Stop cancels both actions, clears horizontal
movement immediately, and leaves gravity running.

`ActiveInstructions` exposes status, elapsed time, and typed instruction progress.
`LastFinishedInstruction` retains the latest completion, including stop.
The player ticks the manager before applying movement, so callers must not tick
it separately. The manager has no gameplay queue; a future HTTP/MCP adapter must
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
uses item origins within `PickupReach` (default 2 units) in the forward hemisphere,
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
