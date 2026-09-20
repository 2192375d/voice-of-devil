using System;
using System.Linq;
using System.Text.Json;
using Godot;

/// <summary>Run check.tscn headless for physics tests or rendered for capture tests.</summary>
public partial class ObservationChecks : Node3D
{
    private Player player;
    private AiCamera camera;
    private Pickable target, partial, blocked;
    private StaticBody3D wall;
    private int tick, checks;
    private GameRequestInbox inbox;
    private GameObservationService observations;
    private PendingGameCall pending;
    private bool mutateBeforeDraw;

    public override void _Ready()
    {
        ProcessPhysicsPriority = 2000;
        player = new Player { Name = "Player" };
        player.AddChild(new Marker3D { Name = "Eye" });
        player.AddChild(new Marker3D { Name = "HoldPoint" });
        player.AddChild(new Marker3D { Name = "PickupOrigin" });
        var viewport = new SubViewport { Name = "SubViewport", Size = new Vector2I(512, 512) };
        camera = new AiCamera { Name = "AICamera", Fov = 75, Near = 0.05f, Far = 100 };
        viewport.AddChild(camera);
        player.AddChild(viewport);
        AddChild(player);
        player.SetPhysicsProcess(false);
        // Avoid a second API-owned observation service interfering with this fixture.
        GetNode<GameApiServer>("/root/GameApiServer").SetPhysicsProcess(false);

        target = AddBox(new Pickable { Freeze = true, DisplayName = "target" }, new Vector3(-2, 0, -6));
        blocked = AddBox(new Pickable { Freeze = true, DisplayName = "blocked" }, new Vector3(2, 0, -6));
        wall = AddBox(new StaticBody3D(), new Vector3(1.3f, 0, -4), new Vector3(1.6f, 2, 0.3f));
        partial = AddBox(new Pickable { Freeze = true, DisplayName = "partial" }, new Vector3(0, 0, -6));
        AddBox(new StaticBody3D(), new Vector3(-0.3f, 0, -4), new Vector3(0.4f, 2, 0.3f));
        AddBox(new Pickable { Freeze = true, DisplayName = "behind" }, new Vector3(0, 0, 6));
        AddBox(new Pickable { Freeze = true, DisplayName = "offscreen" }, new Vector3(50, 0, -6));
        AddBox(new Pickable { Freeze = true, DisplayName = "hidden", Visible = false }, new Vector3(0, 2, -6));
        AddBox(new Door(), new Vector3(-2, 2, -6));
        var plate = new PressurePlate();
        plate.AddChild(new Area3D { Name = "Sensor" });
        AddBox(plate, new Vector3(2, 2, -6));
        AddChild(new DirectionalLight3D { RotationDegrees = new Vector3(-30, -20, 0) });
        RenderingServer.FramePreDraw += BeforeDraw;
    }

    private T AddBox<T>(T body, Vector3 position, Vector3? size = null) where T : CollisionObject3D
    {
        Vector3 dimensions = size ?? Vector3.One;
        body.Position = position;
        body.AddChild(new CollisionShape3D { Name = "CollisionShape3D", Shape = new BoxShape3D { Size = dimensions } });
        body.AddChild(new MeshInstance3D { Mesh = new BoxMesh { Size = dimensions } });
        AddChild(body);
        return body;
    }

    private static JsonElement Json(object value) => JsonSerializer.SerializeToElement(value);
    private void Check(bool condition, string message)
    {
        if (!condition) throw new InvalidOperationException(message);
        checks++;
    }

    public override void _PhysicsProcess(double delta)
    {
        try
        {
            tick++;
            if (tick == 4)
            {
                var hints = GameVisualHints.Capture(player, camera);
                var objects = Json(hints.Data).GetProperty("objects").EnumerateArray().ToArray();
                var labels = objects.Select(o => o.GetProperty("label").GetString()).ToArray();
                foreach (string label in new[] { "target", "partial", "door", "pressure plate" })
                    Check(labels.Contains(label), $"Missing visible {label}");
                foreach (string label in new[] { "blocked", "behind", "offscreen", "hidden" })
                    Check(!labels.Contains(label), $"Leaked {label}");
                foreach (var obj in objects)
                {
                    float[] box = obj.GetProperty("bbox").EnumerateArray().Select(v => v.GetSingle()).ToArray();
                    Check(box.All(v => v >= 0 && v <= 1) && box[0] < box[2] && box[1] < box[3], "Invalid box");
                }
                Check(hints.IsCurrent(camera), "Fresh hints invalid");
                wall.Position += Vector3.Right;
                Check(!hints.IsCurrent(camera), "Occluder motion not detected");
                wall.Position -= Vector3.Right;
                Check(hints.IsCurrent(camera), "Restored hints invalid");
                partial.QueueFree();
                Check(!hints.IsCurrent(camera), "Deleted object not detected");
                var near = GameVisualHints.ProjectBounds(camera,
                    new Aabb(new Vector3(-0.1f, -0.1f, -0.1f), new Vector3(0.2f, 0.2f, 0.2f)),
                    new Transform3D(Basis.Identity, new Vector3(0, 0, -0.08f)));
                Check(near.HasValue && near.Value.Position.X >= 0 && near.Value.End.X <= 512,
                    "Near-plane clipping failed");
                var behind = GameVisualHints.ProjectBounds(camera,
                    new Aabb(-Vector3.One, Vector3.One * 2), new Transform3D(Basis.Identity, new Vector3(0, 0, 5)));
                Check(!behind.HasValue, "Behind-camera bounds were projected");
            }
            if (tick == 6)
            {
                inbox = new GameRequestInbox();
                observations = new GameObservationService(inbox, () => player, () => tick / 60.0);
                StartObservation();
                if (DisplayServer.GetName() == "headless")
                {
                    Check(pending.Task.IsCompleted && pending.Task.Result.IsError, "Headless capture did not fail cleanly");
                    Finish();
                    return;
                }
            }
            if (tick >= 6 && pending != null)
            {
                if (!pending.Task.IsCompleted)
                {
                    // Exercise camera translation and rotation while refreshing the prepared frame.
                    player.Position += new Vector3(0.01f, 0, 0);
                    player.RotateY(0.002f);
                    observations.Prepare();
                    Check(camera.ObservationPrepared, "Camera not locked for observation");
                    Check(camera.GlobalTransform.IsEqualApprox(player.GetNode<Marker3D>("Eye").GlobalTransform),
                        "Prepared camera does not match the eye");
                }
                else
                {
                    var result = pending.Task.Result;
                    Check(!result.IsError, "Rendered capture failed");
                    byte[] png = Convert.FromBase64String(result.PngBase64);
                    Check(png.Length > 8 && png[0] == 137 && png[1] == 80, "Invalid PNG");
                    var state = Json(result.State);
                    Check(!camera.ObservationPrepared, "Camera lock not released");
                    if (!mutateBeforeDraw)
                    {
                        Check(state.GetProperty("hints").ValueKind == JsonValueKind.Object, "Fresh hints missing");
                        Check(state.GetProperty("observation_sequence").GetInt64() == 1, "Incorrect initial sequence");
                        mutateBeforeDraw = true;
                        StartObservation();
                        observations.Prepare();
                    }
                    else
                    {
                        Check(state.GetProperty("hints").ValueKind == JsonValueKind.Null, "Stale hints were attached");
                        Check(state.GetProperty("observation_sequence").GetInt64() == 2, "Incorrect second sequence");
                        Finish();
                    }
                }
            }
            if (tick > 180) throw new TimeoutException("Observation test timed out");
        }
        catch (Exception error)
        {
            GD.PushError(error.ToString());
            GetTree().Quit(1);
        }
    }

    private void StartObservation()
    {
        pending = inbox.Submit($"test-{tick}", new GameCommand("observe"));
        inbox.Drain(true, _ => throw new InvalidOperationException(), observations.Request);
    }

    private void BeforeDraw()
    {
        if (mutateBeforeDraw && IsInstanceValid(target)) target.Position += new Vector3(0.01f, 0, 0);
    }

    private void Finish()
    {
        GD.Print($"PASS: {checks} observation checks ({DisplayServer.GetName()})");
        GetTree().Quit();
    }

    public override void _ExitTree()
    {
        RenderingServer.FramePreDraw -= BeforeDraw;
        observations?.Dispose();
    }
}
