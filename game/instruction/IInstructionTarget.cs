/// <summary>Player operations used by instructions, all on the Godot main thread.</summary>
public interface IInstructionTarget
{
    double RotationSpeedDegrees { get; }
    double MovementSpeedMetersPerSecond { get; }
    void CommandWalkForward(double speedMetersPerSecond);
    void ApplyRightTurnDegrees(double degrees);
    void ClearCommandedMovement();
    InstructionRequestResult TryGrabItem();
    InstructionRequestResult TryDropItem();
    InstructionRequestResult TryInteract();
}
