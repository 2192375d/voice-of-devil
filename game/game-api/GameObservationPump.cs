using System;
using Godot;

/// <summary>Prepare observations after player, door, and plate physics updates.</summary>
public partial class GameObservationPump : Node
{
    public Action Prepare { get; set; }
    public override void _Ready() => ProcessPhysicsPriority = 1000;
    public override void _PhysicsProcess(double delta) => Prepare?.Invoke();
}
