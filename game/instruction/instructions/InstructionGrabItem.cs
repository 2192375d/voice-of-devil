public sealed class InstructionGrabItem : Instruction
{
    public InstructionRequestResult Result { get; private set; }

    internal InstructionRequestResult Perform(IInstructionTarget target)
    {
        Result = target.TryGrabItem();
        if (Result == InstructionRequestResult.PickedUp)
            Complete();
        else
            Fail();
        return Result;
    }
}
