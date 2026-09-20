using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using Godot;

/// <summary>Approximate visual evidence, not an authoritative list of visible objects.</summary>
public sealed class GameVisualHints
{
    private readonly List<(Node3D Node, Transform3D Transform, bool Visible, uint Layers)> tracked = new();
    private readonly SceneTree tree;
    private readonly int nodeCount;
    private readonly Transform3D cameraTransform;
    private readonly float fov, near, far;
    private readonly uint cullMask;
    public object Data { get; private set; }
    public double ElapsedMs { get; private set; }

    private GameVisualHints(Camera3D camera)
    {
        cameraTransform = camera.GlobalTransform;
        fov = camera.Fov;
        near = camera.Near;
        far = camera.Far;
        cullMask = camera.CullMask;
        tree = camera.GetTree();
        nodeCount = tree.GetNodeCount();
    }

    // Called only from the late physics callback, while physics space is accessible.
    public static GameVisualHints Capture(Player player, Camera3D camera)
    {
        var timer = Stopwatch.StartNew();
        var frame = new GameVisualHints(camera);
        var candidates = new List<(CollisionObject3D Body, string Label, Rect2 Box)>();
        foreach (Node node in Descendants(player.GetTree().CurrentScene))
        {
            // Track occluders too: a moving wall invalidates the visibility result.
            if (node is Node3D spatial && node is CollisionObject3D or MeshInstance3D)
                frame.tracked.Add((spatial, spatial.GlobalTransform, spatial.IsVisibleInTree(), Layers(spatial)));
            if (node is not CollisionObject3D body || body.IsQueuedForDeletion()) continue;
            string label = body switch
            {
                Pickable item when !item.IsHeld => item.DisplayName,
                Door => "door",
                PressurePlate => "pressure plate",
                _ => null
            };
            if (label == null) continue;
            Rect2? bounds = null;
            foreach (var mesh in VisualMeshes(body))
            {
                if (mesh.Mesh == null || !mesh.IsVisibleInTree() || (mesh.Layers & camera.CullMask) == 0)
                    continue;
                var box = ProjectBounds(camera, mesh.GetAabb(), mesh.GlobalTransform);
                if (box.HasValue) bounds = bounds.HasValue ? bounds.Value.Merge(box.Value) : box;
            }
            if (bounds.HasValue) candidates.Add((body, label, bounds.Value));
        }

        var objects = new List<object>();
        var viewportSize = camera.GetViewport().GetVisibleRect().Size;
        var space = player.GetWorld3D().DirectSpaceState;
        var excluded = new Godot.Collections.Array<Rid> { player.GetRid() };
        foreach (var candidate in candidates.OrderByDescending(c => c.Box.Size.X * c.Box.Size.Y)
                     .ThenBy(c => c.Body.GetInstanceId()))
        {
            bool visible = false;
            foreach (float y in new[] { 0.2f, 0.5f, 0.8f })
            {
                foreach (float x in new[] { 0.2f, 0.5f, 0.8f })
                {
                    Vector2 pixel = candidate.Box.Position + candidate.Box.Size * new Vector2(x, y);
                    Vector3 origin = camera.ProjectRayOrigin(pixel);
                    var query = PhysicsRayQueryParameters3D.Create(origin,
                        origin + camera.ProjectRayNormal(pixel) * camera.Far, uint.MaxValue, excluded);
                    query.HitFromInside = true;
                    var hit = space.IntersectRay(query);
                    if (hit.Count > 0 && hit["collider"].AsGodotObject() == candidate.Body)
                    {
                        visible = true;
                        break;
                    }
                }
                if (visible) break;
            }
            if (!visible) continue;
            objects.Add(new
            {
                id = candidate.Body.GetInstanceId().ToString(),
                label = candidate.Label,
                bbox = new[] { candidate.Box.Position.X / viewportSize.X,
                    candidate.Box.Position.Y / viewportSize.Y, candidate.Box.End.X / viewportSize.X,
                    candidate.Box.End.Y / viewportSize.Y }
            });
            if (objects.Count == 32) break;
        }
        frame.Data = new { source = "godot", bbox_format = "normalized_xyxy", objects };
        frame.ElapsedMs = timer.Elapsed.TotalMilliseconds;
        return frame;
    }

    public bool IsCurrent(Camera3D camera) => camera.GlobalTransform == cameraTransform
        && camera.Fov == fov && camera.Near == near && camera.Far == far && camera.CullMask == cullMask
        && tree.GetNodeCount() == nodeCount
        && tracked.All(t => GodotObject.IsInstanceValid(t.Node) && t.Node.IsInsideTree()
            && !t.Node.IsQueuedForDeletion() && t.Node.GlobalTransform == t.Transform
            && t.Node.IsVisibleInTree() == t.Visible && Layers(t.Node) == t.Layers);

    private static uint Layers(Node3D node) => node switch
    {
        CollisionObject3D body => body.CollisionLayer,
        MeshInstance3D mesh => mesh.Layers,
        _ => 0
    };

    public static Rect2? ProjectBounds(Camera3D camera, Aabb bounds, Transform3D transform)
    {
        var local = camera.GlobalTransform.AffineInverse() * transform;
        var corners = new Vector3[8];
        for (int i = 0; i < 8; i++)
            corners[i] = local * (bounds.Position + bounds.Size * new Vector3(
                (i & 1) != 0 ? 1 : 0, (i & 2) != 0 ? 1 : 0, (i & 4) != 0 ? 1 : 0));
        var points = new List<Vector3>();
        foreach (var p in corners)
            if (-p.Z >= camera.Near && -p.Z <= camera.Far) points.Add(p);
        // Clip each of the twelve box edges against near/far depth planes.
        for (int i = 0; i < 8; i++)
            foreach (int bit in new[] { 1, 2, 4 })
            {
                if ((i & bit) != 0) continue;
                Vector3 a = corners[i], b = corners[i | bit];
                if (Mathf.IsZeroApprox(b.Z - a.Z)) continue;
                foreach (float plane in new[] { -camera.Near, -camera.Far })
                {
                    float t = (plane - a.Z) / (b.Z - a.Z);
                    if (t >= 0 && t <= 1) points.Add(a.Lerp(b, t));
                }
            }
        if (points.Count == 0) return null;
        Vector2 min = new(float.PositiveInfinity, float.PositiveInfinity);
        Vector2 max = new(float.NegativeInfinity, float.NegativeInfinity);
        foreach (Vector3 point in points)
        {
            Vector2 pixel = camera.UnprojectPosition(camera.GlobalTransform * point);
            if (!pixel.IsFinite()) return null;
            min = min.Min(pixel);
            max = max.Max(pixel);
        }
        var size = camera.GetViewport().GetVisibleRect().Size;
        min = min.Clamp(Vector2.Zero, size);
        max = max.Clamp(Vector2.Zero, size);
        return max.X > min.X && max.Y > min.Y ? new Rect2(min, max - min) : null;
    }

    private static IEnumerable<Node> Descendants(Node root)
    {
        if (root == null) yield break;
        yield return root;
        foreach (Node child in root.GetChildren())
            foreach (Node descendant in Descendants(child)) yield return descendant;
    }

    private static IEnumerable<MeshInstance3D> VisualMeshes(Node root)
    {
        foreach (Node child in root.GetChildren())
        {
            if (child is CollisionObject3D) continue;
            if (child is MeshInstance3D mesh) yield return mesh;
            foreach (var descendant in VisualMeshes(child)) yield return descendant;
        }
    }
}
