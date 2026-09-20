using Godot;

public partial class AiCamera : Camera3D
{
	private Marker3D eye;
	public bool ObservationPrepared { get; set; }
	public override void _Ready()
	{
		var player = GetNode<Player>("../..");
		eye = player.GetNode<Marker3D>("Eye");
		TopLevel = true; // A prepared camera pose must not inherit later player motion.
		var viewport = GetParent<SubViewport>();
		viewport.World3D = player.GetWorld3D();
		viewport.Size = new Vector2I(512, 512);
		viewport.RenderTargetUpdateMode = SubViewport.UpdateMode.Always;
		Current = true;
		SyncToEye();
	}

	public override void _Process(double delta)
	{
		if (!ObservationPrepared) SyncToEye();
	}

	public void SyncToEye()
	{
		if (IsInstanceValid(eye)) GlobalTransform = eye.GlobalTransform;
	}
}
