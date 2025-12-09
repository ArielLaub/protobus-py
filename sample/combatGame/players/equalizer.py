"""Equalizer player strategy."""

from typing import List, Optional

from protobus import Context

from ..base_player import BasePlayer, PlayerState


class Equalizer(BasePlayer):
    """
    Strategy 4: The Equalizer

    Targets the player with health most similar to their own.

    "Let's keep things fair and square!"
    """

    def __init__(self, context: Context, player_id: str):
        super().__init__(context, player_id, "The Equalizer")

    def choose_target(self, alive_players: List[PlayerState]) -> Optional[PlayerState]:
        if not alive_players:
            return None

        # Find player with health closest to our own
        closest: Optional[PlayerState] = None
        smallest_diff = float("inf")

        for player in alive_players:
            diff = abs(player.health - self.health)
            if diff < smallest_diff:
                smallest_diff = diff
                closest = player

        return closest
