"""Strategy 2: always targets the weakest player (lowest health)."""

from typing import List, Optional

from ..base_player import BasePlayer, PlayerState


class BullyHunter(BasePlayer):
    def __init__(self, context, player_id: str) -> None:
        super().__init__(context, player_id, "The Bully Hunter")

    def choose_target(self, alive_players: List[PlayerState]) -> Optional[PlayerState]:
        return min(alive_players, key=lambda p: p.health) if alive_players else None
