using Godot;

/// <summary>Detects weight just above a fixed plate; held objects do not count.</summary>
public partial class PressurePlate : StaticBody3D
{
    public bool IsPressed { get; private set; }
    private Area3D sensor;

    public override void _Ready() => sensor = GetNode<Area3D>("Sensor");

    public override void _PhysicsProcess(double delta)
    {
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
    }
}
