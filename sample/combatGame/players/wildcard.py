"""Strategy 5: a random target every time. "Chaos is a ladder... or something!" """

import random
from typing import List, Optional

from ..base_player import BasePlayer, PlayerState


class Wildcard(BasePlayer):
    def __init__(self, context, player_id: str) -> None:
        super().__init__(context, player_id, "The Wildcard")

    def choose_target(self, alive_players: List[PlayerState]) -> Optional[PlayerState]:
        return random.choice(alive_players) if alive_players else None
