#!/bin/sh
set -e

# google-service-account.json is gitignored and never baked into the image — Fly can only
# hold it as a secret env var, so it's shipped base64-encoded and decoded to disk here on
# boot. Only meaningful when GOOGLE_SERVICE_ACCOUNT_JSON (the path app/config.py reads) is
# also set, matching where this writes the file.
if [ -n "$GOOGLE_SERVICE_ACCOUNT_JSON_B64" ] && [ -n "$GOOGLE_SERVICE_ACCOUNT_JSON" ]; then
    mkdir -p "$(dirname "$GOOGLE_SERVICE_ACCOUNT_JSON")"
    echo "$GOOGLE_SERVICE_ACCOUNT_JSON_B64" | base64 -d > "$GOOGLE_SERVICE_ACCOUNT_JSON"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}"
