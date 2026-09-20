using Godot;

/// <summary>Detects weight just above a fixed plate; held objects do not count.</summary>
public partial class PressurePlate : StaticBody3D
{
    [Export] public Door TargetDoor;
    public bool IsPressed { get; private set; }
    private Area3D sensor;

    public override void _Ready()
    {
        sensor = GetNode<Area3D>("Sensor");
        if (IsInstanceValid(TargetDoor))
        {
            TargetDoor.PlateControlled = true;
            TargetDoor.ActivationPlate = this;
        }
    }

    public override void _PhysicsProcess(double delta)
    {
        bool wasPressed = IsPressed;
        IsPressed = false;
        foreach (Node3D body in sensor.GetOverlappingBodies())
        {
            if (!IsInstanceValid(body) || body.IsQueuedForDeletion())
                continue;
            if (body is Pickable item && item.IsHeld)
                continue;
            if (body is RigidBody3D or CharacterBody3D)
            {
                IsPressed = true;
                break;
            }
        }
        // A fresh press takes priority if both plates are weighted; the pair
        // checks ongoing weight and restores its default when plates clear.
        if (IsPressed && !wasPressed && IsInstanceValid(TargetDoor)
            && IsInstanceValid(TargetDoor.Pair))
            TargetDoor.Pair.SelectDoor(TargetDoor);
    }
}
