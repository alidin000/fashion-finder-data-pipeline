#!/bin/sh
# Start command for the Render API service (see render.yaml).
# Demo mode seeds a synthetic catalogue into temporary storage; real-source mode
# also starts the hourly feed worker.
set -eu

data_dir="${FASHION_FINDER_DATA_DIR:-/data}"
export FASHION_FINDER_DB="$data_dir/fashion_finder.sqlite"
if ! mkdir -p "$data_dir/feeds" 2>/dev/null || [ ! -w "$data_dir" ]; then
  echo "render-api-start: $data_dir is not writable by uid $(id -u); check FASHION_FINDER_DATA_DIR" >&2
  exit 1
fi
if [ "${FASHION_FINDER_DEMO_MODE:-0}" = "1" ]; then
  # Synthetic demo only. The seed refuses a data directory that holds a real catalogue.
  export FASHION_FINDER_SOURCE_POLICIES="${FASHION_FINDER_SOURCE_POLICIES:-$data_dir/source-policies.json}"
  fashion-finder-demo --data-dir "$data_dir"
fi
fashion-finder --data-dir "$data_dir" migrate
# The hourly feed worker imports partner CSV files. FASHION_FINDER_FEED_WORKER=0 skips it
# where offers arrive through the ingest API instead (the free Render plan).
if [ "${FASHION_FINDER_DEMO_MODE:-0}" != "1" ] && [ "${FASHION_FINDER_FEED_WORKER:-1}" = "1" ]; then
  fashion-finder-worker --data-dir "$data_dir" --interval-seconds 3600 &
fi
# One worker by default: Render's free instance has 512 MB, and two workers with the image
# and text models loaded measured 865 MB (docs/benchmark-results.md). Raise WEB_CONCURRENCY
# on a larger plan.
exec gunicorn --bind "0.0.0.0:${PORT:-8000}" --workers "${WEB_CONCURRENCY:-1}" --threads 2 \
  fashion_finder_data.django_project.wsgi:application
