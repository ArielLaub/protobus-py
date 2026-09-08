"""
The player every strategy extends.

Each player is its own protobus service, addressed by an instance name
(``Combat.Player.player3``) that shares the one ``Combat.Player`` contract in
player.proto. Players shoot at each other over RPC and keep their picture of
the arena up to date from events.
"""

import os
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from protobus import Context, Logger, MessageService, MessageServiceOptions, RetryOptions, ServiceProxy


@dataclass
class PlayerState:
    id: str
    name: str
    health: int = 10
    alive: bool = True


@dataclass
class GameState:
    players: Dict[str, PlayerState] = field(default_factory=dict)
    player_order: List[str] = field(default_factory=list)
    my_index: int = -1
    last_attacker: Optional[str] = None
    focus_target: Optional[str] = None
    game_started: bool = False
    game_over: bool = False


class BasePlayer(MessageService):
    def __init__(self, context: Context, player_id: str, player_name: str) -> None:
        # One request at a time: a player's turn is a sequence of calls, and a
        # second shot arriving mid-turn would interleave the two.
        super().__init__(context, MessageServiceOptions(max_concurrent=1, retry=RetryOptions(max_retries=0)))
        self.player_id = player_id
        self.player_name = player_name
        self.health = 10
        self.game_state = GameState()
        self._proxies: Dict[str, ServiceProxy] = {}

    @property
    def service_name(self) -> str:
        return f"Combat.Player.{self.player_id}"

    @property
    def proto_file_name(self) -> str:
        return os.path.join(os.path.dirname(__file__), "player.proto")

    async def init(self) -> None:
        await super().init()
        await self.subscribe_event("Combat.PlayerShot", self.on_player_shot)
        await self.subscribe_event("Combat.PlayerDied", self.on_player_died)
        await self.subscribe_event("Combat.TurnComplete", self.on_turn_complete)
        await self.subscribe_event("Combat.GameOver", self.on_game_over)
        await self.subscribe_event("Combat.GameStarted", self.on_game_started)
        await self.subscribe_event("Combat.PlayerJoined", self.on_player_joined)
        await self.publish_event("Combat.PlayerJoined", {"playerId": self.player_id, "playerName": self.player_name, "health": self.health})
        Logger.info(f"{self.player_name} ({self.player_id}) joined the game")

    # -- RPC: what other players call on us ------------------------------------

    async def shoot(self, request: dict, actor: str, correlation_id: str) -> dict:
        if self.health <= 0:
            return {"hit": False, "remainingHealth": 0}
        hit = random.random() < 0.5
        shooter = self.game_state.players.get(request["shooterId"])
        shooter_name = shooter.name if shooter else request["shooterId"]
        if hit:
            self.health -= 1
            self.game_state.last_attacker = request["shooterId"]
            Logger.info(f"{self.player_name} was hit by {shooter_name}! Health: {self.health}")
        else:
            Logger.info(f"{self.player_name} dodged attack from {shooter_name}!")
        await self.publish_event("Combat.PlayerShot", {
            "shooterId": request["shooterId"], "targetId": self.player_id, "hit": hit, "targetHealth": self.health,
        })
        if hit and self.health <= 0:
            Logger.info(f"{self.player_name} has been eliminated!")
            await self.publish_event("Combat.PlayerDied", {"playerId": self.player_id, "killedBy": request["shooterId"]})
        return {"hit": hit, "remainingHealth": self.health}

    async def initiateGame(self, request: dict, actor: str, correlation_id: str) -> dict:
        self.game_state.player_order = list(request["playerOrder"])
        self.game_state.my_index = request["myIndex"]
        self.game_state.game_started = True
        Logger.info(f"{self.player_name} received game initiation. Order: {', '.join(request['playerOrder'])}, My turn index: {request['myIndex']}")
        if request["myIndex"] == 0:
            await self.take_turn()
        return {"success": True}

    async def getStatus(self, request: dict, actor: str, correlation_id: str) -> dict:
        return {"playerId": self.player_id, "playerName": self.player_name, "health": self.health, "alive": self.health > 0}

    # -- events -----------------------------------------------------------------

    async def on_player_joined(self, event: dict, event_type: str, topic: str) -> None:
        if event["playerId"] != self.player_id:
            self.game_state.players[event["playerId"]] = PlayerState(event["playerId"], event["playerName"], event["health"], True)
            Logger.debug(f"{self.player_name} knows about {event['playerName']}")

    def register_player(self, player_id: str, player_name: str, health: int = 10) -> None:
        """Register another player by hand, for late joiners that missed the event."""
        if player_id != self.player_id:
            self.game_state.players[player_id] = PlayerState(player_id, player_name, health, health > 0)

    async def on_player_shot(self, event: dict, event_type: str, topic: str) -> None:
        player = self.game_state.players.get(event["targetId"])
        if player is not None:
            player.health = event["targetHealth"]
            player.alive = event["targetHealth"] > 0
        if event["targetId"] == self.player_id and event["hit"]:
            self.game_state.last_attacker = event["shooterId"]

    async def on_player_died(self, event: dict, event_type: str, topic: str) -> None:
        player = self.game_state.players.get(event["playerId"])
        if player is not None:
            player.alive = False
            player.health = 0
        if self.game_state.focus_target == event["playerId"]:
            self.game_state.focus_target = None

    async def on_turn_complete(self, event: dict, event_type: str, topic: str) -> None:
        if self.game_state.game_over or self.health <= 0:
            return
        order = self.game_state.player_order
        if self.player_id in order and order.index(self.player_id) == event["nextPlayerIndex"]:
            await self.take_turn()

    async def on_game_started(self, event: dict, event_type: str, topic: str) -> None:
        self.game_state.player_order = list(event["playerOrder"])
        self.game_state.game_started = True

    async def on_game_over(self, event: dict, event_type: str, topic: str) -> None:
        self.game_state.game_over = True
        if event["winnerId"] == self.player_id:
            Logger.info(f"🏆 {self.player_name} WINS THE GAME! 🏆")

    def is_winner(self) -> bool:
        return self.health > 0 and self.game_state.game_over

    # -- turns --------------------------------------------------------------------

    async def take_turn(self) -> None:
        if self.game_state.game_over or self.health <= 0:
            return
        alive = self.get_alive_players()
        if not self.game_state.players:
            Logger.warn(f"{self.player_name} doesn't know about other players yet, ending turn")
            await self.end_turn()
            return
        if not alive:
            Logger.info(f"{self.player_name} is the last one standing!")
            await self.publish_event("Combat.GameOver", {"winnerId": self.player_id, "winnerName": self.player_name})
            return

        target = self.choose_target(alive)
        if target is None:
            Logger.warn(f"{self.player_name} couldn't find a target!")
            await self.end_turn()
            return

        Logger.info(f"{self.player_name} shoots at {target.name}!")
        try:
            result = await self.call_player_method(target.id, "shoot", {"shooterId": self.player_id})
            if result["remainingHealth"] <= 0:
                known = self.game_state.players.get(target.id)
                if known is not None:
                    known.health = 0
                    known.alive = False
        except Exception as err:
            Logger.error(f"{self.player_name} failed to shoot: {err}")

        if not self.get_alive_players():
            Logger.info(f"{self.player_name} is the last one standing!")
            await self.publish_event("Combat.GameOver", {"winnerId": self.player_id, "winnerName": self.player_name})
            return
        await self.end_turn()

    async def end_turn(self) -> None:
        await self.publish_event("Combat.TurnComplete", {"playerId": self.player_id, "nextPlayerIndex": self.get_next_alive_player_index()})

    def get_alive_players(self) -> List[PlayerState]:
        return [p for p in self.game_state.players.values() if p.alive and p.id != self.player_id]

    def get_next_alive_player_index(self) -> int:
        order = self.game_state.player_order
        my_index = order.index(self.player_id)
        for i in range(1, len(order) + 1):
            next_index = (my_index + i) % len(order)
            next_id = order[next_index]
            if next_id == self.player_id:
                continue
            player = self.game_state.players.get(next_id)
            if player is not None and player.alive:
                return next_index
        return (my_index + 1) % len(order)

    async def call_player_method(self, target_player_id: str, method: str, data: Any) -> Any:
        """
        Call a method on another player. A ServiceProxy built for the target's
        instance name routes to that player's queue while the envelope names
        the shared Combat.Player contract.
        """
        proxy = self._proxies.get(target_player_id)
        if proxy is None:
            proxy = ServiceProxy(self.context, f"Combat.Player.{target_player_id}")
            await proxy.init()
            self._proxies[target_player_id] = proxy
        return await getattr(proxy, method)(data, self.player_id)

    def choose_target(self, alive_players: List[PlayerState]) -> Optional[PlayerState]:
        """Each strategy decides differently."""
        raise NotImplementedError
