"""Strategy 4: targets the player whose health is closest to its own."""

from typing import List, Optional

from ..base_player import BasePlayer, PlayerState


class Equalizer(BasePlayer):
    def __init__(self, context, player_id: str) -> None:
        super().__init__(context, player_id, "The Equalizer")

    def choose_target(self, alive_players: List[PlayerState]) -> Optional[PlayerState]:
        return min(alive_players, key=lambda p: abs(p.health - self.health)) if alive_players else None
