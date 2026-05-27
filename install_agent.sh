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

# Export your credentials

# ecmwf-fetch — ECMWF S2S forecasts via the ECMWF Data Stores (ECDS)
export ECMWF_DATASTORES_URL=https://ecds.ecmwf.int/api
export ECMWF_DATASTORES_KEY=

# imerg-fetch — NASA Earthdata (urs.earthdata.nasa.gov)
export EARTHDATA_USERNAME=
export EARTHDATA_PASSWORD=

# tahmo-fetch — TAHMO station API
export TAHMO_API_USERNAME=
export TAHMO_API_PASSWORD=


# Then run the agent
claude --agent rhiza-forecasting:forecaster

EOF
