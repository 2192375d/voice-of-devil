public sealed class InstructionMoveForward : InstructionSustained
{
	protected override void Update(double delta, IInstructionTarget target)
		=> target.CommandWalkForward();
}
