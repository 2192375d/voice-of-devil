using System;
using System.Text.Json;
using System.Threading.Tasks;

public sealed record McpHttpReply(int StatusCode, object Body = null);

/// <summary>Stateless JSON-RPC endpoint. No game objects are accessed here.</summary>
public sealed class McpProtocol
{
    public const string Version = "2025-11-25";
    private readonly McpInbox inbox;
    public McpProtocol(McpInbox inbox) => this.inbox = inbox;

    public async Task<McpHttpReply> HandleAsync(string json, string versionHeader)
    {
        JsonDocument document;
        try { document = JsonDocument.Parse(json); }
        catch (JsonException) { return Error(400, null, -32700, "Parse error."); }
        using (document)
        {
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object
                || !root.TryGetProperty("jsonrpc", out var rpc) || rpc.ValueKind != JsonValueKind.String || rpc.GetString() != "2.0"
                || !root.TryGetProperty("method", out var methodValue) || methodValue.ValueKind != JsonValueKind.String)
                return Error(400, null, -32600, "Invalid JSON-RPC request.");

            string method = methodValue.GetString();
            bool hasId = root.TryGetProperty("id", out var idValue);
            if (hasId && idValue.ValueKind is not (JsonValueKind.String or JsonValueKind.Number))
                return Error(400, null, -32600, "Request ID must be a string or number.");
            object id = hasId ? idValue.Clone() : null;
            root.TryGetProperty("params", out var parameters);
            if (parameters.ValueKind is not (JsonValueKind.Undefined or JsonValueKind.Object))
                return Error(400, id, -32602, "params must be an object.");

            // Missing headers imply the older 2025-03-26 revision, which this endpoint
            // does not implement. initialize itself negotiates without a version header.
            if ((method != "initialize" && versionHeader != Version)
                || (method == "initialize" && versionHeader != null && versionHeader != Version))
                return Error(400, id, -32600, "Unsupported MCP-Protocol-Version; use " + Version + ".");

            if (!hasId)
            {
                if (method == "notifications/cancelled" && parameters.ValueKind == JsonValueKind.Object
                    && parameters.TryGetProperty("requestId", out var cancelled)
                    && cancelled.ValueKind is JsonValueKind.String or JsonValueKind.Number)
                    inbox.Cancel(cancelled.GetRawText());
                // Unknown notifications are ignored. In particular, tools/call without
                // an ID must never execute a game action.
                return new McpHttpReply(202);
            }

            switch (method)
            {
                case "initialize":
                    if (parameters.ValueKind != JsonValueKind.Object
                        || !parameters.TryGetProperty("protocolVersion", out var version) || version.ValueKind != JsonValueKind.String
                        || !parameters.TryGetProperty("capabilities", out var capabilities) || capabilities.ValueKind != JsonValueKind.Object
                        || !parameters.TryGetProperty("clientInfo", out var client) || client.ValueKind != JsonValueKind.Object
                        || !StringProperty(client, "name") || !StringProperty(client, "version"))
                        return Error(200, id, -32602, "initialize requires protocolVersion, capabilities, and clientInfo.");
                    return Result(id, new
                    {
                        protocolVersion = Version,
                        capabilities = new { tools = new { listChanged = false } },
                        serverInfo = new { name = "voice-of-devil", version = "1.0.0" },
                        instructions = "One shared player. Walking continues until stop. Observe while moving. Stop before grabbing or dropping. clear_queue does not stop active actions."
                    });
                case "ping": return Result(id, new { });
                case "tools/list": return Result(id, McpTools.List());
                case "tools/call":
                    if (parameters.ValueKind != JsonValueKind.Object || !parameters.TryGetProperty("name", out var name)
                        || name.ValueKind != JsonValueKind.String)
                        return Error(200, id, -32602, "tools/call requires a tool name.");
                    parameters.TryGetProperty("arguments", out var arguments);
                    if (!McpTools.TryParse(name.GetString(), arguments, out var command, out string error))
                        return Error(200, id, -32602, error);
                    var result = await inbox.Submit(idValue.GetRawText(), command).Task.ConfigureAwait(false);
                    return Result(id, result.ToWire());
                default: return Error(200, id, -32601, "Method not found.");
            }
        }
    }

    private static bool StringProperty(JsonElement value, string property)
        => value.TryGetProperty(property, out var field) && field.ValueKind == JsonValueKind.String;

    private static McpHttpReply Result(object id, object result) => new(200, new { jsonrpc = "2.0", id, result });
    public static McpHttpReply Error(int status, object id, int code, string message)
        => new(status, new { jsonrpc = "2.0", id, error = new { code, message } });
}
