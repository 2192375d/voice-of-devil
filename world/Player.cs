using Godot;

public partial class Player : CharacterBody3D
{
    [Export] public float Speed = 5.0f;
    [Export] public float JumpVelocity = 4.5f;

    public override void _PhysicsProcess(double delta)
    {
        Vector3 velocity = Vector3.Zero;
        if (Input.IsActionJustPressed("ui_accept") && IsOnFloor())
        {
            velocity.Y = JumpVelocity;
        }
        Velocity = velocity;
        MoveAndSlide();
    }
}
