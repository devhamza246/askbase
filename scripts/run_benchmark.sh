#!/usr/bin/env bash
# Score the 40-question benchmark (and optionally the live-model adversarial run) on the deployed stack.
# Needs ANTHROPIC_API_KEY in .env. Records benchmark/cassette.jsonl so the result is replayable offline in CI.
#
#   bash scripts/run_benchmark.sh              # benchmark only (about 80 model calls)
#   bash scripts/run_benchmark.sh --adversarial  # also the 20 attack prompts against the real model
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo ".env not found" >&2; exit 2; }
grep -Eq '^ANTHROPIC_API_KEY=.+' .env || { echo "ANTHROPIC_API_KEY is empty in .env; add it and run 'docker compose --env-file .env up -d' first." >&2; exit 2; }
DC="docker compose --env-file .env"
mkdir -p benchmark/results

# keep an existing cassette so repeat runs only pay for new requests
if [[ -s benchmark/cassette.jsonl ]]; then $DC cp benchmark/cassette.jsonl api:/tmp/cassette.jsonl; $DC exec -T -u root api chown askbase:askbase /tmp/cassette.jsonl; fi

echo "== benchmark (live model on cassette misses)"
set +e
$DC exec -T api python -m benchmark.run --mode auto --cassette /tmp/cassette.jsonl \
    --out /tmp/latest.json --gauge-out /tmp/gauge-import.json
status=$?
set -e
$DC cp api:/tmp/cassette.jsonl benchmark/cassette.jsonl
$DC cp api:/tmp/latest.json benchmark/results/latest.json
$DC cp api:/tmp/gauge-import.json benchmark/results/gauge-import.json

if [[ "${1:-}" == "--adversarial" ]]; then
  echo "== adversarial prompts against the live model"
  docker rm -f askbase-adversarial >/dev/null 2>&1 || true
  $DC run --no-deps -T --name askbase-adversarial bootstrap python -m benchmark.adversarial --live --out /tmp/adversarial-live.json || true
  docker cp askbase-adversarial:/tmp/adversarial-live.json benchmark/results/adversarial-live.json || true
  docker rm -f askbase-adversarial >/dev/null 2>&1 || true
fi

echo
echo "Results: benchmark/results/latest.json   Cassette: benchmark/cassette.jsonl (commit it)"
exit $status
