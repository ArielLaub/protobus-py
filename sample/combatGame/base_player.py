"""Base player class for the combat game."""

import os
import random
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from protobus import Context, MessageService, ServiceProxy, Logger


@dataclass
class PlayerState:
    """State of a single player."""
    id: str
    name: str
    health: int = 10
    alive: bool = True


@dataclass
class GameState:
    """State of the game."""
    players: Dict[str, PlayerState] = field(default_factory=dict)
    turn_order: List[str] = field(default_factory=list)
    current_turn_index: int = 0
    game_started: bool = False
    game_over: bool = False
    winner: Optional[str] = None
    last_attacker: Optional[str] = None
    focus_target: Optional[str] = None


class BasePlayer(MessageService):
    """
    Abstract base class for combat game players.

    Subclasses must implement the chooseTarget method to define
    their targeting strategy.
    """

    def __init__(self, context: Context, player_id: str, name: str):
        super().__init__(context)
        self._player_id = player_id
        self._name = name
        self._health = 10
        self._alive = True
        self._game_state = GameState()
        self._player_proxies: Dict[str, ServiceProxy] = {}

    @property
    def service_name(self) -> str:
        return f"combat.Player.{self._player_id}"

    @property
    def proto_file_name(self) -> str:
        return os.path.join(os.path.dirname(__file__), "player.proto")

    @property
    def Proto(self) -> str:
        # Return empty string - we're using JSON encoding
        return ""

    @property
    def player_id(self) -> str:
        return self._player_id

    @property
    def name(self) -> str:
        return self._name

    @property
    def health(self) -> int:
        return self._health

    @property
    def alive(self) -> bool:
        return self._alive

    @property
    def game_state(self) -> GameState:
        return self._game_state

    async def init(self) -> None:
        """Initialize the player service."""
        await super().init()

        # Subscribe to game events
        await self.subscribe_event("PlayerJoined", self._on_player_joined)
        await self.subscribe_event("PlayerShot", self._on_player_shot)
        await self.subscribe_event("PlayerDied", self._on_player_died)
        await self.subscribe_event("TurnComplete", self._on_turn_complete)
        await self.subscribe_event("GameOver", self._on_game_over)
        await self.subscribe_event("GameStarted", self._on_game_started)

        Logger.info(f"Player {self._name} ({self._player_id}) initialized")

    def register_player(self, player_id: str, name: str, health: int = 10) -> None:
        """Register another player in the game state."""
        self._game_state.players[player_id] = PlayerState(
            id=player_id,
            name=name,
            health=health,
            alive=True
        )

    async def _get_player_proxy(self, player_id: str) -> ServiceProxy:
        """Get or create a proxy for communicating with another player."""
        if player_id not in self._player_proxies:
            proxy = ServiceProxy(self._context, f"combat.Player.{player_id}")
            await proxy.init()
            self._player_proxies[player_id] = proxy
        return self._player_proxies[player_id]

    async def call_player_method(
        self, player_id: str, method: str, data: Dict[str, Any]
    ) -> Any:
        """Call a method on another player's service."""
        proxy = await self._get_player_proxy(player_id)
        method_func = getattr(proxy, method)
        return await method_func(data)

    # RPC Methods (called by other players)

    async def shoot(
        self, data: Dict[str, Any], actor: Optional[str], correlation_id: str
    ) -> Dict[str, Any]:
        """Handle being shot by another player."""
        shooter_id = data.get("shooterId", "")

        # 50/50 chance to hit
        hit = random.random() < 0.5
        damage = 0

        if hit:
            damage = 1
            self._health -= damage
            self._game_state.last_attacker = shooter_id

            if self._health <= 0:
                self._health = 0
                self._alive = False

        return {
            "hit": hit,
            "damage": damage,
            "remainingHealth": self._health
        }

    async def initiateGame(
        self, data: Dict[str, Any], actor: Optional[str], correlation_id: str
    ) -> Dict[str, Any]:
        """Handle game initiation."""
        turn_order = data.get("turnOrder", [])
        self._game_state.turn_order = turn_order
        self._game_state.game_started = True
        self._game_state.current_turn_index = 0
        return {"success": True}

    async def getStatus(
        self, data: Dict[str, Any], actor: Optional[str], correlation_id: str
    ) -> Dict[str, Any]:
        """Return current player status."""
        return {
            "playerId": self._player_id,
            "name": self._name,
            "health": self._health,
            "alive": self._alive
        }

    # Event handlers

    async def _on_player_joined(self, data: Any, topic: str) -> None:
        """Handle PlayerJoined event."""
        player_id = data.get("playerId", "")
        if player_id != self._player_id:
            self._game_state.players[player_id] = PlayerState(
                id=player_id,
                name=data.get("name", ""),
                health=data.get("health", 10),
                alive=True
            )

    async def _on_player_shot(self, data: Any, topic: str) -> None:
        """Handle PlayerShot event."""
        target_id = data.get("targetId", "")
        if target_id in self._game_state.players:
            self._game_state.players[target_id].health = data.get("targetHealth", 0)

    async def _on_player_died(self, data: Any, topic: str) -> None:
        """Handle PlayerDied event."""
        player_id = data.get("playerId", "")
        if player_id in self._game_state.players:
            self._game_state.players[player_id].alive = False
            self._game_state.players[player_id].health = 0

        if player_id == self._player_id:
            self._alive = False

    async def _on_turn_complete(self, data: Any, topic: str) -> None:
        """Handle TurnComplete event."""
        self._game_state.current_turn_index = data.get("nextPlayerIndex", 0)

    async def _on_game_over(self, data: Any, topic: str) -> None:
        """Handle GameOver event."""
        self._game_state.game_over = True
        self._game_state.winner = data.get("winnerId", "")

    async def _on_game_started(self, data: Any, topic: str) -> None:
        """Handle GameStarted event."""
        self._game_state.turn_order = data.get("turnOrder", [])
        self._game_state.game_started = True

    # Game logic

    def is_my_turn(self) -> bool:
        """Check if it's this player's turn."""
        if not self._game_state.game_started or self._game_state.game_over:
            return False
        if not self._alive:
            return False
        if not self._game_state.turn_order:
            return False

        current_player = self._game_state.turn_order[
            self._game_state.current_turn_index % len(self._game_state.turn_order)
        ]
        return current_player == self._player_id

    def get_alive_opponents(self) -> List[PlayerState]:
        """Get list of alive opponents."""
        return [
            p for p in self._game_state.players.values()
            if p.alive and p.id != self._player_id
        ]

    @abstractmethod
    def choose_target(self, alive_players: List[PlayerState]) -> Optional[PlayerState]:
        """
        Choose a target to shoot.

        Subclasses must implement this method to define their targeting strategy.

        Args:
            alive_players: List of alive opponent players

        Returns:
            The player to target, or None if no valid target
        """
        pass

    async def take_turn(self) -> None:
        """Execute this player's turn."""
        if not self.is_my_turn():
            return

        alive_opponents = self.get_alive_opponents()
        target = self.choose_target(alive_opponents)

        if target:
            Logger.info(f"{self._name} shoots at {target.name}!")

            # Call the target's shoot method
            result = await self.call_player_method(
                target.id, "shoot", {"shooterId": self._player_id}
            )

            hit = result.get("hit", False)
            damage = result.get("damage", 0)
            remaining_health = result.get("remainingHealth", 0)

            if hit:
                Logger.info(
                    f"  HIT! {target.name} takes {damage} damage "
                    f"({remaining_health} HP remaining)"
                )
            else:
                Logger.info(f"  MISS!")

            # Publish shot event
            await self.publish_event("PlayerShot", {
                "shooterId": self._player_id,
                "targetId": target.id,
                "hit": hit,
                "damage": damage,
                "targetHealth": remaining_health
            })

            # Check if target died
            if remaining_health <= 0:
                Logger.info(f"  {target.name} has been eliminated!")
                await self.publish_event("PlayerDied", {
                    "playerId": target.id,
                    "killerId": self._player_id
                })

        await self.end_turn()

    async def end_turn(self) -> None:
        """End this player's turn."""
        # Calculate next player index (skip dead players)
        next_index = (self._game_state.current_turn_index + 1) % len(
            self._game_state.turn_order
        )

        # Find next alive player
        attempts = 0
        while attempts < len(self._game_state.turn_order):
            next_player_id = self._game_state.turn_order[next_index]
            if next_player_id == self._player_id:
                # It's us, we're alive
                break
            if next_player_id in self._game_state.players:
                if self._game_state.players[next_player_id].alive:
                    break
            next_index = (next_index + 1) % len(self._game_state.turn_order)
            attempts += 1

        await self.publish_event("TurnComplete", {
            "playerId": self._player_id,
            "nextPlayerIndex": next_index
        })

    def check_win_condition(self) -> Optional[str]:
        """Check if the game is over and return winner ID if so."""
        alive_players = [
            p for p in self._game_state.players.values() if p.alive
        ]

        # Add ourselves if alive
        if self._alive:
            all_alive = [self._player_id] + [p.id for p in alive_players]
        else:
            all_alive = [p.id for p in alive_players]

        # Deduplicate
        all_alive = list(set(all_alive))

        if len(all_alive) == 1:
            return all_alive[0]
        elif len(all_alive) == 0:
            return None  # Draw?

        return None  # Game continues
