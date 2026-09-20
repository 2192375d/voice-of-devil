using Godot;

/// <summary>Owns a mutually exclusive pair; closes one door before opening the other.</summary>
public partial class DoorPair : Node
{
    [Export] public Door OrangeDoor;
    [Export] public Door BlueDoor;

    private Door selectedDoor;

    public override void _Ready()
    {
        if (!HasDoors())
        {
            GD.PushError($"{GetPath()}: assign two distinct doors to the pair.");
            SetPhysicsProcess(false);
            return;
        }
        OrangeDoor.AttachPair(this);
        BlueDoor.AttachPair(this);
        OrangeDoor.SetOpen(true, immediate: true);
        BlueDoor.SetOpen(false, immediate: true);
        selectedDoor = OrangeDoor;
    }

    public void SelectDoor(Door door)
    {
        if (HasDoors() && (door == OrangeDoor || door == BlueDoor))
            selectedDoor = door;
    }

    public override void _PhysicsProcess(double delta)
    {
        if (!HasDoors())
        {
            if (IsInstanceValid(OrangeDoor)) OrangeDoor.SetOpen(false);
            if (IsInstanceValid(BlueDoor)) BlueDoor.SetOpen(false);
            return;
        }
        // A selection lasts only while its plate remains weighted. Prefer the
        // other weighted plate on release, otherwise restore the orange default.
        if (!IsPressed(selectedDoor))
            selectedDoor = IsPressed(BlueDoor) ? BlueDoor : OrangeDoor;
        Door other = selectedDoor == OrangeDoor ? BlueDoor : OrangeDoor;
        other.SetOpen(false);
        selectedDoor.SetOpen(other.IsClosed);
    }

    private static bool IsPressed(Door door)
    {
        PressurePlate plate = door.ActivationPlate;
        return IsInstanceValid(plate) && plate.IsInsideTree()
            && !plate.IsQueuedForDeletion() && plate.IsPressed;
    }

    private bool HasDoors() => IsInstanceValid(OrangeDoor) && IsInstanceValid(BlueDoor)
        && OrangeDoor != BlueDoor && OrangeDoor.IsInsideTree() && BlueDoor.IsInsideTree()
        && !OrangeDoor.IsQueuedForDeletion() && !BlueDoor.IsQueuedForDeletion();
}
