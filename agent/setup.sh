#!/usr/bin/env bash
# Homelab Agent — dks01 setup script
# Run from the directory containing this file:  bash setup.sh
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$AGENT_DIR"

echo "==> Checking Python version..."
PY=$(command -v python3 || true)
if [ -z "$PY" ]; then
  echo "ERROR: python3 not found. Install Python 3.12+ and re-run."
  exit 1
fi

PY_VER=$("$PY" -c 'import sys; print(sys.version_info[:2])')
if "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'; then
  echo "    Python OK: $($PY --version)"
else
  echo "ERROR: Python 3.12+ required, found $($PY --version)."
  exit 1
fi

echo "==> Checking hatch..."
if ! command -v hatch &>/dev/null; then
  echo "    hatch not found — installing via pip..."
  "$PY" -m pip install --quiet hatch
fi
echo "    hatch OK: $(hatch --version)"

echo "==> Checking environment variables..."
MISSING=()
[ -z "${ANTHROPIC_API_KEY:-}" ] && MISSING+=(ANTHROPIC_API_KEY)
[ -z "${SLACK_WEBHOOK_URL:-}" ]  && MISSING+=(SLACK_WEBHOOK_URL)

if [ ${#MISSING[@]} -gt 0 ]; then
  echo ""
  echo "  The following env vars are not set:"
  for v in "${MISSING[@]}"; do echo "    - $v"; done
  echo ""
  echo "  Add them to ~/.profile and re-run, e.g.:"
  echo "    echo 'export ANTHROPIC_API_KEY=\"sk-ant-...\"' >> ~/.profile"
  echo "    echo 'export SLACK_WEBHOOK_URL=\"\"'           >> ~/.profile"
  echo "    source ~/.profile"
  echo ""
  echo "  SLACK_WEBHOOK_URL can be left empty for now."
  exit 1
fi
echo "    ANTHROPIC_API_KEY: set"
echo "    SLACK_WEBHOOK_URL: ${SLACK_WEBHOOK_URL:+set (will be used)}${SLACK_WEBHOOK_URL:-not set (Slack notifications disabled)}"

echo "==> Creating hatch environment..."
hatch env create

echo ""
echo "Setup complete. To run the agent:"
echo ""
echo "  cd $AGENT_DIR"
echo "  hatch run python cli.py"
echo ""
echo "First things to try:"
echo "  > list all running services"
echo "  > show me the logs for traefik_traefik"
echo ""
echo "Slash commands inside the REPL:"
echo "  /status   — quick service health table"
echo "  /safemode — show current safe mode state"
echo "  /quit     — exit"
