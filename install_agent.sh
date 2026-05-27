#!/usr/bin/env bash
# Install the Rhiza forecasting agent and skills as a Claude Code plugin.
# This runs the canonical `claude plugin` CLI only. It copies no files, and it
# never reads or outputs the contents of .env or any credential file.
set -e

claude plugin marketplace add rhiza-research/forecasting-skills
claude plugin install rhiza-forecasting@rhiza

cat <<'EOF'

Installed the rhiza-forecasting plugin.

Next steps:
  1. cp .env.example .env     # then edit .env and fill in your credentials
  2. claude --agent rhiza-forecasting:forecaster

EOF
