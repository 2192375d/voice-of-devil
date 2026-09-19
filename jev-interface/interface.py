from dotenv import load_dotenv
import os
import asyncio
from backboard import BackboardClient
from pydantic import BaseModel, Field

load_dotenv()
secret_key = os.getenv('BACKBOARD_APIKEY')


questions = {
    "speed": {
        "type": "noul",
        "instructions": "Speed for moving if moving is enabled",
    },
    "X-dir": {
        "type": "choice",
        "instructions": "What X compass heading is described? 0 = north, 90 = east, 180 = south, 270 = west.",
        "criteria": {str(a): f"{a} degrees" for a in list(range(0, 360, 2))},
    },
    "Y-dir": {
        "type": "choice",
        "instructions": "What Y compass heading is described? 0 = middle, 90 = top, -90 = down (only relevant if observe_action is over 0.5)",
        "criteria": {str(a): f"{a} degrees" for a in list(range(-90, 90, 2))},
    },
    "movement_action": {
        "type": "choice",
        "instructions": "What type of action is this?",
        "criteria": {
            "walking": "Move forwards",
            "stopped": "Stop moving forwards",
        },
    },
    "observe_action": {
        "type": "noul",
        "instructions": "Should we move the camera point?",
    },
}

async def main():
    client = BackboardClient(api_key=secret_key)
    
    # Send a message — thread and assistant are auto-created
    response = await client.send_message(
        "Game status: position at (0,12), camera at (45, 0), target at (130,12)",
        memory="Auto",
        llm_provider="typesafe",
        model_name="jev-latest",
        stream=False,
        system_one= { "questions" : questions }
    )
    print(response.content)

asyncio.run(main())
