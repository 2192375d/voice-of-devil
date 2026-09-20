using System;
using System.Collections.Generic;
using Godot;

/// <summary>All methods and rendering callbacks run on the Godot main thread.</summary>
public sealed class GameObservationService : IDisposable
{
    private readonly GameRequestInbox inbox;
    private readonly Func<Player> playerProvider;
    private readonly Func<double> timeProvider;
    private readonly List<PendingGameCall> waiting = new();
    private readonly List<PendingGameCall> capturing = new();
    private SubViewport viewport;
    private object snapshot;
    private long sequence;
    private AiCamera camera;
    private GameVisualHints hints;
    private double hintPreparationMs;

    public GameObservationService(GameRequestInbox inbox, Func<Player> playerProvider, Func<double> timeProvider)
    {
        this.inbox = inbox;
        this.playerProvider = playerProvider;
        this.timeProvider = timeProvider;
        RenderingServer.FramePreDraw += BeforeDraw;
        RenderingServer.FramePostDraw += AfterDraw;
    }

    public void Request(PendingGameCall call)
    {
        if (DisplayServer.GetName() == "headless")
            inbox.Complete(call, GameCommandResult.Status("observation_error", true, "A rendering-capable game instance is required."));
        else
        {
            waiting.RemoveAll(old => old.Task.IsCompleted);
            waiting.Add(call);
        }
    }

    public void Prepare()
    {
        waiting.RemoveAll(call => call.Task.IsCompleted);
        if (waiting.Count == 0)
        {
            ReleaseCamera();
            snapshot = null;
            viewport = null;
            hints = null;
            hintPreparationMs = 0;
            return;
        }
        try
        {
            Player player = playerProvider();
            if (player == null) throw new InvalidOperationException("Player is unavailable.");
            viewport = player.GetNode<SubViewport>("SubViewport");
            var nextCamera = viewport.GetNode<AiCamera>("AICamera");
            if (camera != nextCamera) ReleaseCamera();
            camera = nextCamera;
            camera.SyncToEye();
            camera.ObservationPrepared = true;
            hints = null;
            try
            {
                hints = GameVisualHints.Capture(player, camera);
                hintPreparationMs += hints.ElapsedMs;
            }
            catch (Exception exception)
            {
                GD.PushWarning($"Visual hints unavailable: {exception.GetType().Name}");
            }
            snapshot = GameState.Snapshot(player, camera, sequence + 1, timeProvider(), inbox.PendingActionCount,
                hints?.Data);
        }
        catch (Exception)
        {
            capturing.AddRange(waiting);
            waiting.Clear();
            Finish(GameCommandResult.Status("observation_error", true, "Could not prepare the AI camera and player state."));
        }
    }

    private void BeforeDraw()
    {
        waiting.RemoveAll(call => call.Task.IsCompleted);
        if (waiting.Count == 0 || snapshot == null) return;
        capturing.AddRange(waiting);
        waiting.Clear();
        ++sequence;
        try
        {
            if (!GodotObject.IsInstanceValid(camera)) throw new InvalidOperationException();
            object hintData = hints?.Data;
            if (hints != null && !hints.IsCurrent(camera))
            {
                GD.PushWarning($"Visual hints discarded: changed before capture sequence={sequence}");
                hintData = null;
            }
            snapshot = GameState.Snapshot(playerProvider(), camera, sequence, timeProvider(), inbox.PendingActionCount, hintData);
        }
        catch (Exception)
        {
            Finish(GameCommandResult.Status("observation_error", true, "Could not finalize observation state."));
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
            GD.Print($"Observation sequence={sequence} hint_preparation_ms={hintPreparationMs:F2}");
            Finish(new GameCommandResult(snapshot, PngBase64: Convert.ToBase64String(png)));
        }
        catch (Exception)
        {
            Finish(GameCommandResult.Status("observation_error", true, "Could not capture the rendered AI viewport."));
        }
    }

    private void Finish(GameCommandResult result)
    {
        foreach (var call in capturing) inbox.Complete(call, result);
        capturing.Clear();
        snapshot = null;
        viewport = null;
        hints = null;
        hintPreparationMs = 0;
        ReleaseCamera();
    }

    private void ReleaseCamera()
    {
        if (GodotObject.IsInstanceValid(camera)) camera.ObservationPrepared = false;
        camera = null;
    }

    public void Dispose()
    {
        RenderingServer.FramePreDraw -= BeforeDraw;
        RenderingServer.FramePostDraw -= AfterDraw;
        foreach (var call in waiting) inbox.Complete(call, GameCommandResult.Status("shutdown", true));
        waiting.Clear();
        Finish(GameCommandResult.Status("shutdown", true));
    }
}
