using Godot;

/// <summary>
/// Attach to a RigidBody3D with a collision shape and visual children.
/// The caller selects the item and validates a clear drop location. Call these
/// methods on the main thread, outside physics-query callbacks.
/// </summary>
public partial class Pickable : RigidBody3D
{
    public const string GroupName = "pickables";

    [Export] public string DisplayName { get; set; } = "Item";

    public bool IsHeld { get; private set; }
    public Node3D HoldPoint { get; private set; }

    private Node releaseParent;
    private uint savedCollisionLayer;
    private uint savedCollisionMask;
    private bool savedFreeze;

    public override void _Ready()
    {
        AddToGroup(GroupName);
    }

    /// <summary>Attach at the hand's origin and orientation, retaining item scale.</summary>
    public bool TryPickUp(Node3D holdPoint)
    {
        if (IsHeld || !IsInsideTree() || IsQueuedForDeletion()
            || !IsInstanceValid(holdPoint) || !holdPoint.IsInsideTree()
            || holdPoint.IsQueuedForDeletion() || holdPoint == this || IsAncestorOf(holdPoint))
            return false;

        releaseParent = GetParent();
        savedCollisionLayer = CollisionLayer;
        savedCollisionMask = CollisionMask;
        savedFreeze = Freeze;

        Freeze = true;
        LinearVelocity = Vector3.Zero;
        AngularVelocity = Vector3.Zero;
        CollisionLayer = 0;
        CollisionMask = 0;

        Reparent(holdPoint, true);
        Position = Vector3.Zero;
        Rotation = Vector3.Zero;
        HoldPoint = holdPoint;
        IsHeld = true;
        return true;
    }

    /// <summary>Check every enabled collision shape at the proposed release transform.</summary>
    public bool IsDropSpaceClear(Transform3D worldTransform, uint obstacleMask)
    {
        var space = GetWorld3D().DirectSpaceState;
        foreach (uint ownerId in GetShapeOwners())
        {
            if (IsShapeOwnerDisabled(ownerId))
                continue;

            for (int i = 0; i < ShapeOwnerGetShapeCount(ownerId); i++)
            {
                var query = new PhysicsShapeQueryParameters3D
                {
                    Shape = ShapeOwnerGetShape(ownerId, i),
                    Transform = worldTransform * ShapeOwnerGetTransform(ownerId),
                    CollisionMask = obstacleMask,
                    Exclude = new Godot.Collections.Array<Rid> { GetRid() },
                    Margin = 0.01f
                };
                // The player remains included, so large items cannot be released inside it.
                if (space.IntersectShape(query, 1).Count > 0)
                    return false;
            }
        }
        return true;
    }

    /// <summary>
    /// Release at a caller-validated world transform, without throwing the item.
    /// Restore the original parent, or the current scene if that parent was removed.
    /// </summary>
    public bool TryDrop(Transform3D worldTransform)
    {
        if (!IsHeld || !IsInsideTree() || IsQueuedForDeletion())
            return false;

        Node parent = releaseParent;
        if (!IsInstanceValid(parent) || !parent.IsInsideTree() || parent.IsQueuedForDeletion())
            parent = GetTree().CurrentScene;

        if (!IsInstanceValid(parent) || !parent.IsInsideTree() || parent.IsQueuedForDeletion()
            || parent == this || IsAncestorOf(parent))
            return false;

        Reparent(parent, true);
        GlobalTransform = worldTransform;
        CollisionLayer = savedCollisionLayer;
        CollisionMask = savedCollisionMask;
        LinearVelocity = Vector3.Zero;
        AngularVelocity = Vector3.Zero;
        Freeze = savedFreeze;
        Sleeping = false;

        HoldPoint = null;
        releaseParent = null;
        IsHeld = false;
        return true;
    }
}
