using System;
using Godot;

public sealed class InstructionRotate : Instruction<Vector3>
{
    public double RemainingDegrees { get; private set; }

    internal InstructionRotate(Vector3 degrees) : base(degrees)
    {
        RemainingDegrees = degrees.Y;
        if (RemainingDegrees == 0)
            Complete();
    }

    protected override void Update(double delta, IInstructionTarget target)
    {
        double step = Math.CopySign(
            Math.Min(Math.Abs(RemainingDegrees), target.RotationSpeedDegrees * delta),
            RemainingDegrees);

        target.ApplyRightTurnDegrees(step);
        RemainingDegrees -= step;
        if (RemainingDegrees == 0)
            Complete();
    }
}
