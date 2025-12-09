"""Wildcard player strategy."""

import random
from typing import List, Optional

from protobus import Context

from ..base_player import BasePlayer, PlayerState


class Wildcard(BasePlayer):
    """
    Strategy 5: The Wildcard

    Picks a random target every time.

    "Chaos is a ladder... or something!"
    """

    def __init__(self, context: Context, player_id: str):
        super().__init__(context, player_id, "The Wildcard")

    def choose_target(self, alive_players: List[PlayerState]) -> Optional[PlayerState]:
        if not alive_players:
            return None

        return random.choice(alive_players)
