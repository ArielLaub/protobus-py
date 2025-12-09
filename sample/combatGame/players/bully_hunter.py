"""BullyHunter player strategy."""

from typing import List, Optional

from protobus import Context

from ..base_player import BasePlayer, PlayerState


class BullyHunter(BasePlayer):
    """
    Strategy 2: The Bully Hunter

    Always targets the weakest player (lowest health).

    "Pick on someone your own size!"
    """

    def __init__(self, context: Context, player_id: str):
        super().__init__(context, player_id, "The Bully Hunter")

    def choose_target(self, alive_players: List[PlayerState]) -> Optional[PlayerState]:
        if not alive_players:
            return None

        # Find player with lowest health
        return min(alive_players, key=lambda p: p.health)
