using System;
using System.Collections.Generic;
using Godot;

/// <summary>All methods and rendering callbacks run on the Godot main thread.</summary>
public sealed class McpObservationService : IDisposable
{
    private readonly McpInbox inbox;
    private readonly Func<Player> playerProvider;
    private readonly Func<double> timeProvider;
    private readonly List<McpPendingCall> waiting = new();
    private readonly List<McpPendingCall> capturing = new();
    private SubViewport viewport;
    private object snapshot;
    private long sequence;

    public McpObservationService(McpInbox inbox, Func<Player> playerProvider, Func<double> timeProvider)
    {
        this.inbox = inbox;
        this.playerProvider = playerProvider;
        this.timeProvider = timeProvider;
        RenderingServer.FramePreDraw += BeforeDraw;
        RenderingServer.FramePostDraw += AfterDraw;
    }

    public void Request(McpPendingCall call)
    {
        if (DisplayServer.GetName() == "headless")
            inbox.Complete(call, McpToolResult.Status("observation_error", true, "A rendering-capable game instance is required."));
        else
        {
            waiting.RemoveAll(old => old.Task.IsCompleted);
            waiting.Add(call);
        }
    }

    private void BeforeDraw()
    {
        waiting.RemoveAll(call => call.Task.IsCompleted);
        if (waiting.Count == 0) return;
        capturing.AddRange(waiting);
        waiting.Clear();
        try
        {
            Player player = playerProvider();
            if (player == null) throw new InvalidOperationException("Player is unavailable.");
            viewport = player.GetNode<SubViewport>("SubViewport");
            var camera = viewport.GetNode<AiCamera>("AICamera");
            camera.SyncToEye();
            snapshot = McpGameState.Snapshot(player, camera, ++sequence, timeProvider(), inbox.PendingActionCount);
        }
        catch (Exception)
        {
            Finish(McpToolResult.Status("observation_error", true, "Could not prepare the AI camera and player state."));
        }
    }

    private void AfterDraw()
    {
        if (capturing.Count == 0) return;
        try
        {
            if (!GodotObject.IsInstanceValid(viewport)) throw new InvalidOperationException();
            using var image = viewport.GetTexture().GetImage();
            if (image == null || image.IsEmpty()) throw new InvalidOperationException();
            byte[] png = image.SavePngToBuffer();
            if (png.Length == 0) throw new InvalidOperationException();
            Finish(new McpToolResult(snapshot, PngBase64: Convert.ToBase64String(png)));
        }
        catch (Exception)
        {
            Finish(McpToolResult.Status("observation_error", true, "Could not capture the rendered AI viewport."));
        }
    }

    private void Finish(McpToolResult result)
    {
        foreach (var call in capturing) inbox.Complete(call, result);
        capturing.Clear();
        snapshot = null;
        viewport = null;
    }

    public void Dispose()
    {
        RenderingServer.FramePreDraw -= BeforeDraw;
        RenderingServer.FramePostDraw -= AfterDraw;
        foreach (var call in waiting) inbox.Complete(call, McpToolResult.Status("shutdown", true));
        waiting.Clear();
        Finish(McpToolResult.Status("shutdown", true));
    }
}
