using Godot;
using System.Collections.Generic;

public partial class Player : CharacterBody3D, IInstructionTarget
{
	[Export] public float Speed = 5.0f;
	[Export] public float RotationSpeed = 90.0f;
	[Export(PropertyHint.Range, "0.1,10,0.1")] public float PickupReach = 2.0f;
	[Export(PropertyHint.Layers3DPhysics)] public uint PickupObstacleMask = uint.MaxValue;
	[Export(PropertyHint.Layers3DPhysics)] public uint DropObstacleMask = uint.MaxValue;

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

		Velocity = velocity;
		MoveAndSlide();
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
