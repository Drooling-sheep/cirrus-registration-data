#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${VPS_HOST:-}" || -z "${VPS_USER:-}" || -z "${VPS_SSH_PRIVATE_KEY:-}" ]]; then
  echo "VPS deploy secrets are not configured; skipping VPS deploy."
  exit 0
fi

VPS_PORT="${VPS_PORT:-22}"
SITE_ROOT="/var/www/cailusaul.uk"
SSH_KEY="$HOME/.ssh/cailusaul_vps_deploy"
DEPLOY_DIR="$(mktemp -d)"
trap 'rm -rf "$DEPLOY_DIR"' EXIT

mkdir -p "$DEPLOY_DIR/cirrus" "$DEPLOY_DIR/data"
cp web/index.html "$DEPLOY_DIR/cirrus/index.html"
if [[ -f web/chart.js ]]; then
  cp web/chart.js "$DEPLOY_DIR/cirrus/chart.js"
fi

DATA_FILES=(
  cirrus_registrations.json
  cirrus_registrations.csv
  cirrus_aircraft_snapshot.json
  serial_history.json
  serial_tracking.json
  flight_activity.json
  flight_activity_history.json
  used_market.json
  transfer_history.json
  listing_history.json
  listings_aso.csv
)

for file in "${DATA_FILES[@]}"; do
  if [[ -f "data/$file" ]]; then
    cp "data/$file" "$DEPLOY_DIR/data/$file"
  fi
done

install -m 700 -d "$HOME/.ssh"
printf '%s\n' "$VPS_SSH_PRIVATE_KEY" > "$SSH_KEY"
chmod 600 "$SSH_KEY"
ssh-keyscan -p "$VPS_PORT" "$VPS_HOST" >> "$HOME/.ssh/known_hosts"

SSH_OPTS=(ssh -i "$SSH_KEY" -p "$VPS_PORT" -o IdentitiesOnly=yes)
RSYNC_SSH="ssh -i $SSH_KEY -p $VPS_PORT -o IdentitiesOnly=yes"

"${SSH_OPTS[@]}" "$VPS_USER@$VPS_HOST" "mkdir -p '$SITE_ROOT/tools/cirrus' '$SITE_ROOT/tools/data'"
rsync -az --delete -e "$RSYNC_SSH" "$DEPLOY_DIR/cirrus/" "$VPS_USER@$VPS_HOST:$SITE_ROOT/tools/cirrus/"
rsync -az --delete -e "$RSYNC_SSH" "$DEPLOY_DIR/data/" "$VPS_USER@$VPS_HOST:$SITE_ROOT/tools/data/"
