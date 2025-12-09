"""GiantSlayer player strategy."""

from typing import List, Optional

from protobus import Context

from ..base_player import BasePlayer, PlayerState


class GiantSlayer(BasePlayer):
    """
    Strategy 3: The Giant Slayer

    Always targets the strongest player (highest health).

    "The bigger they are, the harder they fall!"
    """

    def __init__(self, context: Context, player_id: str):
        super().__init__(context, player_id, "The Giant Slayer")

    def choose_target(self, alive_players: List[PlayerState]) -> Optional[PlayerState]:
        if not alive_players:
            return None

        # Find player with highest health
        return max(alive_players, key=lambda p: p.health)
