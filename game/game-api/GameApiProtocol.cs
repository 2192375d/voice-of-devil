using System;
using System.Text.Json;
using System.Threading.Tasks;

public sealed record GameHttpReply(int StatusCode, object Body);

/// <summary>Plain JSON command validation; never accesses Godot objects.</summary>
public sealed class GameApiProtocol
{
    private readonly GameRequestInbox inbox;
    public GameApiProtocol(GameRequestInbox inbox) => this.inbox = inbox;

    public static GameHttpReply Error(int code, string requestId, string status, string message)
        => new(code, GameCommandResult.Status(status, true, message).ToWire(requestId));

    public async Task<GameHttpReply> HandleAsync(string body, string requestId)
    {
        try
        {
            using var document = JsonDocument.Parse(body);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
                return Error(400, requestId, "invalid_request", "Request must be a JSON object.");
            bool hasCommand = false, hasArguments = false;
            foreach (var property in root.EnumerateObject())
            {
                if (property.Name == "command" && !hasCommand) hasCommand = true;
                else if (property.Name == "arguments" && !hasArguments) hasArguments = true;
                else return Error(400, requestId, "invalid_request", "Unknown or duplicate request property.");
            }
            if (!root.TryGetProperty("command", out var name) || name.ValueKind != JsonValueKind.String)
                return Error(400, requestId, "invalid_request", "command must be a string.");
            root.TryGetProperty("arguments", out var arguments);
            if (!GameCommands.TryParse(name.GetString(), arguments, out var command, out string error))
                return Error(400, requestId, "invalid_arguments", error);
            var call = inbox.Submit(requestId, command);
            var result = await call.Task.ConfigureAwait(false);
            return new GameHttpReply(200, result.ToWire(requestId));
        }
        catch (JsonException)
        {
            return Error(400, requestId, "invalid_json", "Request body is not valid JSON.");
        }
    }
}
