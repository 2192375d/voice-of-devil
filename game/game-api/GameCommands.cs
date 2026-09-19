using System;
using System.Text.Json;

public sealed record GameCommand(string Name, float YDegrees = 0)
{
    public bool IsControl => Name is "stop" or "clear_queue";
    public bool IsAction => Name is "walk_forward" or "rotate" or "grab_item" or "drop_item";
}

public sealed record GameCommandResult(object State, bool IsError = false, string PngBase64 = null)
{
    public static GameCommandResult Status(string status, bool error = false, string message = null)
        => new(new { status, message }, error);

    public object ToWire(string requestId) => new
    {
        request_id = requestId,
        ok = !IsError,
        result = State,
        image = PngBase64 == null ? null : new { mime_type = "image/png", data = PngBase64 }
    };
}

public static class GameCommands
{
    public static bool TryParse(string name, JsonElement arguments, out GameCommand command, out string error)
    {
        command = null;
        error = null;
        if (name is not ("walk_forward" or "rotate" or "stop" or "grab_item" or "drop_item" or "observe" or "clear_queue"))
        {
            error = "Unknown command.";
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
                error = "This command takes no arguments.";
                return false;
            }
            command = new GameCommand(name);
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
        command = new GameCommand(name, y);
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
