#!/usr/bin/env bash
# End-to-end demo: replicate the daily S2S pipeline from
# ECMWF-S2S4AFRICA/.github/workflows/daily_download2.0.yml using the
# forecasting-skills CLI. Requires ECMWF_API_*, EARTHDATA_*, TAHMO_API_*.

set -euo pipefail

# Resolve cwd to the directory containing this script so relative paths in the
# pipeline (./demo_output, ../, etc.) and direnv's .envrc load consistently
# regardless of the caller's PWD.
cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")"
eval "$(direnv export bash 2>/dev/null || true)"

days_ago() { date -u -d "$1 days ago" +%Y-%m-%d 2>/dev/null || date -u -v-"$1"d +%Y-%m-%d; }
date_minus() { date -u -d "$1 - $2 days" +%Y-%m-%d 2>/dev/null || date -u -j -v-"$2"d -f "%Y-%m-%d" "$1" +%Y-%m-%d; }
# Positional args: INIT_DATE END_DATE OUT
# Defaults reproduce upstream daily_download2.0.yml: INIT = now_dt - 2, END = now_dt.
INIT_DATE="${1:-$(days_ago 2)}"
END_DATE="${2:-$(days_ago 0)}"
START_DATE=$(date_minus "$END_DATE" 60)
OUT="${3:-./demo_output}"
COUNTRIES=(Kenya Ghana Senegal Ethiopia)
declare -A ISO=([Kenya]=KEN [Ghana]=GHA [Senegal]=SEN [Ethiopia]=ETH)

mkdir -p "$OUT"
uv tool install --force --reinstall ../

# Africa is a continent, not a country, so the continental extent is passed as an
# explicit bbox rather than via resolve-region (which is country-level).
forecasting-skills ecmwf-fetch  --date  "$INIT_DATE"  --bbox=23/-20/-37/59 --output "$OUT/ecmwf.zarr"
forecasting-skills deaccumulate -i "$OUT/ecmwf.zarr" -o "$OUT/ecmwf_per_step.zarr"
forecasting-skills imerg-fetch  --start "$START_DATE" --end "$END_DATE" --output "$OUT/imerg.zarr"
forecasting-skills chirps-fetch --start "$START_DATE" --end "$END_DATE" --output "$OUT/chirps.zarr"

for COUNTRY in "${COUNTRIES[@]}"; do
    d="$OUT/$COUNTRY"
    mkdir -p "$d"

    # Resolve the country once: capture the bbox on stdout and write the boundary
    # polygon to boundary.geojson (the --geojson status note goes to stderr, so the
    # command substitution captures only the bbox).
    BBOX=$(forecasting-skills resolve-region "${ISO[$COUNTRY]}" --geojson "$d/boundary.geojson")

    forecasting-skills clip-region        -i "$OUT/ecmwf_per_step.zarr" -o "$d/ecmwf.zarr"         --bbox="$BBOX"
    forecasting-skills aggregate-temporal -i "$d/ecmwf.zarr"    -o "$d/ecmwf_weekly.zarr"  --period weekly  --method sum
    forecasting-skills aggregate-temporal -i "$d/ecmwf.zarr"    -o "$d/ecmwf_dekadal.zarr" --period dekadal --method sum
    forecasting-skills plot               -i "$d/ecmwf_weekly.zarr"  -o "$d/weekly_precip.png"  --variable tp --colormap white,wheat,lightgreen,green,lightblue,blue,yellow,orange,red,purple --bbox="$BBOX"
    forecasting-skills plot               -i "$d/ecmwf_dekadal.zarr" -o "$d/dekadal_precip.png" --variable tp --colormap white,wheat,lightgreen,green,lightblue,blue,yellow,orange,red,purple --bbox="$BBOX"
    # Upstream daily_download2.0.yml runs a downscale step (dowscale_dekade.py) producing
    # dekadal_precip_downscaled.png. That artifact isn't in the emailed attachments, so
    # we run the downscale + plot here for parity with the workflow steps but the result is
    # not part of the email deliverable.
    forecasting-skills downscale          -i "$d/ecmwf_dekadal.zarr" -o "$d/ecmwf_dekadal_ds.zarr" --method linear-interpolation --target-resolution 0.25 --variable tp
    forecasting-skills plot               -i "$d/ecmwf_dekadal_ds.zarr" -o "$d/dekadal_precip_ds.png" --variable tp

    forecasting-skills clip-region        -i "$OUT/imerg.zarr"  -o "$d/imerg.zarr"         --bbox="$BBOX"
    forecasting-skills aggregate-temporal -i "$d/imerg.zarr"    -o "$d/imerg_weekly.zarr"  --period weekly  --method sum --anchor-end "$END_DATE"
    forecasting-skills aggregate-temporal -i "$d/imerg.zarr"    -o "$d/imerg_dekadal.zarr" --period dekadal --method sum --anchor-end "$END_DATE"

    forecasting-skills clip-region        -i "$OUT/chirps.zarr" -o "$d/chirps.zarr"         --bbox="$BBOX"
    forecasting-skills aggregate-temporal -i "$d/chirps.zarr"   -o "$d/chirps_weekly.zarr"  --period weekly  --method sum --anchor-end "$END_DATE"
    forecasting-skills aggregate-temporal -i "$d/chirps.zarr"   -o "$d/chirps_dekadal.zarr" --period dekadal --method sum --anchor-end "$END_DATE"

    forecasting-skills tahmo-fetch --country "$COUNTRY" --start "$START_DATE" --end "$END_DATE" --output "$d/tahmo.zarr"

    forecasting-skills plot-compare -i "$d/tahmo.zarr" -i "$d/imerg_weekly.zarr"   --variable precip --overlay-resample sum --panels 4 --bbox="$BBOX" --mask-geojson "$d/boundary.geojson" -o "$d/imerg_${COUNTRY}_weekly.png"
    forecasting-skills plot-compare -i "$d/tahmo.zarr" -i "$d/imerg_dekadal.zarr"  --variable precip --overlay-resample sum             --bbox="$BBOX" --mask-geojson "$d/boundary.geojson" -o "$d/imerg_${COUNTRY}_dekadal.png"
    forecasting-skills plot-compare -i "$d/tahmo.zarr" -i "$d/chirps_weekly.zarr"  --variable precip --overlay-resample sum --panels 4 --bbox="$BBOX" --mask-geojson "$d/boundary.geojson" -o "$d/chirps_${COUNTRY}_weekly.png"
    forecasting-skills plot-compare -i "$d/tahmo.zarr" -i "$d/chirps_dekadal.zarr" --variable precip --overlay-resample sum             --bbox="$BBOX" --mask-geojson "$d/boundary.geojson" -o "$d/chirps_${COUNTRY}_dekadal.png"

    forecasting-skills email-report \
        --from "$COUNTRY Data Share <demo@example.com>" \
        --to recipient@example.com \
        --subject "Daily S2S Outlook — $COUNTRY ($INIT_DATE)" \
        --body "S2S outlook plus sat-vs-station comparison for $COUNTRY." \
        --attach \
            "$d/weekly_precip.png" \
            "$d/dekadal_precip.png" \
            "$d/imerg_${COUNTRY}_weekly.png" \
            "$d/imerg_${COUNTRY}_dekadal.png" \
            "$d/chirps_${COUNTRY}_weekly.png" \
            "$d/chirps_${COUNTRY}_dekadal.png" \
        --output "$d/$COUNTRY.eml"
done
