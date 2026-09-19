using Godot;

public partial class AiCamera : Camera3D
{
    Cache cache;
    public override void _Ready()
    {
        cache = GetNode<Cache>("/root/Cache");
    }

    public override void _Process(double delta)
    {
        Position = cache.Player.Position;
    }
}
