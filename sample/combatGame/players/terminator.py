"""Strategy 6: picks one target and keeps at it until they are gone."""

import random
from typing import List, Optional

from ..base_player import BasePlayer, PlayerState


class Terminator(BasePlayer):
    def __init__(self, context, player_id: str) -> None:
        super().__init__(context, player_id, "The Terminator")

    def choose_target(self, alive_players: List[PlayerState]) -> Optional[PlayerState]:
        if not alive_players:
            return None
        focus = self.game_state.focus_target
        if focus:
            for p in alive_players:
                if p.id == focus:
                    return p
            self.game_state.focus_target = None
        target = random.choice(alive_players)
        self.game_state.focus_target = target.id
        return target
