#!/usr/bin/env bash
# Linux/macOS variant of scripts/setup.ps1 (same steps). Flags via env:
#   SKIP_INSTALL=1 SKIP_FRONTEND=1 SKIP_SEED=1 SKIP_DOCKER=1 SCALE=default SEED=42 DATABASE_URL=...
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
PY="$REPO/.venv/bin/python"
step() { printf '==> %s\n' "$*"; }

[ -x "$PY" ] || { step "creating .venv"; python3.12 -m venv .venv; }
if [ -z "${SKIP_INSTALL:-}" ]; then
  step "pip install -e backend[dev]"
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -e "./backend[dev]"
fi
if [ -z "${SKIP_FRONTEND:-}" ]; then step "npm ci"; (cd frontend && npm ci); fi

if [ ! -f .env ]; then
  step "creating .env with random secrets (not printed)"
  hmac="$("$PY" -c 'import secrets;print(secrets.token_hex(32))')"
  token="$("$PY" -c 'import secrets;print(secrets.token_hex(32))')"
  sed -e "s/^LAE_HMAC_KEY=\s*$/LAE_HMAC_KEY=$hmac/" -e "s/^LAE_API_TOKEN=\s*$/LAE_API_TOKEN=$token/" \
    .env.example > .env
  chmod 600 .env
fi

if [ -z "${SKIP_DOCKER:-}" ]; then
  step "docker compose up -d db"
  docker compose up -d db
  cid="$(docker compose ps -q db)"
  for _ in $(seq 1 60); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$cid")" = healthy ] && break
    sleep 2
  done
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$cid")" = healthy ] || { echo "db not healthy" >&2; exit 1; }
fi

step "creating test databases"
"$PY" scripts/create_test_dbs.py
step "alembic upgrade head"
if [ -n "${DATABASE_URL:-}" ]; then
  "$PY" -m alembic -c backend/alembic.ini -x "url=$DATABASE_URL" upgrade head
else
  "$PY" -m alembic -c backend/alembic.ini upgrade head
fi
if [ -z "${SKIP_SEED:-}" ]; then
  url="${DATABASE_URL:-$(grep -E '^LAE_DATABASE_URL=' .env | head -1 | cut -d= -f2-)}"
  step "seeding (scale=${SCALE:-default} seed=${SEED:-42})"
  "$PY" -m db.fixtures.generator --scale "${SCALE:-default}" --seed "${SEED:-42}" --url "$url"
fi
[ -f secrets/signing_key.pem ] || "$REPO/.venv/bin/lae" keys generate --out secrets/signing_key.pem
step "setup complete"
