public enum InstructionStatus
{
    Running,
    Completed,
    Cancelled,
    Failed
}

/// <summary>Execution state owned by the manager; external callers can only inspect it.</summary>
public abstract class Instruction
{
    public InstructionStatus Status { get; private set; } = InstructionStatus.Running;

    protected void Complete() => Status = InstructionStatus.Completed;
    protected void Fail() => Status = InstructionStatus.Failed;

    internal void Cancel()
    {
        if (Status == InstructionStatus.Running)
            Status = InstructionStatus.Cancelled;
    }
}

public abstract class InstructionSustained : Instruction
{
    public double ElapsedSeconds { get; private set; }

    internal void Tick(double delta, IInstructionTarget target)
    {
        if (Status != InstructionStatus.Running)
            return;

        ElapsedSeconds += delta;
        Update(delta, target);
    }

    protected abstract void Update(double delta, IInstructionTarget target);
}

/// <summary>A sustained action with strongly typed, immutable arguments.</summary>
public abstract class Instruction<T> : InstructionSustained
{
    public T Arguments { get; }

    protected Instruction(T arguments) => Arguments = arguments;
}
