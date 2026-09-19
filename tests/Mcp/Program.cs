using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

static class Program
{
    private static int checks;

    static async Task Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "--serve")
        {
            await Serve(int.Parse(args[1]));
            return;
        }
        await InboxChecks();
        await ProtocolChecks();
        await HttpChecks();
        Console.WriteLine($"All {checks} MCP checks passed.");
    }

    private static void Check(bool condition, string description)
    {
        if (!condition) throw new Exception(description);
        checks++;
    }

    private static string Status(McpToolResult result) => JsonSerializer.SerializeToElement(result.State).GetProperty("status").GetString();
    private static JsonElement Body(McpHttpReply reply) => JsonSerializer.SerializeToElement(reply.Body);
    private static string Call(int id, string name, object arguments = null) => JsonSerializer.Serialize(new
    {
        jsonrpc = "2.0", id, method = "tools/call", @params = new { name, arguments = arguments ?? new { } }
    });

    private static async Task InboxChecks()
    {
        DateTimeOffset now = DateTimeOffset.UtcNow;
        var inbox = new McpInbox(clock: () => now);
        var target = new FakePlayer();
        var manager = new InstructionManager(target);
        var order = new List<string>();
        McpToolResult Execute(McpCommand command)
        {
            order.Add(command.Name);
            return McpInstructionBridge.Execute(manager, command);
        }
        void Observe(McpPendingCall call) => inbox.Complete(call, new McpToolResult(new { status = "observed" }, PngBase64: "iVBORw0KGgo="));

        var walk = inbox.Submit("1", new("walk_forward"));
        var turn = inbox.Submit("2", new("rotate", 90));
        var busy = inbox.Submit("3", new("rotate", 45));
        inbox.Drain(true, Execute, Observe);
        Check(Status(await walk.Task) == "started" && Status(await turn.Task) == "started", "parallel actions start");
        Check(Status(await busy.Task) == "busy" && (await busy.Task).IsError, "manager busy result is a tool error");
        Check(manager.ActiveInstructions.Count == 2, "walk and turn remain active after response");

        var old = inbox.Submit("4", new("grab_item"));
        var observation = inbox.Submit("5", new("observe"));
        var clear = inbox.Submit("6", new("clear_queue"));
        var later = inbox.Submit("7", new("walk_forward"));
        inbox.Drain(true, Execute, Observe);
        Check(Status(await old.Task) == "cancelled", "clear cancels older action");
        Check(Status(await observation.Task) == "observed", "clear preserves observations");
        Check(Status(await later.Task) == "already_running", "clear preserves later actions and active walk");
        Check(JsonSerializer.SerializeToElement((await clear.Task).State).GetProperty("cleared_count").GetInt32() == 1, "clear reports count");
        Check(manager.ActiveInstructions.Count == 2, "clear does not stop active actions");

        order.Clear();
        var nextWalk = inbox.Submit("8", new("walk_forward"));
        var stop = inbox.Submit("9", new("stop"));
        inbox.Drain(true, Execute, Observe);
        Check(order.SequenceEqual(new[] { "stop", "walk_forward" }), "stop has priority but does not clear pending requests");
        Check(Status(await stop.Task) == "stopped" && Status(await nextWalk.Task) == "started", "pending action may start after stop");

        var expired = inbox.Submit("10", new("rotate", 90));
        now += TimeSpan.FromSeconds(11);
        inbox.Drain(true, Execute, Observe);
        Check(Status(await expired.Task) == "expired" && manager.ActiveInstructions.Count == 1, "expired command cannot execute later");
        var cancelled = inbox.Submit("11", new("rotate", 90));
        inbox.Cancel("11");
        inbox.Drain(true, Execute, Observe);
        Check(Status(await cancelled.Task) == "cancelled" && manager.ActiveInstructions.Count == 1, "explicit cancellation prevents dispatch");

        var notReady = inbox.Submit("12", new("walk_forward"));
        inbox.Drain(false, Execute, Observe);
        Check(Status(await notReady.Task) == "not_ready", "player readiness");
        var pendingImage = inbox.Submit("13", new("observe"));
        inbox.Drain(true, Execute, _ => { });
        now += TimeSpan.FromSeconds(11);
        inbox.Expire();
        Check(Status(await pendingImage.Task) == "observation_error", "missing render callback expires");

        var limited = new McpInbox(capacity: 2);
        var a = limited.Submit("a", new("walk_forward"));
        var duplicate = limited.Submit("a", new("rotate", 90));
        Check(Status(await duplicate.Task) == "duplicate_request", "reject duplicate in-flight request ID");
        var b = limited.Submit("b", new("observe"));
        Check(Status(await limited.Submit("c", new("walk_forward")).Task) == "queue_full", "bounded inbox");
        limited.Shutdown();
        Check(Status(await a.Task) == "shutdown" && Status(await b.Task) == "shutdown", "shutdown resolves outstanding calls");
        Check(Status(await limited.Submit("d", new("stop")).Task) == "shutdown", "shutdown rejects new calls");

        var concurrent = new McpInbox();
        var submitted = await Task.WhenAll(Enumerable.Range(0, 100).Select(i => Task.Run(() => concurrent.Submit(i.ToString(), new("walk_forward")))));
        int executed = 0;
        concurrent.Drain(true, _ => { executed++; return McpToolResult.Status("started"); }, _ => { });
        Check(executed == 64 && concurrent.PendingActionCount == 36, "per-tick budget with concurrent submissions");
        concurrent.Drain(true, _ => { executed++; return McpToolResult.Status("started"); }, _ => { });
        await Task.WhenAll(submitted.Select(c => c.Task));
        Check(executed == 100, "concurrent requests execute exactly once");
    }

    private static async Task ProtocolChecks()
    {
        var inbox = new McpInbox();
        var protocol = new McpProtocol(inbox);
        var malformed = await protocol.HandleAsync("{", McpProtocol.Version);
        Check(Body(malformed).GetProperty("error").GetProperty("code").GetInt32() == -32700, "parse error");
        var batch = await protocol.HandleAsync("[]", McpProtocol.Version);
        Check(batch.StatusCode == 400, "no JSON-RPC batches");
        string initialize = """{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"future-version","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}""";
        var initialized = await protocol.HandleAsync(initialize, null);
        Check(Body(initialized).GetProperty("result").GetProperty("protocolVersion").GetString() == McpProtocol.Version, "version negotiation");
        var notification = await protocol.HandleAsync("""{"jsonrpc":"2.0","method":"notifications/initialized"}""", McpProtocol.Version);
        Check(notification.StatusCode == 202 && notification.Body == null, "notification has empty response");
        var list = await protocol.HandleAsync("""{"jsonrpc":"2.0","id":2,"method":"tools/list"}""", McpProtocol.Version);
        Check(Body(list).GetProperty("result").GetProperty("tools").GetArrayLength() == 7, "seven tools discoverable");
        Check((await protocol.HandleAsync(Call(3, "stop"), "unsupported")).StatusCode == 400, "unsupported protocol header");
        Check((await protocol.HandleAsync(Call(3, "stop"), null)).StatusCode == 400, "missing post-initialize protocol header");
        foreach (var arguments in new object[] { new { }, new { degrees = new { x = 1, y = 90, z = 0 } }, new { degrees = new { x = 0, y = "90", z = 0 } } })
            Check(Body(await protocol.HandleAsync(Call(4, "rotate", arguments), McpProtocol.Version)).TryGetProperty("error", out _), "invalid rotation arguments");
        Check(Body(await protocol.HandleAsync(Call(4, "unknown"), McpProtocol.Version)).TryGetProperty("error", out _), "unknown tool");
        var call = protocol.HandleAsync(Call(5, "rotate", new { degrees = new { x = 0, y = -450, z = 0 } }), McpProtocol.Version);
        inbox.Drain(true, command => { Check(command.YDegrees == -450, "relative signed angle preserved"); return McpToolResult.Status("started"); }, _ => { });
        Check(!Body(await call).GetProperty("result").GetProperty("isError").GetBoolean(), "successful tool response");
        var cancelled = protocol.HandleAsync(Call(6, "walk_forward"), McpProtocol.Version);
        await protocol.HandleAsync("""{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":6}}""", McpProtocol.Version);
        Check(Body(await cancelled).GetProperty("result").GetProperty("structuredContent").GetProperty("status").GetString() == "cancelled", "protocol cancellation reaches inbox");
        var observe = protocol.HandleAsync(Call(7, "observe"), McpProtocol.Version);
        inbox.Drain(true, _ => throw new Exception(), pending => inbox.Complete(pending,
            new McpToolResult(new { status = "observed", observation_sequence = 1 }, PngBase64: "iVBORw0KGgo=")));
        var observation = Body(await observe).GetProperty("result");
        Check(observation.GetProperty("content").GetArrayLength() == 2 && observation.GetProperty("structuredContent").GetProperty("observation_sequence").GetInt32() == 1,
            "image plus structured observation");
        var unwanted = await protocol.HandleAsync("""{"jsonrpc":"2.0","method":"tools/call","params":{"name":"walk_forward"}}""", McpProtocol.Version);
        Check(unwanted.StatusCode == 202 && inbox.PendingActionCount == 0, "notification cannot execute an action");
    }

    private static async Task HttpChecks()
    {
        var probe = new TcpListener(IPAddress.Loopback, 0);
        probe.Start();
        int port = ((IPEndPoint)probe.LocalEndpoint).Port;
        probe.Stop();
        var inbox = new McpInbox();
        using var server = new McpHttpTransport(inbox, new[] { "http://allowed.example" });
        server.Start("127.0.0.1", port);
        using var client = new HttpClient { BaseAddress = new Uri($"http://127.0.0.1:{port}"), Timeout = TimeSpan.FromSeconds(5) };
        using var unsupportedGet = await client.GetAsync("/mcp");
        Check(unsupportedGet.StatusCode == HttpStatusCode.MethodNotAllowed, "GET explicitly declines SSE");
        using var missingPath = await client.GetAsync("/wrong");
        Check(missingPath.StatusCode == HttpStatusCode.NotFound, "endpoint path");
        async Task<HttpResponseMessage> Send(string origin = null, string body = null, bool accept = true)
        {
            using var request = new HttpRequestMessage(HttpMethod.Post, "/mcp");
            request.Content = new StringContent(body ?? """{"jsonrpc":"2.0","id":10,"method":"ping"}""", Encoding.UTF8, "application/json");
            request.Headers.Add("MCP-Protocol-Version", McpProtocol.Version);
            if (accept) request.Headers.Add("Accept", "application/json, text/event-stream");
            if (origin != null) request.Headers.Add("Origin", origin);
            return await client.SendAsync(request);
        }
        using var ping = await Send();
        Check(ping.StatusCode == HttpStatusCode.OK, "real HTTP ping");
        using var forbidden = await Send("http://untrusted.example");
        Check(forbidden.StatusCode == HttpStatusCode.Forbidden, "origin rejection");
        using var allowed = await Send("http://allowed.example");
        Check(allowed.StatusCode == HttpStatusCode.OK, "origin allowlist");
        using var badAccept = await Send(accept: false);
        Check(badAccept.StatusCode == HttpStatusCode.NotAcceptable, "Accept header validation");
        using var large = await Send(body: new string(' ', 65537));
        Check(large.StatusCode == HttpStatusCode.RequestEntityTooLarge, "body size limit");
        var call = Send(body: Call(11, "walk_forward"));
        for (int i = 0; i < 100 && inbox.PendingActionCount == 0; i++) await Task.Delay(10);
        inbox.Drain(true, _ => McpToolResult.Status("started"), _ => { });
        using var reply = await call;
        var body = JsonDocument.Parse(await reply.Content.ReadAsStringAsync());
        Check(body.RootElement.GetProperty("result").GetProperty("structuredContent").GetProperty("status").GetString() == "started", "HTTP request dispatch and response");
    }

    // A deterministic fixture for a standard external MCP client. It hosts the real
    // transport and dispatcher, with no Godot rendering or game simulation.
    private static async Task Serve(int port)
    {
        var inbox = new McpInbox();
        using var server = new McpHttpTransport(inbox);
        server.Start("127.0.0.1", port);
        var manager = new InstructionManager(new FakePlayer());
        Console.WriteLine("MCP fixture ready");
        while (true)
        {
            inbox.Drain(true, command => McpInstructionBridge.Execute(manager, command), call => inbox.Complete(call,
                McpToolResult.Status("observation_error", true, "Fixture has no renderer.")));
            manager.PhysicsUpdate(0.016);
            await Task.Delay(16);
        }
    }

    private sealed class FakePlayer : IInstructionTarget
    {
        public double RotationSpeedDegrees => 90;
        public void CommandWalkForward() { }
        public void ApplyRightTurnDegrees(double degrees) { }
        public void ClearCommandedMovement() { }
        public InstructionRequestResult TryGrabItem() => InstructionRequestResult.NoItemInReach;
        public InstructionRequestResult TryDropItem() => InstructionRequestResult.HandsEmpty;
    }
}
