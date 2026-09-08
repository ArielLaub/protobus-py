#!/usr/bin/env python3
"""
Combat Game — a battle royale between six protobus services.

Every player is a service of its own, sharing one .proto contract under an
instance name; they shoot at each other over RPC and follow the game through
events. Run it with a broker up::

    docker compose up -d
    python -m sample.combatGame.game_runner
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from protobus import Context, LogLevel, set_log_level, set_logger  # noqa: E402

from sample.combatGame.players import BullyHunter, Equalizer, GiantSlayer, Terminator, Vindicator, Wildcard  # noqa: E402

AMQP_URL = os.environ.get("AMQP_URL", "amqp://guest:guest@localhost:5672/")
PROTO_DIR = os.path.dirname(os.path.abspath(__file__))


class GameLogger:
    def info(self, message):
        print(f"[INFO] {message}")

    def warn(self, message):
        print(f"[WARN] {message}")

    def debug(self, message):
        pass

    def error(self, message):
        print(f"[ERROR] {message}")


async def run_game() -> int:
    set_logger(GameLogger())
    set_log_level(LogLevel.Info)

    print("=" * 60)
    print("🎮 COMBAT GAME - Battle Royale! 🎮")
    print("=" * 60)
    print()

    context = Context()
    await context.init(AMQP_URL, [PROTO_DIR])

    players = [
        Vindicator(context, "player1"),
        BullyHunter(context, "player2"),
        GiantSlayer(context, "player3"),
        Equalizer(context, "player4"),
        Wildcard(context, "player5"),
        Terminator(context, "player6"),
    ]

    print("Players joining the arena:")
    print("-" * 40)
    for player in players:
        await player.init()
        print(f"  ⚔️  {player.player_name} ({player.player_id})")
    print("-" * 40)
    print()

    # Register every player with every other, so nobody depends on having
    # seen the PlayerJoined events that fired before it subscribed.
    for player in players:
        for other in players:
            if player is not other:
                player.register_player(other.player_id, other.player_name)

    player_order = [p.player_id for p in players]
    print(f"Turn order: {' -> '.join(player_order)}")
    print()
    print("=" * 60)
    print("🔔 LET THE BATTLE BEGIN! 🔔")
    print("=" * 60)
    print()

    # Index 0 is initiated LAST because its initiateGame takes the first turn
    # inline, and that turn ends by publishing TurnComplete. Every other player
    # must already know the turn order by then.
    for i in range(len(players) - 1, -1, -1):
        await players[i].initiateGame({"playerOrder": player_order, "myIndex": i}, "runner", "init")

    deadline = asyncio.get_running_loop().time() + 60
    while asyncio.get_running_loop().time() < deadline:
        if any(p.is_winner() for p in players):
            break
        await asyncio.sleep(0.01)

    print()
    print("=" * 60)
    print("📊 FINAL RESULTS 📊")
    print("=" * 60)
    winners = 0
    for player in players:
        status = await player.getStatus({}, "runner", "status")
        icon = "👑" if status["alive"] else "💀"
        outcome = "(WINNER!)" if status["alive"] else "(eliminated)"
        winners += int(status["alive"])
        print(f"  {icon} {status['playerName']}: {status['health']} HP {outcome}")
    print("=" * 60)
    print()

    for player in players:
        await player.close()
    await context.close()
    print("Game ended. Goodbye!")
    return 0 if winners == 1 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_game()))
