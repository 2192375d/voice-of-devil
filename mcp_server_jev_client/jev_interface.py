from dotenv import load_dotenv
import json
import os
import asyncio
from backboard import BackboardClient
from pydantic import BaseModel, Field

SYSTEM_PROMPT = """
You are playing as an AI controlled in a game world where the objective of the game is to solve puzzles. You must follow the operator's commands and respond with the weight of each action according to the operator's commands and world state.
"""

load_dotenv()
secret_key = os.getenv('BACKBOARD_APIKEY')

questions = {
    "x_dir": {
        "type": "choice",
        "instructions": "What X compass heading is described? 0 = north, 90 = east, 180 = south, 270 = west.",
        "criteria": {str(a): f"{a} degrees" for a in range(0, 360, 2)},
    },
    "y_dir": {
        "type": "choice",
        "instructions": "What Y compass heading is described? 0 = middle, 90 = top, -90 = down (only relevant if observe_action is over 0.5)",
        "criteria": {str(a): f"{a} degrees" for a in range(-90, 90, 2)},
    },
    "z_dir": {
        "type": "choice",
        "instructions": "What Z rotation (roll) is described? 0 = level, 90 = roll right, 270 = roll left.",
        "criteria": {str(a): f"{a} degrees" for a in range(0, 360, 2)},
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

class JevInterface(object):
    def __init__(self):
        self.context = SYSTEM_PROMPT
        self.client = BackboardClient(api_key=secret_key)
        # TODO: decide whether we want to store context of previous chats too

    async def send_req(self, user_request="", game_state_request=""):
        # Send a message — thread and assistant are auto-created
        self.context += f"""
        # USER REQUEST:
        {user_request}
        # GAME STATUS:
        {game_state_request}
        """
        response = await self.client.send_message(
            self.context,
            system_prompt=SYSTEM_PROMPT,
            llm_provider="typesafe",
            model_name="jev-latest",
            stream=False,
            system_one={
                "state": {
                    "user_request": user_request,
                    "game_status": game_state_request,
                },
                "questions": questions,
            },
        )
    
        val = json.loads(response.content)
        x_dirs = val['x_dir']['probabilities']
        y_dirs = val['y_dir']['probabilities']
        z_dirs = val['z_dir']['probabilities']
        movement_actions = val['movement_action']['probabilities']

        tgt_x_dir = max(x_dirs, key=x_dirs.get)
        tgt_x_dir_prob = x_dirs[tgt_x_dir]

        tgt_y_dir = max(y_dirs, key=y_dirs.get)
        tgt_y_dir_prob = y_dirs[tgt_y_dir]

        tgt_z_dir = max(z_dirs, key=z_dirs.get)
        tgt_z_dir_prob = z_dirs[tgt_z_dir]

        tgt_movement_actions = max(movement_actions, key=movement_actions.get)
        tgt_movement_actions_prob = movement_actions[tgt_movement_actions]

        tgt_observe_noul = val['observe_action']['noul']
        self.context += f"""
        ### AI Response (Highest Scores with Probability)
        X_DIR: {tgt_x_dir} ({tgt_x_dir_prob})
        Y_DIR: {tgt_y_dir} ({tgt_y_dir_prob})
        Z_DIR: {tgt_z_dir} ({tgt_z_dir_prob})
        Movement_Actions: {tgt_movement_actions} ({tgt_movement_actions_prob})
        Observe: {tgt_observe_noul}
        """
        # get the highest actions of each relevant area
        return {
            "x_dir": (tgt_x_dir, tgt_x_dir_prob),
            "y_dir": (tgt_y_dir, tgt_y_dir_prob),
            "z_dir": (tgt_z_dir, tgt_z_dir_prob),
            "movement_actions": (tgt_movement_actions, tgt_movement_actions_prob),
            "observe_action": (tgt_observe_noul)
        }
    
    #asyncio.run(main())
    
