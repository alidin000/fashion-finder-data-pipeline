#!/bin/sh
# Start command for the Render API private service (see render.yaml).
# A Render disk attaches to one service only, so this one container runs the
# migration, the hourly feed worker and Gunicorn against the same SQLite file.
set -eu

data_dir="${FASHION_FINDER_DATA_DIR:-/data}"
export FASHION_FINDER_DB="$data_dir/fashion_finder.sqlite"
if ! mkdir -p "$data_dir/feeds" 2>/dev/null || [ ! -w "$data_dir" ]; then
  echo "render-api-start: $data_dir is not writable by uid $(id -u); check the disk mount" >&2
  exit 1
fi
if [ "${FASHION_FINDER_DEMO_MODE:-0}" = "1" ]; then
  # Synthetic demo only. The seed refuses a data directory that holds a real catalogue.
  export FASHION_FINDER_SOURCE_POLICIES="${FASHION_FINDER_SOURCE_POLICIES:-$data_dir/source-policies.json}"
  fashion-finder-demo --data-dir "$data_dir"
fi
fashion-finder --data-dir "$data_dir" migrate
fashion-finder-worker --data-dir "$data_dir" --interval-seconds 3600 &
exec gunicorn --bind "0.0.0.0:${PORT:-8000}" --workers 2 --threads 2 \
  fashion_finder_data.django_project.wsgi:application
