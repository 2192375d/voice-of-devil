using Godot;
using System.Collections.Generic;

public partial class Player : CharacterBody3D, IInstructionTarget
{
	[Export] public float Speed = 5.0f;
	[Export] public float RotationSpeed = 90.0f;
	[Export(PropertyHint.Range, "0,1,0.05")] public float MaxStepHeight = 0.35f;
	[Export(PropertyHint.Range, "0.1,10,0.1")] public float PickupReach = 5.0f;
	[Export(PropertyHint.Layers3DPhysics)] public uint PickupObstacleMask = uint.MaxValue;
	[Export(PropertyHint.Layers3DPhysics)] public uint DropObstacleMask = uint.MaxValue;

	[Export(PropertyHint.Range, "0.1,10,0.1")] public float InteractionReach = 5.0f;
	[Export(PropertyHint.Layers3DPhysics)] public uint InteractionObstacleMask = uint.MaxValue;

	public Marker3D HoldPoint { get; private set; }
	private Marker3D pickupOrigin;
	private Pickable heldItem;
	public Pickable HeldItem => IsInstanceValid(heldItem) && !heldItem.IsQueuedForDeletion()
		&& heldItem.IsHeld && heldItem.HoldPoint == HoldPoint ? heldItem : null;

	public InstructionManager Instructions { get; private set; }
	double IInstructionTarget.RotationSpeedDegrees => RotationSpeed;
	private bool walkingCommanded;

	private Cache cache;

	public override void _Ready()
	{
		base._Ready();
		HoldPoint = GetNode<Marker3D>("HoldPoint");
		pickupOrigin = GetNode<Marker3D>("PickupOrigin");
		Instructions = new InstructionManager(this);
		cache = GetNode<Cache>("/root/Cache");
		cache.Player = this;
	}

	public override void _PhysicsProcess(double delta)
	{
		Instructions.PhysicsUpdate(delta);
		Vector3 velocity = Velocity;
		Vector3 forward = -GlobalBasis.Z;
		forward.Y = 0;
		Vector3 walking = walkingCommanded ? forward.Normalized() * Speed : Vector3.Zero;
		velocity.X = walking.X;
		velocity.Z = walking.Z;
		velocity += GetGravity() * (float)delta;

		if (TryStepUp(walking * (float)delta))
			velocity.Y = 0;
		Velocity = velocity;
		MoveAndSlide();
		ApplyFloorSnap();
	}

	private bool TryStepUp(Vector3 motion)
	{
		if (!IsOnFloor() || Velocity.Y > 0 || motion.IsZeroApprox()
			|| !float.IsFinite(MaxStepHeight) || MaxStepHeight <= 0)
			return false;

		Transform3D start = GlobalTransform;
		if (!TestMove(start, motion))
			return false;
		// Sweep the full character up, across, and down: never step through a ceiling or wall.
		Vector3 lift = Vector3.Up * MaxStepHeight;
		if (TestMove(start, lift))
			return false;
		Transform3D raised = start;
		raised.Origin += lift;
		// Look past the capsule's rounded edge to find the tread, not its vertical riser.
		float radius = GetNode<CollisionShape3D>("CollisionShape3D").Shape is CapsuleShape3D capsule
			? capsule.Radius * Mathf.Max(GlobalBasis.X.Length(), GlobalBasis.Z.Length()) : 0;
		Vector3 probe = motion.Normalized() * Mathf.Max(motion.Length(), radius + SafeMargin);
		if (TestMove(raised, probe))
			return false;
		raised.Origin += probe;
		var landing = new KinematicCollision3D();
		if (!TestMove(raised, -lift, landing)
			|| landing.GetNormal().Dot(Vector3.Up) < Mathf.Cos(FloorMaxAngle))
			return false;
		float rise = MaxStepHeight + landing.GetTravel().Y;
		if (rise <= SafeMargin || rise > MaxStepHeight)
			return false;
		// Horizontal travel is still performed by MoveAndSlide below.
		GlobalPosition += Vector3.Up * rise;
		return true;
	}

	void IInstructionTarget.CommandWalkForward() => walkingCommanded = true;

	void IInstructionTarget.ApplyRightTurnDegrees(double degrees)
	{
		// Godot's forward is -Z; negative Y rotation turns right.
		RotateY((float)(-degrees * System.Math.PI / 180.0));
	}

	void IInstructionTarget.ClearCommandedMovement()
	{
		walkingCommanded = false;
		Velocity = new Vector3(0, Velocity.Y, 0);
	}

	InstructionRequestResult IInstructionTarget.TryInteract()
	{
		if (!float.IsFinite(InteractionReach) || InteractionReach <= 0)
			return InstructionRequestResult.InvalidArguments;

		Vector3 origin = pickupOrigin.GlobalPosition;
		Vector3 forward = -GlobalBasis.Z.Normalized();
		var candidates = new List<(Door Door, Vector3 Position)>();
		foreach (Node node in GetTree().GetNodesInGroup(Door.GroupName))
		{
			if (node is not Door door || door.IsQueuedForDeletion() || !door.IsInsideTree())
				continue;
			Vector3 position = door.GetInteractionPosition(origin);
			Vector3 offset = position - origin;
			if (offset.LengthSquared() <= InteractionReach * InteractionReach && forward.Dot(offset) >= 0)
				candidates.Add((door, position));
		}
		candidates.Sort((a, b) =>
		{
			int order = a.Position.DistanceSquaredTo(origin)
				.CompareTo(b.Position.DistanceSquaredTo(origin));
			return order != 0 ? order : string.CompareOrdinal(a.Door.GetPath().ToString(), b.Door.GetPath().ToString());
		});
		var excluded = new Godot.Collections.Array<Rid> { GetRid() };
		if (HeldItem is Pickable held)
			excluded.Add(held.GetRid());
		foreach (var candidate in candidates)
		{
			Door door = candidate.Door;
			var ray = PhysicsRayQueryParameters3D.Create(origin, candidate.Position,
				InteractionObstacleMask, excluded);
			ray.HitFromInside = true;
			var hit = GetWorld3D().DirectSpaceState.IntersectRay(ray);
			if (hit.Count > 0 && hit["collider"].AsGodotObject() != door)
				continue;
			return door.TryInteract();
		}
		return InstructionRequestResult.NoInteractableInReach;
	}

	InstructionRequestResult IInstructionTarget.TryDropItem()
	{
		Pickable item = HeldItem;
		if (item == null)
			return InstructionRequestResult.HandsEmpty;

		Transform3D dropTransform = item.GlobalTransform;
		// A held item can pass through walls while collisions are suspended. Do not
		// let it be released on the other side, even if its destination is clear.
		var ray = PhysicsRayQueryParameters3D.Create(pickupOrigin.GlobalPosition,
			dropTransform.Origin, DropObstacleMask,
			new Godot.Collections.Array<Rid> { GetRid(), item.GetRid() });
		ray.HitFromInside = true;
		if (GetWorld3D().DirectSpaceState.IntersectRay(ray).Count > 0
			|| !item.IsDropSpaceClear(dropTransform, DropObstacleMask))
			return InstructionRequestResult.DropBlocked;

		if (!item.TryDrop(dropTransform))
			return InstructionRequestResult.DropBlocked;

		heldItem = null;
		return InstructionRequestResult.Dropped;
	}

	InstructionRequestResult IInstructionTarget.TryGrabItem()
	{
		if (HeldItem != null)
			return InstructionRequestResult.HandsFull;
		if (!float.IsFinite(PickupReach) || PickupReach <= 0)
			return InstructionRequestResult.InvalidArguments;

		Vector3 origin = pickupOrigin.GlobalPosition;
		Vector3 forward = -GlobalBasis.Z.Normalized();
		var candidates = new List<Pickable>();
		foreach (Node node in GetTree().GetNodesInGroup(Pickable.GroupName))
		{
			if (node is not Pickable item || item.IsHeld || item.IsQueuedForDeletion()
				|| !item.IsInsideTree())
				continue;
			Vector3 offset = item.GlobalPosition - origin;
			if (offset.LengthSquared() <= PickupReach * PickupReach && forward.Dot(offset) >= 0)
				candidates.Add(item);
		}

		// A stable scene path breaks equal-distance ties, independently of group order.
		candidates.Sort((a, b) =>
		{
			int distanceOrder = a.GlobalPosition.DistanceSquaredTo(origin)
				.CompareTo(b.GlobalPosition.DistanceSquaredTo(origin));
			return distanceOrder != 0 ? distanceOrder
				: string.CompareOrdinal(a.GetPath().ToString(), b.GetPath().ToString());
		});

		var space = GetWorld3D().DirectSpaceState;
		foreach (Pickable item in candidates)
		{
			var query = PhysicsRayQueryParameters3D.Create(origin, item.GlobalPosition,
				PickupObstacleMask, new Godot.Collections.Array<Rid> { GetRid() });
			query.HitFromInside = true;
			var hit = space.IntersectRay(query);
			if (hit.Count > 0 && hit["collider"].AsGodotObject() != item)
				continue;
			if (!item.TryPickUp(HoldPoint))
				continue;

			heldItem = item;
			return InstructionRequestResult.PickedUp;
		}
		return InstructionRequestResult.NoItemInReach;
	}
}
