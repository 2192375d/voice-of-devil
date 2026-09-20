using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using Godot;

static class Program
{
    private static int checks;

    static async Task Main()
    {
        InstructionChecks();
        await ApiChecks();
        Console.WriteLine($"All {checks} targeted cancellation checks passed.");
    }

    private static void InstructionChecks()
    {
        var target = new FakeTarget();
        var manager = new InstructionManager(target);

        Check(manager.WalkForward(10) == InstructionRequestResult.Started, "walk starts");
        Check(manager.Rotate(new Vector3(0, 180, 0)) == InstructionRequestResult.Started, "rotation starts");
        manager.PhysicsUpdate(0.25);
        var walk = manager.ActiveInstructions.OfType<InstructionMoveForward>().Single();
        var rotation = manager.ActiveInstructions.OfType<InstructionRotate>().Single();

        Check(manager.CancelRotation() == InstructionRequestResult.Stopped, "rotation cancellation succeeds");
        Check(rotation.Status == InstructionStatus.Cancelled, "rotation is marked cancelled");
        Check(manager.ActiveInstructions.Count == 1 && manager.ActiveInstructions[0] == walk,
            "rotation cancellation preserves walking");
        double angle = target.RightTurnDegrees;
        manager.PhysicsUpdate(0.25);
        Check(target.WalkingSpeed == 5 && target.RightTurnDegrees == angle,
            "walking continues without further rotation");
        Check(manager.CancelRotation() == InstructionRequestResult.Stopped
            && manager.ActiveInstructions.Count == 1, "rotation cancellation is idempotent");

        Check(manager.Rotate(new Vector3(0, -90, 0)) == InstructionRequestResult.Started,
            "rotation restarts alongside walking");
        rotation = manager.ActiveInstructions.OfType<InstructionRotate>().Single();
        Check(manager.CancelWalk() == InstructionRequestResult.Stopped, "walk cancellation succeeds");
        Check(walk.Status == InstructionStatus.Cancelled && target.WalkingSpeed == 0,
            "walking is cancelled and cleared immediately");
        Check(manager.ActiveInstructions.Count == 1 && manager.ActiveInstructions[0] == rotation,
            "walk cancellation preserves rotation");
        manager.PhysicsUpdate(0.5);
        Check(target.WalkingSpeed == 0 && target.RightTurnDegrees == angle - 45,
            "rotation continues without walking");
        Check(manager.CancelWalk() == InstructionRequestResult.Stopped
            && manager.ActiveInstructions.Count == 1, "walk cancellation is idempotent");
    }

    private static async Task ApiChecks()
    {
        var target = new FakeTarget();
        var manager = new InstructionManager(target);
        var inbox = new GameRequestInbox();
        var order = new List<string>();
        GameCommandResult Execute(GameCommand command)
        {
            order.Add(command.Name);
            return GameInstructionBridge.Execute(manager, command);
        }

        manager.WalkForward(10);
        manager.Rotate(new Vector3(0, 180, 0));
        var replacement = inbox.Submit("replacement", new GameCommand("rotate", 30));
        var cancel = inbox.Submit("cancel", new GameCommand("cancel_rotation"));
        inbox.Drain(true, Execute, _ => throw new Exception("unexpected observation"));
        Check(order.SequenceEqual(new[] { "cancel_rotation", "rotate" }),
            "targeted cancellation is prioritized over ordinary actions");
        Check(Status(await cancel.Task) == "stopped" && !(await cancel.Task).IsError,
            "targeted cancellation returns a successful stopped status");
        Check(Status(await replacement.Task) == "started",
            "a replacement rotation starts on the same tick");
        Check(manager.ActiveInstructions.OfType<InstructionMoveForward>().Count() == 1
            && manager.ActiveInstructions.OfType<InstructionRotate>().Single().Arguments.Y == 30,
            "rotation replacement preserves active walking");

        Check(new GameCommand("cancel_walk").IsControl && new GameCommand("cancel_rotation").IsControl,
            "targeted cancellations are controls");
        Check(!new GameCommand("cancel_walk").IsAction && !new GameCommand("cancel_rotation").IsAction,
            "targeted cancellations are not queued actions");

        var protocol = new GameApiProtocol(inbox);
        foreach (string name in new[] { "cancel_walk", "cancel_rotation" })
        {
            var rejected = await protocol.HandleAsync(
                JsonSerializer.Serialize(new { command = name, arguments = new { unexpected = true } }),
                "invalid-" + name);
            Check(rejected.StatusCode == 400, $"{name} rejects arguments");

            var accepted = protocol.HandleAsync(JsonSerializer.Serialize(new { command = name }), name);
            inbox.Drain(true, Execute, _ => throw new Exception("unexpected observation"));
            var body = JsonSerializer.SerializeToElement((await accepted).Body);
            Check(body.GetProperty("ok").GetBoolean()
                && body.GetProperty("result").GetProperty("status").GetString() == "stopped",
                $"{name} accepts omitted arguments");

            accepted = protocol.HandleAsync(
                JsonSerializer.Serialize(new { command = name, arguments = new { } }),
                "empty-" + name);
            inbox.Drain(true, Execute, _ => throw new Exception("unexpected observation"));
            body = JsonSerializer.SerializeToElement((await accepted).Body);
            Check(body.GetProperty("ok").GetBoolean(), $"{name} accepts an empty argument object");
        }
    }

    private static string Status(GameCommandResult result)
        => JsonSerializer.SerializeToElement(result.State).GetProperty("status").GetString();

    private static void Check(bool condition, string message)
    {
        if (!condition)
            throw new Exception(message);
        checks++;
    }

    private sealed class FakeTarget : IInstructionTarget
    {
        public double RotationSpeedDegrees => 90;
        public double MovementSpeedMetersPerSecond => 5;
        public double WalkingSpeed { get; private set; }
        public double RightTurnDegrees { get; private set; }

        public void CommandWalkForward(double speedMetersPerSecond) => WalkingSpeed = speedMetersPerSecond;
        public void ApplyRightTurnDegrees(double degrees) => RightTurnDegrees += degrees;
        public void ClearCommandedMovement() => WalkingSpeed = 0;
        public InstructionRequestResult TryGrabItem() => InstructionRequestResult.NoItemInReach;
        public InstructionRequestResult TryDropItem() => InstructionRequestResult.HandsEmpty;
        public InstructionRequestResult TryInteract() => InstructionRequestResult.NoInteractableInReach;
    }
}
