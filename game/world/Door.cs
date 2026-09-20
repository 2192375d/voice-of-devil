using Godot;

/// <summary>A door that owns its hinge motion, independently of player instructions.</summary>
public partial class Door : AnimatableBody3D
{
    public const string GroupName = "interactable_doors";
    [Export] public float OpenAngleDegrees = 90.0f;
    [Export(PropertyHint.Range, "0.01,10,0.01")] public double MotionDuration = 0.6;
    [Export] public Vector3 HingePosition = new(-0.044947147f, 0, -0.4619959f);

    [Export] public bool PlateControlled = false;
    [Export] public PressurePlate ActivationPlate;

    public bool IsOpen { get; private set; }
    public bool IsMoving { get; private set; }
    public Vector3 GetInteractionPosition(Vector3 origin)
    {
        var halfSize = ((BoxShape3D)collider.Shape).Size * 0.5f;
        Vector3 local = collider.ToLocal(origin);
        return collider.ToGlobal(new Vector3(
            Mathf.Clamp(local.X, -halfSize.X, halfSize.X),
            Mathf.Clamp(local.Y, -halfSize.Y, halfSize.Y),
            Mathf.Clamp(local.Z, -halfSize.Z, halfSize.Z)));
    }
    private CollisionShape3D collider;
    private Transform3D closedTransform;
    private double progress;
    private bool opening;


    public override void _Ready()
    {
        collider = GetNode<CollisionShape3D>("CollisionShape3D");
        closedTransform = Transform;
        // Motion is applied directly in _PhysicsProcess, not by a render-frame tween.
        SyncToPhysics = false;
        AddToGroup(GroupName);
    }

    public InstructionRequestResult TryInteract()
    {
        if (PlateControlled)
            return InstructionRequestResult.PlateControlled;
        if (IsMoving)
            return InstructionRequestResult.Busy;
        if (!HasValidMotion())
            return InstructionRequestResult.InvalidArguments;
        opening = !IsOpen;
        IsMoving = true;
        return opening ? InstructionRequestResult.Opening : InstructionRequestResult.Closing;
    }

    private bool HasValidMotion() => double.IsFinite(MotionDuration) && MotionDuration > 0
        && float.IsFinite(OpenAngleDegrees) && OpenAngleDegrees != 0 && HingePosition.IsFinite();

    public override void _PhysicsProcess(double delta)
    {
        if (PlateControlled)
        {
            opening = IsInstanceValid(ActivationPlate) && ActivationPlate.IsInsideTree()
                && !ActivationPlate.IsQueuedForDeletion() && ActivationPlate.IsPressed;
            IsMoving = opening ? progress < 1 : progress > 0;
        }
        if (!IsMoving || !HasValidMotion())
            return;
        progress = System.Math.Clamp(progress + (opening ? delta : -delta) / MotionDuration, 0, 1);
        float angle = Mathf.DegToRad(OpenAngleDegrees) * (float)progress;
        var rotation = new Basis(Vector3.Up, angle);
        // Compose with the authored pose so scale and hinge remain fixed, even when reversing.
        Transform = closedTransform * new Transform3D(rotation, HingePosition - rotation * HingePosition);
        IsOpen = progress == 1;
        IsMoving = progress > 0 && progress < 1;
        if (progress == 0)
            Transform = closedTransform;
    }
}
