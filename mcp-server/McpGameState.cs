using System.Linq;
using System.Text.Json;
using Godot;

public static class McpGameState
{
    public static object Snapshot(Player player, AiCamera camera, long sequence, double simulationTime, int pendingActions)
    {
        var item = player.HeldItem;
        return new
        {
            status = "observed",
            observation_sequence = sequence,
            simulation_time = simulationTime,
            position = Vector(player.GlobalPosition),
            rotation_degrees = Vector(player.GlobalRotationDegrees),
            velocity = Vector(player.Velocity),
            grounded = player.IsOnFloor(),
            held_item = item == null ? null : new { id = item.GetInstanceId().ToString(), name = item.DisplayName },
            camera = new { position = Vector(camera.GlobalPosition), rotation_degrees = Vector(camera.GlobalRotationDegrees), width = 512, height = 512 },
            active_instructions = player.Instructions.ActiveInstructions.Select(InstructionState).ToArray(),
            pending_action_count = pendingActions,
            last_finished_instruction = InstructionState(player.Instructions.LastFinishedInstruction)
        };
    }

    private static object InstructionState(Instruction instruction)
    {
        if (instruction == null) return null;
        return new
        {
            type = instruction switch
            {
                InstructionMoveForward => "walk_forward",
                InstructionRotate => "rotate",
                InstructionGrabItem => "grab_item",
                InstructionDropItem => "drop_item",
                InstructionStop => "stop",
                _ => instruction.GetType().Name
            },
            status = Snake(instruction.Status.ToString()),
            elapsed_seconds = instruction is SustainedAction action ? (double?)action.ElapsedSeconds : null,
            requested_degrees = instruction is InstructionRotate rotation ? (float?)rotation.Arguments.Y : null,
            remaining_degrees = instruction is InstructionRotate turn ? (double?)turn.RemainingDegrees : null,
            result = instruction switch
            {
                InstructionGrabItem grab => Snake(grab.Result.ToString()),
                InstructionDropItem drop => Snake(drop.Result.ToString()),
                InstructionStop => "stopped",
                _ => null
            }
        };
    }

    private static string Snake(string value) => JsonNamingPolicy.SnakeCaseLower.ConvertName(value);
    private static object Vector(Vector3 value) => new { x = value.X, y = value.Y, z = value.Z };
}
