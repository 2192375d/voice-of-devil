using Godot;
using System;
using System.Collections.Concurrent;

public partial class GameApiServer : Node
{
	[Export] public string BindAddress = "0.0.0.0";
	[Export] public int Port = 3000;
	[Export] public string[] AllowedOrigins = Array.Empty<string>();

	private readonly ConcurrentQueue<string> messages = new();
	private GameRequestInbox inbox;
	private GameHttpTransport transport;
	private GameObservationService observations;
	private double simulationTime;

	public override void _Ready()
	{
		ProcessPhysicsPriority = -1000;
		inbox = new GameRequestInbox();
		observations = new GameObservationService(inbox, GetPlayer, () => simulationTime);
		AddChild(new GameObservationPump { Prepare = observations.Prepare });
		string bind = System.Environment.GetEnvironmentVariable("VOD_API_BIND") ?? BindAddress;
		string portSetting = System.Environment.GetEnvironmentVariable("VOD_API_PORT");
		string originsSetting = System.Environment.GetEnvironmentVariable("VOD_API_ORIGINS");
		try
		{
			int port = portSetting == null ? Port : int.Parse(portSetting);
			string[] origins = originsSetting == null ? AllowedOrigins
				: originsSetting.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
			transport = new GameHttpTransport(inbox, origins, messages.Enqueue);
			transport.Start(bind, port);
			GD.Print($"Game API listening at http://{bind}:{port}/api/v1/commands");
		}
		catch (Exception exception)
		{
			transport?.Dispose();
			inbox.Shutdown();
			GD.PushError($"Game API could not start: {exception.Message}");
		}
	}

	public override void _PhysicsProcess(double delta)
	{
		simulationTime += delta;
		var player = GetPlayer();
		inbox.Drain(player != null, command => GameInstructionBridge.Execute(player.Instructions, command), observations.Request);
	}

	public override void _Process(double delta)
	{
		while (messages.TryDequeue(out string message)) GD.PushWarning(message);
	}

	private Player GetPlayer()
	{
		var player = GetNodeOrNull<Cache>("/root/Cache")?.Player;
		return IsInstanceValid(player) && !player.IsQueuedForDeletion() && player.IsInsideTree()
			&& player.Instructions != null ? player : null;
	}

	public override void _ExitTree()
	{
		observations?.Dispose();
		transport?.Dispose();
		inbox?.Shutdown();
	}
}
