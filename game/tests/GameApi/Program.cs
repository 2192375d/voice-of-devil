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
        Console.WriteLine($"All {checks} Game API checks passed.");
    }

    private static void Check(bool condition, string description)
    {
        if (!condition) throw new Exception(description);
        checks++;
    }

    private static string Status(GameCommandResult result) => JsonSerializer.SerializeToElement(result.State).GetProperty("status").GetString();
    private static JsonElement Body(GameHttpReply reply) => JsonSerializer.SerializeToElement(reply.Body);
    private static string Call(string name, object arguments = null) => JsonSerializer.Serialize(new
    {
        command = name, arguments = arguments ?? new { }
    });

    private static async Task InboxChecks()
    {
        DateTimeOffset now = DateTimeOffset.UtcNow;
        var inbox = new GameRequestInbox(clock: () => now);
        var target = new FakePlayer();
        var manager = new InstructionManager(target);
        var order = new List<string>();
        GameCommandResult Execute(GameCommand command)
        {
            order.Add(command.Name);
            return GameInstructionBridge.Execute(manager, command);
        }
        void Observe(PendingGameCall call) => inbox.Complete(call, new GameCommandResult(new { status = "observed" }, PngBase64: "iVBORw0KGgo="));

        var walk = inbox.Submit("1", new("walk_forward"));
        var turn = inbox.Submit("2", new("rotate", 90));
        var busy = inbox.Submit("3", new("rotate", 45));
        inbox.Drain(true, Execute, Observe);
        Check(Status(await walk.Task) == "started" && Status(await turn.Task) == "started", "parallel actions start");
        Check(Status(await busy.Task) == "busy" && (await busy.Task).IsError, "manager busy result is a command error");
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
        var notReady = inbox.Submit("12", new("walk_forward"));
        inbox.Drain(false, Execute, Observe);
        Check(Status(await notReady.Task) == "not_ready", "player readiness");
        var pendingImage = inbox.Submit("13", new("observe"));
        inbox.Drain(true, Execute, _ => { });
        now += TimeSpan.FromSeconds(11);
        inbox.Expire();
        Check(Status(await pendingImage.Task) == "observation_error", "missing render callback expires");

        var limited = new GameRequestInbox(capacity: 2);
        var a = limited.Submit("a", new("walk_forward"));
        var duplicate = limited.Submit("a", new("rotate", 90));
        Check(Status(await duplicate.Task) == "duplicate_request", "reject duplicate in-flight request ID");
        var b = limited.Submit("b", new("observe"));
        Check(Status(await limited.Submit("c", new("walk_forward")).Task) == "queue_full", "bounded inbox");
        limited.Shutdown();
        Check(Status(await a.Task) == "shutdown" && Status(await b.Task) == "shutdown", "shutdown resolves outstanding calls");
        Check(Status(await limited.Submit("d", new("stop")).Task) == "shutdown", "shutdown rejects new calls");

        var concurrent = new GameRequestInbox();
        var submitted = await Task.WhenAll(Enumerable.Range(0, 100).Select(i => Task.Run(() => concurrent.Submit(i.ToString(), new("walk_forward")))));
        int executed = 0;
        concurrent.Drain(true, _ => { executed++; return GameCommandResult.Status("started"); }, _ => { });
        Check(executed == 64 && concurrent.PendingActionCount == 36, "per-tick budget with concurrent submissions");
        concurrent.Drain(true, _ => { executed++; return GameCommandResult.Status("started"); }, _ => { });
        await Task.WhenAll(submitted.Select(c => c.Task));
        Check(executed == 100, "concurrent requests execute exactly once");
    }

    private static async Task ProtocolChecks()
    {
        var inbox = new GameRequestInbox();
        var protocol = new GameApiProtocol(inbox);
        foreach (string invalid in new[] { "{", "[]", "{}", "null", "{\"command\":3}",
            "{\"command\":\"stop\",\"extra\":1}", "{\"command\":\"stop\",\"command\":\"walk_forward\"}" })
        {
            var reply = await protocol.HandleAsync(invalid, "test");
            Check(reply.StatusCode == 400 && !Body(reply).GetProperty("ok").GetBoolean(), "invalid request envelope");
        }
        foreach (var arguments in new object[] { new { }, new { degrees = new { x = 1, y = 90, z = 0 } }, new { degrees = new { x = 0, y = "90", z = 0 } } })
            Check((await protocol.HandleAsync(Call("rotate", arguments), "test")).StatusCode == 400, "invalid rotation arguments");
        Check((await protocol.HandleAsync(Call("unknown"), "test")).StatusCode == 400, "unknown command");
        Check((await protocol.HandleAsync(Call("stop", new { extra = 1 }), "test")).StatusCode == 400, "unexpected arguments");
        var call = protocol.HandleAsync(Call("rotate", new { degrees = new { x = 0, y = -450, z = 0 } }), "turn");
        inbox.Drain(true, command => { Check(command.YDegrees == -450, "relative signed angle preserved"); return GameCommandResult.Status("started"); }, _ => { });
        var result = Body(await call);
        Check(result.GetProperty("ok").GetBoolean() && result.GetProperty("request_id").GetString() == "turn"
            && result.GetProperty("image").ValueKind == JsonValueKind.Null, "command response envelope");
        var observe = protocol.HandleAsync(Call("observe"), "observe");
        inbox.Drain(true, _ => throw new Exception(), pending => inbox.Complete(pending,
            new GameCommandResult(new { status = "observed", observation_sequence = 1 }, PngBase64: "iVBORw0KGgo=")));
        var observation = Body(await observe);
        Check(observation.GetProperty("image").GetProperty("mime_type").GetString() == "image/png"
            && observation.GetProperty("result").GetProperty("observation_sequence").GetInt32() == 1, "image and state response");
        var busy = protocol.HandleAsync(Call("grab_item"), "busy");
        inbox.Drain(true, _ => GameCommandResult.Status("busy", true), _ => { });
        var failure = await busy;
        Check(failure.StatusCode == 200 && !Body(failure).GetProperty("ok").GetBoolean(), "gameplay rejection is HTTP 200 with ok false");
        var noArgs = protocol.HandleAsync("{\"command\":\"stop\"}", "stop");
        inbox.Drain(true, _ => GameCommandResult.Status("stopped"), _ => { });
        Check(Body(await noArgs).GetProperty("ok").GetBoolean(), "arguments can be omitted");
    }

    private static async Task HttpChecks()
    {
        var probe = new TcpListener(IPAddress.Loopback, 0);
        probe.Start();
        int port = ((IPEndPoint)probe.LocalEndpoint).Port;
        probe.Stop();
        var inbox = new GameRequestInbox();
        using var server = new GameHttpTransport(inbox, new[] { "http://allowed.example" });
        server.Start("127.0.0.1", port);
        using var client = new HttpClient { BaseAddress = new Uri($"http://127.0.0.1:{port}"), Timeout = TimeSpan.FromSeconds(5) };
        using var unsupportedGet = await client.GetAsync("/api/v1/commands");
        Check(unsupportedGet.StatusCode == HttpStatusCode.MethodNotAllowed, "GET rejected");
        using var oldPath = await client.GetAsync("/mcp");
        Check(oldPath.StatusCode == HttpStatusCode.NotFound, "old MCP endpoint removed");
        async Task<HttpResponseMessage> Send(string origin = null, string body = "{}", string contentType = "application/json")
        {
            using var request = new HttpRequestMessage(HttpMethod.Post, "/api/v1/commands");
            request.Content = new StringContent(body, Encoding.UTF8, contentType);
            if (origin != null) request.Headers.Add("Origin", origin);
            return await client.SendAsync(request);
        }
        using var malformed = await Send();
        Check(malformed.StatusCode == HttpStatusCode.BadRequest, "no handshake or MCP headers required");
        using var forbidden = await Send("http://untrusted.example");
        Check(forbidden.StatusCode == HttpStatusCode.Forbidden, "origin rejection");
        using var allowed = await Send("http://allowed.example");
        Check(allowed.StatusCode == HttpStatusCode.BadRequest, "allowed origin reaches validation");
        using var wrongType = await Send(contentType: "text/plain");
        Check(wrongType.StatusCode == HttpStatusCode.UnsupportedMediaType, "content type validation");
        using var large = await Send(body: new string(' ', 65537));
        Check(large.StatusCode == HttpStatusCode.RequestEntityTooLarge, "body size limit");
        foreach (var response in new[] { unsupportedGet, oldPath, malformed, forbidden, allowed, wrongType, large })
        {
            using var error = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
            Check(!error.RootElement.GetProperty("ok").GetBoolean()
                && error.RootElement.GetProperty("request_id").GetString().Length > 0, "HTTP errors use JSON envelope");
        }
        var call = Send(body: Call("walk_forward"));
        for (int i = 0; i < 100 && inbox.PendingActionCount == 0; i++) await Task.Delay(10);
        inbox.Drain(true, _ => GameCommandResult.Status("started"), _ => { });
        using var reply = await call;
        using var body = JsonDocument.Parse(await reply.Content.ReadAsStringAsync());
        Check(body.RootElement.GetProperty("result").GetProperty("status").GetString() == "started", "HTTP dispatch and response");
    }

    // A deterministic fixture for a plain HTTP client. It hosts the real
    // transport and dispatcher, with no Godot rendering or game simulation.
    private static async Task Serve(int port)
    {
        var inbox = new GameRequestInbox();
        using var server = new GameHttpTransport(inbox);
        server.Start("127.0.0.1", port);
        var manager = new InstructionManager(new FakePlayer());
        Console.WriteLine("Game API fixture ready");
        while (true)
        {
            inbox.Drain(true, command => GameInstructionBridge.Execute(manager, command), call => inbox.Complete(call,
                GameCommandResult.Status("observation_error", true, "Fixture has no renderer.")));
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
