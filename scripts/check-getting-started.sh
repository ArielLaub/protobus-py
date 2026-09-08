#!/usr/bin/env bash
#
# Runs the Getting Started tutorial from docs/guide/getting-started.md exactly
# as written — server, event subscriber, client — against a live RabbitMQ and
# asserts the documented output. Keeps the docs honest.
#
# Env:
#   AMQP_URL  broker to connect to (default amqp://guest:guest@localhost:5672/)
#   PYTHON    interpreter to use (default: python)

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python}"
DOC="$REPO_ROOT/docs/guide/getting-started.md"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/protobus-getting-started.XXXXXX")"
trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$WORK"' EXIT
mkdir -p "$WORK/proto"

# Pull each fenced block out of the doc by the file name in its first line.
extract() {  # extract <language> <file comment> <destination>
    "$PYTHON" - "$DOC" "$1" "$2" > "$3" <<'PY'
import re, sys
doc, lang, marker = open(sys.argv[1]).read(), sys.argv[2], sys.argv[3]
for block in re.findall(r"```" + lang + r"\n(.*?)```", doc, re.S):
    if marker in block.splitlines()[0]:
        print(block, end="")
        sys.exit(0)
if lang == "protobuf":
    print(re.findall(r"```protobuf\n(.*?)```", doc, re.S)[0], end="")
    sys.exit(0)
sys.exit(f"block {marker!r} not found in {doc}")
PY
}
extract protobuf "" "$WORK/proto/Calculator.proto"
extract python "# context.py" "$WORK/context.py"
extract python "# calculator_service.py" "$WORK/calculator_service.py"
extract python "# server.py" "$WORK/server.py"
extract python "# client.py" "$WORK/client.py"
extract python "# event_subscriber.py" "$WORK/event_subscriber.py"

cd "$WORK"
export LOG_LEVEL=warn PYTHONUNBUFFERED=1
"$PYTHON" server.py > server.log 2>&1 &
"$PYTHON" event_subscriber.py > subscriber.log 2>&1 &
for _ in $(seq 1 40); do
    grep -q "Listening for events" subscriber.log 2>/dev/null && break
    sleep 0.25
done
sleep 1
OUT="$("$PYTHON" client.py)"
echo "$OUT"
[ "$OUT" = "5 + 3 = 8" ] || { echo "FAIL: client printed $OUT" >&2; exit 1; }
for _ in $(seq 1 40); do
    grep -q "Received event: add = 8" subscriber.log && break
    sleep 0.25
done
grep -q "Received event: add = 8" subscriber.log || { echo "FAIL: subscriber did not receive the event" >&2; cat subscriber.log >&2; exit 1; }
kill -TERM $(jobs -p)
wait || true
echo "PASS: the Getting Started tutorial runs as written"
