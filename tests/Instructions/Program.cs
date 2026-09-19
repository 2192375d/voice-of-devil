using System;
using Godot;

// Run with: dotnet run --project tests/Instructions/Instructions.csproj
// This checks scheduling and action math without requiring the Godot runtime.
static class Program
{
    static void Main()
    {
        var target = new FakePlayer();
        var manager = new InstructionManager(target);

        Check(manager.WalkForward() == InstructionRequestResult.Started, "start walking");
        Check(manager.WalkForward() == InstructionRequestResult.AlreadyRunning, "duplicate walk");
        Check(manager.Rotate(new Vector3(0, 90, 0)) == InstructionRequestResult.Started, "concurrent rotation");
        Check(manager.Rotate(new Vector3(0, -90, 0)) == InstructionRequestResult.Busy, "rotation conflict");
        manager.PhysicsUpdate(0.5);
        Check(target.Walking && target.RightTurnDegrees == 45, "walk and rotate together");
        manager.PhysicsUpdate(0.75);
        Check(target.RightTurnDegrees == 90, "clamp final rotation step");
        Check(manager.ActiveInstructions.Count == 1, "walking survives rotation completion");
        Check(manager.LastFinishedInstruction.Status == InstructionStatus.Completed, "completion status");
        manager.PhysicsUpdate(100);
        Check(target.Walking && manager.ActiveInstructions.Count == 1, "walking has no duration limit");

        Check(manager.Rotate(new Vector3(0, -450, 0)) == InstructionRequestResult.Started, "negative multi-turn");
        manager.PhysicsUpdate(5);
        Check(target.RightTurnDegrees == -360, "preserve signed angles beyond 360 degrees");

        manager.Rotate(new Vector3(0, 180, 0));
        manager.PhysicsUpdate(0.25);
        var walking = manager.ActiveInstructions[0];
        var rotating = manager.ActiveInstructions[1];
        Check(rotating.ElapsedSeconds == 0.25, "elapsed simulation time");
        double angleBeforeStop = target.RightTurnDegrees;
        Check(manager.Stop() == InstructionRequestResult.Stopped, "global stop");
        Check(!target.Walking && manager.ActiveInstructions.Count == 0, "immediate stop");
        Check(walking.Status == InstructionStatus.Cancelled && rotating.Status == InstructionStatus.Cancelled, "cancel all statuses");
        manager.PhysicsUpdate(1);
        Check(!target.Walking && target.RightTurnDegrees == angleBeforeStop, "no motion after stop");
        Check(manager.Stop() == InstructionRequestResult.Stopped, "repeat stop");
        Check(manager.WalkForward() == InstructionRequestResult.Started, "restart walking");
        Check(manager.Rotate(new Vector3(0, 45, 0)) == InstructionRequestResult.Started, "restart rotation");
        manager.PhysicsUpdate(0.5);
        Check(target.Walking && target.RightTurnDegrees == angleBeforeStop + 45, "relative rotation after restart");
        manager.Stop();

        foreach (Vector3 invalid in new[] {
            new Vector3(1, 0, 0), new Vector3(0, 0, 1),
            new Vector3(0, float.NaN, 0), new Vector3(0, float.PositiveInfinity, 0) })
            Check(manager.Rotate(invalid) == InstructionRequestResult.InvalidArguments, "reject invalid angles");
        Check(manager.Rotate(Vector3.Zero) == InstructionRequestResult.Started
            && manager.ActiveInstructions.Count == 0, "zero rotation completes immediately");
        target.RotationSpeedDegrees = 0;
        Check(manager.Rotate(new Vector3(0, 90, 0)) == InstructionRequestResult.InvalidArguments, "reject invalid rotation speed");
        Check(manager.GrabItem() == InstructionRequestResult.NoItemInReach, "empty pickup");
        Check(manager.LastFinishedInstruction.Status == InstructionStatus.Failed, "failed pickup status");
        target.GrabResult = InstructionRequestResult.PickedUp;
        manager.WalkForward();
        int callsBeforeBusy = target.GrabCalls;
        Check(manager.GrabItem() == InstructionRequestResult.Busy && target.GrabCalls == callsBeforeBusy,
            "walking prevents pickup without touching items");
        manager.Stop();
        target.RotationSpeedDegrees = 90;
        manager.Rotate(new Vector3(0, 90, 0));
        Check(manager.GrabItem() == InstructionRequestResult.Busy, "rotation prevents pickup");
        manager.Stop();
        Check(manager.GrabItem() == InstructionRequestResult.PickedUp, "pickup after stop");
        Check(manager.LastFinishedInstruction.Status == InstructionStatus.Completed, "pickup completion");
        target.GrabResult = InstructionRequestResult.HandsFull;
        Check(manager.GrabItem() == InstructionRequestResult.HandsFull, "held item prevents another pickup");
        Check(manager.DropItem() == InstructionRequestResult.HandsEmpty, "empty hands drop");
        target.DropResult = InstructionRequestResult.Dropped;
        manager.WalkForward();
        int dropsBeforeBusy = target.DropCalls;
        Check(manager.DropItem() == InstructionRequestResult.Busy && target.DropCalls == dropsBeforeBusy,
            "walking prevents drop without releasing item");
        manager.Stop();
        manager.Rotate(new Vector3(0, 90, 0));
        Check(manager.DropItem() == InstructionRequestResult.Busy, "rotation prevents drop");
        manager.Stop();
        target.DropResult = InstructionRequestResult.DropBlocked;
        Check(manager.DropItem() == InstructionRequestResult.DropBlocked, "blocked drop result");
        Check(manager.LastFinishedInstruction.Status == InstructionStatus.Failed, "blocked drop status");
        target.DropResult = InstructionRequestResult.Dropped;
        Check(manager.DropItem() == InstructionRequestResult.Dropped, "drop after stop");
        Check(manager.LastFinishedInstruction is InstructionDropItem drop
            && drop.Result == InstructionRequestResult.Dropped
            && drop.Status == InstructionStatus.Completed, "drop completion");
        Console.WriteLine("All instruction checks passed.");
    }

    static void Check(bool condition, string message)
    {
        if (!condition)
            throw new Exception(message);
    }

    sealed class FakePlayer : IInstructionTarget
    {
        public double RotationSpeedDegrees { get; set; } = 90;
        public bool Walking { get; private set; }
        public double RightTurnDegrees { get; private set; }
        public InstructionRequestResult GrabResult { get; set; } = InstructionRequestResult.NoItemInReach;
        public int GrabCalls { get; private set; }
        public InstructionRequestResult DropResult { get; set; } = InstructionRequestResult.HandsEmpty;
        public int DropCalls { get; private set; }
        public InstructionRequestResult TryDropItem()
        {
            DropCalls++;
            return DropResult;
        }
        public InstructionRequestResult TryGrabItem()
        {
            GrabCalls++;
            return GrabResult;
        }
        public void CommandWalkForward() => Walking = true;
        public void ApplyRightTurnDegrees(double degrees) => RightTurnDegrees += degrees;
        public void ClearCommandedMovement() => Walking = false;
    }
}
