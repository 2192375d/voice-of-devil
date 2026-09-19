public sealed class InstructionMoveForward : SustainedAction
{
    protected override void Update(double delta, IInstructionTarget target)
        => target.CommandWalkForward();
}
