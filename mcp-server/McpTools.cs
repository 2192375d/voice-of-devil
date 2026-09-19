using System;
using System.Collections.Generic;
using System.Text.Json;

public sealed record McpCommand(string Name, float YDegrees = 0)
{
    public bool IsControl => Name is "stop" or "clear_queue";
    public bool IsAction => Name is "walk_forward" or "rotate" or "grab_item" or "drop_item";
}

public sealed record McpToolResult(object State, bool IsError = false, string PngBase64 = null)
{
    public static McpToolResult Status(string status, bool error = false, string message = null)
        => new(new { status, message }, error);

    public object ToWire()
    {
        var content = new List<object> { new { type = "text", text = JsonSerializer.Serialize(State) } };
        if (PngBase64 != null)
            content.Add(new { type = "image", mimeType = "image/png", data = PngBase64 });
        return new { content, structuredContent = State, isError = IsError };
    }
}

public static class McpTools
{
    public static object List()
    {
        object empty = new { type = "object", properties = new { }, additionalProperties = false };
        object angle = new
        {
            type = "object",
            properties = new
            {
                degrees = new
                {
                    type = "object",
                    properties = new
                    {
                        x = new { type = "number", @const = 0 },
                        y = new { type = "number", description = "Relative degrees; positive turns right." },
                        z = new { type = "number", @const = 0 }
                    },
                    required = new[] { "x", "y", "z" }, additionalProperties = false
                }
            },
            required = new[] { "degrees" }, additionalProperties = false
        };
        object Tool(string name, string description, object schema) => new { name, description, inputSchema = schema };
        return new
        {
            tools = new[]
            {
                Tool("walk_forward", "Walk continuously until stop. May overlap rotation. Repeated calls are idempotent.", empty),
                Tool("rotate", "Turn by relative Y degrees at fixed speed, then finish. Another active turn returns busy.", angle),
                Tool("stop", "Cancel all active actions. Does not clear pending requests or drop the held item.", empty),
                Tool("grab_item", "Pick the nearest visible item in the forward hemisphere. Requires idle movement and empty hands.", empty),
                Tool("drop_item", "Release the held item if there is room. Requires idle movement.", empty),
                Tool("observe", "Get a first-person PNG and matching player/action state. Gameplay continues.", empty),
                Tool("clear_queue", "Discard earlier pending action requests, without stopping active actions. Preserve observations and later requests.", empty)
            }
        };
    }

    public static bool TryParse(string name, JsonElement arguments, out McpCommand command, out string error)
    {
        command = null;
        error = null;
        if (name is not ("walk_forward" or "rotate" or "stop" or "grab_item" or "drop_item" or "observe" or "clear_queue"))
        {
            error = "Unknown tool.";
            return false;
        }
        if (arguments.ValueKind != JsonValueKind.Undefined && arguments.ValueKind != JsonValueKind.Object)
        {
            error = "arguments must be an object.";
            return false;
        }
        if (name != "rotate")
        {
            if (arguments.ValueKind == JsonValueKind.Object && arguments.EnumerateObject().MoveNext())
            {
                error = "This tool takes no arguments.";
                return false;
            }
            command = new McpCommand(name);
            return true;
        }
        if (arguments.ValueKind != JsonValueKind.Object || !arguments.TryGetProperty("degrees", out var degrees)
            || degrees.ValueKind != JsonValueKind.Object || Count(arguments) != 1 || Count(degrees) != 3
            || !Number(degrees, "x", out float x) || !Number(degrees, "y", out float y)
            || !Number(degrees, "z", out float z) || x != 0 || z != 0)
        {
            error = "rotate requires degrees: {x: 0, y: finite number, z: 0}.";
            return false;
        }
        command = new McpCommand(name, y);
        return true;
    }

    private static int Count(JsonElement obj)
    {
        int count = 0;
        foreach (var property in obj.EnumerateObject()) count++;
        return count;
    }

    private static bool Number(JsonElement obj, string name, out float value)
    {
        value = 0;
        return obj.TryGetProperty(name, out var element) && element.ValueKind == JsonValueKind.Number
            && element.TryGetSingle(out value) && float.IsFinite(value);
    }
}
