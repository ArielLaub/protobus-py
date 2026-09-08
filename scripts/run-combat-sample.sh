#!/usr/bin/env bash
#
# Runs the combat-game sample end to end against a live RabbitMQ and asserts
# the battle royale terminated correctly: exactly one player left standing.
#
# This is the one exercise of the framework as a consumer sees it — proto
# loading from disk, instance-named services, RPC, pub/sub events and shutdown
# in one process — so a regression the unit and integration suites miss shows
# up here as "no winner" or "several winners".
#
# Env:
#   AMQP_URL  broker to connect to (default amqp://guest:guest@localhost:5672/)
#   PYTHON    interpreter to use (default: python)

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
PYTHON="${PYTHON:-python}"

LOG="$(mktemp "${TMPDIR:-/tmp}/protobus-combat-sample.XXXXXX")"
trap 'rm -f "$LOG"' EXIT

echo "==> Running sample.combatGame.game_runner"
set +e
"$PYTHON" -m sample.combatGame.game_runner 2>&1 | tee "$LOG"
STATUS="${PIPESTATUS[0]}"
set -e

if [ "$STATUS" -ne 0 ]; then
    echo "FAIL: sample exited with status $STATUS" >&2
    exit 1
fi

SHOTS="$(grep -c 'shoots at' "$LOG" || true)"
WINNERS="$(grep -c '(WINNER!)' "$LOG" || true)"
echo
echo "==> Result: ${SHOTS} shots fired, ${WINNERS} winner(s)"
if [ "$WINNERS" -ne 1 ]; then echo "FAIL: expected exactly 1 winner, got ${WINNERS}" >&2; exit 1; fi
if [ "$SHOTS" -lt 1 ]; then echo "FAIL: no shots were fired" >&2; exit 1; fi
echo "PASS: combat game completed with exactly one winner"
