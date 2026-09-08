"""Strategy 1: shoots back at whoever shot them last. "An eye for an eye!" """

import random
from typing import List, Optional

from ..base_player import BasePlayer, PlayerState


class Vindicator(BasePlayer):
    def __init__(self, context, player_id: str) -> None:
        super().__init__(context, player_id, "The Vindicator")

    def choose_target(self, alive_players: List[PlayerState]) -> Optional[PlayerState]:
        if not alive_players:
            return None
        attacker = self.game_state.last_attacker
        if attacker:
            for p in alive_players:
                if p.id == attacker:
                    return p
        return random.choice(alive_players)
