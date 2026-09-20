public sealed class InstructionInteract : Instruction
{
    public InstructionRequestResult Result { get; private set; }

    internal InstructionRequestResult Perform(IInstructionTarget target)
    {
        Result = target.TryInteract();
        if (Result is InstructionRequestResult.Opening or InstructionRequestResult.Closing)
            Complete();
        else
            Fail();
        return Result;
    }
}
