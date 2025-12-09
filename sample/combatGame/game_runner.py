#!/usr/bin/env python3
"""
Combat Game Battle Royale - Game Runner

A multi-player combat simulation demonstrating Protobus messaging.
"""

import asyncio
import os
import sys

# Add project root to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from protobus import Context, Logger, set_logger

from sample.combatGame.players import (
    Vindicator,
    BullyHunter,
    GiantSlayer,
    Equalizer,
    Wildcard,
    Terminator,
)


class GameLogger:
    """Custom logger that filters debug messages."""

    def info(self, message):
        print(f"[INFO] {message}")

    def warn(self, message):
        print(f"[WARN] {message}")

    def debug(self, message):
        pass  # Suppress debug messages

    def error(self, message):
        print(f"[ERROR] {message}")


async def main():
    """Run the combat game."""
    # Set up custom logger
    set_logger(GameLogger())

    # Get RabbitMQ URL from environment or use default
    amqp_url = os.environ.get("AMQP_URL", "amqp://guest:guest@localhost:5672/")

    print("=" * 60)
    print("     COMBAT GAME BATTLE ROYALE")
    print("=" * 60)
    print()

    # Create context
    context = Context()
    await context.init(amqp_url)

    # Create players
    player_classes = [
        (Vindicator, "player1"),
        (BullyHunter, "player2"),
        (GiantSlayer, "player3"),
        (Equalizer, "player4"),
        (Wildcard, "player5"),
        (Terminator, "player6"),
    ]

    players = []
    for player_class, player_id in player_classes:
        player = player_class(context, player_id)
        context.factory.parse("", player.service_name)
        await player.init()
        players.append(player)

    print("Players entering the arena:")
    for player in players:
        print(f"  - {player.name} ({player.player_id})")
    print()

    # Register all players with each other
    for player in players:
        for other in players:
            if other.player_id != player.player_id:
                player.register_player(other.player_id, other.name, other.health)

    # Set up turn order
    turn_order = [p.player_id for p in players]

    # Initiate game for all players
    for player in players:
        await player.initiateGame({"turnOrder": turn_order}, None, "")

    print("Game started! Turn order:")
    for i, player_id in enumerate(turn_order):
        player = next(p for p in players if p.player_id == player_id)
        print(f"  {i + 1}. {player.name}")
    print()
    print("-" * 60)
    print()

    # Game loop
    max_rounds = 100
    current_round = 0
    game_timeout = 60  # seconds
    start_time = asyncio.get_event_loop().time()

    winner = None

    while current_round < max_rounds:
        current_round += 1

        # Check timeout
        if asyncio.get_event_loop().time() - start_time > game_timeout:
            print("\nGame timed out!")
            break

        # Each player takes a turn
        for player in players:
            if not player.alive:
                continue

            if player.is_my_turn():
                await player.take_turn()

                # Small delay between turns
                await asyncio.sleep(0.05)

                # Check win condition
                alive_players = [p for p in players if p.alive]
                if len(alive_players) == 1:
                    winner = alive_players[0]
                    break
                elif len(alive_players) == 0:
                    print("\nEveryone is dead! It's a draw!")
                    break

        if winner or len([p for p in players if p.alive]) <= 1:
            break

        # Update turn index for next round
        for player in players:
            player.game_state.current_turn_index = (
                player.game_state.current_turn_index + 1
            ) % len(turn_order)

    print()
    print("-" * 60)
    print()

    # Determine winner
    if not winner:
        alive_players = [p for p in players if p.alive]
        if len(alive_players) == 1:
            winner = alive_players[0]

    if winner:
        print(f"WINNER: {winner.name}!")
        await players[0].publish_event("GameOver", {
            "winnerId": winner.player_id,
            "winnerName": winner.name
        })
    else:
        print("No winner - it's a draw!")

    print()
    print("Final Status:")
    print("-" * 40)
    for player in players:
        status = "ALIVE" if player.alive else "DEAD"
        print(f"  {player.name}: {player.health} HP [{status}]")

    print()
    print("=" * 60)

    # Clean up
    await context.close()


if __name__ == "__main__":
    asyncio.run(main())
