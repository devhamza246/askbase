#!/usr/bin/env bash
# Record the demo's suggested questions and attacks, then switch the public demo to recorded-answer mode (no API spending).
#
#   1. put ANTHROPIC_API_KEY in .env, then:  docker compose --env-file .env up -d
#   2. bash scripts/record_demo.sh           # about 11 prompts, a few cents
#   3. git add benchmark/cassette.jsonl && git commit -m "record demo answers" && git push
#   4. on the server: set ASKBASE_LLM_MODE=replay in .env, remove ANTHROPIC_API_KEY from .env, and rebuild:
#        docker compose --env-file .env up -d --build
#      (the cassette is baked into the image; in replay mode the key is never read and no request leaves the server)
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo ".env not found" >&2; exit 2; }
grep -Eq '^ANTHROPIC_API_KEY=.+' .env || { echo "ANTHROPIC_API_KEY is empty in .env" >&2; exit 2; }
DC="docker compose --env-file .env"
[[ -s benchmark/cassette.jsonl ]] && $DC cp benchmark/cassette.jsonl api:/tmp/cassette.jsonl
set +e
$DC exec -T api python -m benchmark.record_demo --cassette /tmp/cassette.jsonl
status=$?
set -e
$DC cp api:/tmp/cassette.jsonl benchmark/cassette.jsonl
echo "Cassette: benchmark/cassette.jsonl ($(wc -l < benchmark/cassette.jsonl) recorded responses). Commit it, then switch to replay mode (see the header of this script)."
exit $status
