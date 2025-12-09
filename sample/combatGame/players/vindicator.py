"""Vindicator player strategy."""

import random
from typing import List, Optional

from protobus import Context

from ..base_player import BasePlayer, PlayerState


class Vindicator(BasePlayer):
    """
    Strategy 1: The Vindicator

    Shoots back at whoever shot them last. If nobody has attacked them yet,
    picks randomly.

    "An eye for an eye!"
    """

    def __init__(self, context: Context, player_id: str):
        super().__init__(context, player_id, "The Vindicator")

    def choose_target(self, alive_players: List[PlayerState]) -> Optional[PlayerState]:
        if not alive_players:
            return None

        # If someone attacked us, shoot them back
        if self.game_state.last_attacker:
            attacker = next(
                (p for p in alive_players if p.id == self.game_state.last_attacker),
                None
            )
            if attacker:
                return attacker

        # Otherwise, pick randomly
        return random.choice(alive_players)
