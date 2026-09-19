using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;

public sealed class PendingGameCall
{
    internal readonly TaskCompletionSource<GameCommandResult> Completion = new(TaskCreationOptions.RunContinuationsAsynchronously);
    internal bool Dispatched;
    internal DateTimeOffset Created;
    public long Sequence { get; internal init; }
    public string RequestId { get; internal init; }
    public GameCommand Command { get; internal init; }
    public Task<GameCommandResult> Task => Completion.Task;
}

/// <summary>Transport-owned inbox. Only Drain invokes the game, on its physics thread.</summary>
public sealed class GameRequestInbox
{
    private readonly object gate = new();
    private readonly List<PendingGameCall> calls = new();
    private readonly Func<DateTimeOffset> clock;
    private readonly int capacity;
    private readonly TimeSpan timeout;
    private long sequence;
    private bool closed;

    public GameRequestInbox(int capacity = 256, TimeSpan? timeout = null, Func<DateTimeOffset> clock = null)
    {
        this.capacity = capacity;
        this.timeout = timeout ?? TimeSpan.FromSeconds(10);
        this.clock = clock ?? (() => DateTimeOffset.UtcNow);
    }

    public int PendingActionCount
    {
        get { lock (gate) return calls.Count(c => !c.Dispatched && c.Command.IsAction); }
    }

    public PendingGameCall Submit(string requestId, GameCommand command)
    {
        lock (gate)
        {
            ExpireLocked();
            var call = new PendingGameCall { RequestId = requestId, Command = command, Sequence = ++sequence, Created = clock() };
            string error = closed ? "shutdown" : calls.Any(c => c.RequestId == requestId) ? "duplicate_request"
                : calls.Count >= capacity ? "queue_full" : null;
            if (error != null)
                call.Completion.SetResult(GameCommandResult.Status(error, true));
            else
                calls.Add(call);
            return call;
        }
    }

    public void Complete(PendingGameCall call, GameCommandResult result)
    {
        lock (gate)
        {
            if (calls.Remove(call)) call.Completion.TrySetResult(result);
        }
    }

    public void Expire() { lock (gate) ExpireLocked(); }

    private void ExpireLocked()
    {
        foreach (var call in calls.ToArray())
            if ((!call.Dispatched || call.Command.Name == "observe") && clock() - call.Created >= timeout)
                Complete(call, GameCommandResult.Status(call.Dispatched ? "observation_error" : "expired", true,
                    "Request deadline exceeded."));
    }

    public void Drain(bool ready, Func<GameCommand, GameCommandResult> execute,
        Action<PendingGameCall> observe, int ordinaryBudget = 64)
    {
        // Snapshot the tick's work. New arrivals wait until the next tick.
        PendingGameCall[] batch;
        lock (gate)
        {
            ExpireLocked();
            var pending = calls.Where(c => !c.Dispatched).ToArray();
            batch = pending.Where(c => c.Command.IsControl)
                .Concat(pending.Where(c => !c.Command.IsControl).Take(ordinaryBudget)).ToArray();
        }
        foreach (var call in batch)
        {
            lock (gate)
            {
                ExpireLocked();
                if (!calls.Contains(call)) continue;
                call.Dispatched = true;
                if (call.Command.Name == "clear_queue")
                {
                    var discarded = calls.Where(c => !c.Dispatched && c.Sequence < call.Sequence && c.Command.IsAction).ToArray();
                    foreach (var old in discarded) Complete(old, GameCommandResult.Status("cancelled", true, "Cleared from the request inbox."));
                    Complete(call, new GameCommandResult(new { status = "cleared", cleared_count = discarded.Length }));
                    continue;
                }
            }
            if (!ready)
            {
                Complete(call, GameCommandResult.Status("not_ready", true));
                continue;
            }
            try
            {
                if (call.Command.Name == "observe") observe(call);
                else Complete(call, execute(call.Command));
            }
            catch (Exception)
            {
                Complete(call, GameCommandResult.Status("execution_error", true, "The game could not execute the request."));
            }
        }
    }

    public void Shutdown()
    {
        lock (gate)
        {
            closed = true;
            foreach (var call in calls.ToArray()) Complete(call, GameCommandResult.Status("shutdown", true));
        }
    }
}
