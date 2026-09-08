"""Strategy 3: always targets the strongest player (highest health)."""

from typing import List, Optional

from ..base_player import BasePlayer, PlayerState


class GiantSlayer(BasePlayer):
    def __init__(self, context, player_id: str) -> None:
        super().__init__(context, player_id, "The Giant Slayer")

    def choose_target(self, alive_players: List[PlayerState]) -> Optional[PlayerState]:
        return max(alive_players, key=lambda p: p.health) if alive_players else None
