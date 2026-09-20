using System;

/// <summary>Timed forward/backward movement; collisions do not extend its duration.</summary>
public sealed class InstructionMoveForward : Instruction<double>
{
    public double DurationSeconds { get; }
    public double RemainingSeconds { get; private set; }
    private readonly double speed;

    internal InstructionMoveForward(double meters, double speedMetersPerSecond) : base(meters)
    {
        if (meters == 0)
        {
            Complete();
            return;
        }

        speed = Math.CopySign(speedMetersPerSecond, meters);
        DurationSeconds = Math.Abs(meters) / speedMetersPerSecond;
        RemainingSeconds = DurationSeconds;
    }

    protected override void Update(double delta, IInstructionTarget target)
    {
        if (delta == 0)
            return;

        double movementSeconds = Math.Min(delta, RemainingSeconds);
        // Scale the last physics tick so small distances cannot overshoot by a full tick.
        target.CommandWalkForward(speed * (movementSeconds / delta));
        RemainingSeconds -= movementSeconds;
        if (RemainingSeconds <= 1e-12 * DurationSeconds)
        {
            RemainingSeconds = 0;
            Complete();
        }
    }
}
