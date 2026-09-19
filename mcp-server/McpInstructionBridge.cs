using System.Text.Json;
using Godot;

public static class McpInstructionBridge
{
    public static McpToolResult Execute(InstructionManager manager, McpCommand command)
    {
        var result = command.Name switch
        {
            "walk_forward" => manager.WalkForward(),
            "rotate" => manager.Rotate(new Vector3(0, command.YDegrees, 0)),
            "stop" => manager.Stop(),
            "grab_item" => manager.GrabItem(),
            "drop_item" => manager.DropItem(),
            _ => InstructionRequestResult.InvalidArguments
        };
        bool success = result is InstructionRequestResult.Started or InstructionRequestResult.AlreadyRunning
            or InstructionRequestResult.Stopped or InstructionRequestResult.PickedUp or InstructionRequestResult.Dropped;
        return McpToolResult.Status(JsonNamingPolicy.SnakeCaseLower.ConvertName(result.ToString()), !success);
    }
}
