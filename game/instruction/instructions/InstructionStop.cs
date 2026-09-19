public sealed class InstructionStop : Instruction
{
    internal void Perform(InstructionManager manager)
    {
        manager.CancelAll();
        Complete();
    }
}
