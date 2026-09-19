using Godot;

public partial class Player : CharacterBody3D
{
	[Export] public float Speed = 5.0f;
	[Export] public float JumpVelocity = 4.5f;

	private Cache cache;

	public override void _Ready()
	{
		base._Ready();
		cache = GetNode<Cache>("/root/Cache");
		cache.Player = this;
	}

	public override void _PhysicsProcess(double delta)
	{
		Vector3 velocity = Velocity;
		velocity += GetGravity() * (float)delta;
		if (Input.IsActionJustPressed("ui_accept") && IsOnFloor())
		{
			velocity.Y = JumpVelocity;
		}

		Velocity = velocity;
		MoveAndSlide();
	}
}
