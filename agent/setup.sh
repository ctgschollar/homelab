#!/usr/bin/env bash
# Homelab Agent — dks01 setup script
# Run from the directory containing this file:  bash setup.sh
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$AGENT_DIR"

echo "==> Checking hatch..."
if ! command -v hatch &>/dev/null; then
  echo "    hatch not found — installing via pip..."
  pip install --quiet hatch
fi
echo "    hatch OK: $(hatch --version)"
echo "    (hatch will manage Python 3.12 automatically)"

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
