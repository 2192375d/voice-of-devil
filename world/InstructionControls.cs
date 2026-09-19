using Godot;

/// <summary>Development controls that invoke player instructions without a queue.</summary>
public partial class InstructionControls : HBoxContainer
{
    private Player player;
    private bool pickupRequested;
    private bool dropRequested;

    public override void _Ready()
    {
        player = GetNode<Player>("../../Player");
        GetNode<Button>("WalkForward").Pressed += () => player.Instructions.WalkForward();
        GetNode<Button>("Rotate").Pressed += () => player.Instructions.Rotate(new Vector3(0, 90, 0));
        GetNode<Button>("Stop").Pressed += () =>
        {
            pickupRequested = false;
            dropRequested = false;
            player.Instructions.Stop();
        };
        GetNode<Button>("PickUp").Pressed += () =>
        {
            pickupRequested = true;
            dropRequested = false;
        };
        GetNode<Button>("Drop").Pressed += () =>
        {
            dropRequested = true;
            pickupRequested = false;
        };
    }

    public override void _PhysicsProcess(double delta)
    {
        if (pickupRequested)
        {
            pickupRequested = false;
            GD.Print($"Pick up: {player.Instructions.GrabItem()}");
        }
        else if (dropRequested)
        {
            dropRequested = false;
            GD.Print($"Drop: {player.Instructions.DropItem()}");
        }
    }
}
