using System;
using System.Collections.Generic;
using Godot;

public enum InstructionRequestResult
{
    Started,
    AlreadyRunning,
    Busy,
    InvalidArguments,
    Stopped,
    PickedUp,
    HandsFull,
    NoItemInReach,
    Dropped,
    HandsEmpty,
    DropBlocked,
    Opening,
    Closing,
    NoInteractableInReach,
    PlateControlled
}

/// <summary>
/// Main-thread action controller. Walking and rotation overlap; conflicting requests
/// are rejected rather than queued. The transport layer owns the future request inbox.
/// </summary>
public sealed class InstructionManager
{
    private readonly IInstructionTarget target;
    private readonly List<InstructionSustained> active = new();

    public IReadOnlyList<InstructionSustained> ActiveInstructions { get; }
    public Instruction LastFinishedInstruction { get; private set; }

    public InstructionManager(IInstructionTarget target)
    {
        this.target = target ?? throw new ArgumentNullException(nameof(target));
        ActiveInstructions = active.AsReadOnly();
    }

    public InstructionRequestResult WalkForward(double meters)
    {
        if (!double.IsFinite(meters) || meters <= 0
            || !double.IsFinite(target.MovementSpeedMetersPerSecond) || target.MovementSpeedMetersPerSecond <= 0
            || !double.IsFinite(meters / target.MovementSpeedMetersPerSecond)
            || meters / target.MovementSpeedMetersPerSecond <= 0)
            return InstructionRequestResult.InvalidArguments;

        if (active.Exists(instruction => instruction is InstructionMoveForward))
            return InstructionRequestResult.AlreadyRunning;

        active.Add(new InstructionMoveForward(meters, target.MovementSpeedMetersPerSecond));
        return InstructionRequestResult.Started;
    }

    public InstructionRequestResult Rotate(Vector3 degrees)
    {
        if (!float.IsFinite(degrees.X) || !float.IsFinite(degrees.Y)
            || !float.IsFinite(degrees.Z) || degrees.X != 0 || degrees.Z != 0)
            return InstructionRequestResult.InvalidArguments;

        // A zero-angle request is always a successful no-op.
        if (degrees.Y == 0)
        {
            LastFinishedInstruction = new InstructionRotate(degrees);
            return InstructionRequestResult.Started;
        }

        if (!double.IsFinite(target.RotationSpeedDegrees) || target.RotationSpeedDegrees <= 0)
            return InstructionRequestResult.InvalidArguments;

        if (active.Exists(instruction => instruction is InstructionRotate))
            return InstructionRequestResult.Busy;

        active.Add(new InstructionRotate(degrees));
        return InstructionRequestResult.Started;
    }

    /// <summary>Run on the main physics tick because pickup queries the physics world.</summary>
    public InstructionRequestResult GrabItem()
    {
        if (active.Count > 0)
            return InstructionRequestResult.Busy;

        var instruction = new InstructionGrabItem();
        var result = instruction.Perform(target);
        LastFinishedInstruction = instruction;
        return result;
    }

    /// <summary>Run on the main physics tick because dropping checks for collisions.</summary>
    public InstructionRequestResult DropItem()
    {
        if (active.Count > 0)
            return InstructionRequestResult.Busy;

        var instruction = new InstructionDropItem();
        var result = instruction.Perform(target);
        LastFinishedInstruction = instruction;
        return result;
    }

    /// <summary>Run on the physics tick because interaction checks line of sight.</summary>
    public InstructionRequestResult Interact()
    {
        if (active.Count > 0)
            return InstructionRequestResult.Busy;

        var instruction = new InstructionInteract();
        var result = instruction.Perform(target);
        LastFinishedInstruction = instruction;
        return result;
    }

    public InstructionRequestResult Stop()
    {
        var instruction = new InstructionStop();
        instruction.Perform(this);
        LastFinishedInstruction = instruction;
        return InstructionRequestResult.Stopped;
    }

    public void PhysicsUpdate(double delta)
    {
        if (!double.IsFinite(delta) || delta < 0)
            throw new ArgumentOutOfRangeException(nameof(delta));

        target.ClearCommandedMovement();
        foreach (var instruction in active)
        {
            instruction.Tick(delta, target);
            if (instruction.Status != InstructionStatus.Running)
                LastFinishedInstruction = instruction;
        }
        active.RemoveAll(instruction => instruction.Status != InstructionStatus.Running);
    }

    internal void CancelAll()
    {
        foreach (var instruction in active)
            instruction.Cancel();
        active.Clear();
        target.ClearCommandedMovement();
    }
}
