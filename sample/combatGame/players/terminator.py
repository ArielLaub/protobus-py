"""Terminator player strategy."""

import random
from typing import List, Optional

from protobus import Context

from ..base_player import BasePlayer, PlayerState


class Terminator(BasePlayer):
    """
    Strategy 6: The Terminator

    Picks one target and relentlessly attacks until they're dead, then moves on.

    "I'll be back... for you specifically!"
    """

    def __init__(self, context: Context, player_id: str):
        super().__init__(context, player_id, "The Terminator")

    def choose_target(self, alive_players: List[PlayerState]) -> Optional[PlayerState]:
        if not alive_players:
            return None

        # If we have a focus target and they're still alive, keep shooting
        if self.game_state.focus_target:
            target = next(
                (p for p in alive_players if p.id == self.game_state.focus_target),
                None
            )
            if target:
                return target
            # Focus target is dead, clear it
            self.game_state.focus_target = None

        # Pick a new focus target (randomly)
        new_target = random.choice(alive_players)
        self.game_state.focus_target = new_target.id
        return new_target
