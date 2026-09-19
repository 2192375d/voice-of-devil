public sealed class InstructionDropItem : Instruction
{
    public InstructionRequestResult Result { get; private set; }

    internal InstructionRequestResult Perform(IInstructionTarget target)
    {
        Result = target.TryDropItem();
        if (Result == InstructionRequestResult.Dropped)
            Complete();
        else
            Fail();
        return Result;
    }
}
