#!/bin/sh
set -eu

cd "$(dirname "$0")/../.."

defaults=config/defaults
mkdir -p config

if [ "${1:-}" = "--defaults" ]; then
    revert=yes
else
    printf 'revert all config to defaults? [y/N]: '
    read -r revert || revert=
fi

case "$revert" in
    y|Y|yes|YES)
        cp "$defaults"/*.yaml config/
        echo "config reset to defaults"
        exit 0
        ;;
esac

ask() {
    label=$1
    default=$2
    value=
    printf '%s [%s]: ' "$label" "$default" >&2
    read -r value || value=
    if [ -n "$value" ]; then
        printf '%s' "$value"
    else
        printf '%s' "$default"
    fi
}

to_seconds() {
    case "$1" in
        *h) echo $(( ${1%h} * 3600 )) ;;
        *m) echo $(( ${1%m} * 60 )) ;;
        *d) echo $(( ${1%d} * 86400 )) ;;
        *w) echo $(( ${1%w} * 604800 )) ;;
        *) echo "$1" ;;
    esac
}

to_epoch() {
    case "$1" in
        *[!0-9]*) date -u -d "$1" +%s ;;
        *) echo "$1" ;;
    esac
}

escape_sed() {
    printf '%s' "$1" | sed 's/[&|]/\\&/g'
}

echo "collector" >&2
account_types=$(ask 'account types, comma separated' HARDCORE_IRONMAN)
interval=$(to_seconds "$(ask 'backfill interval (24h)' 24h)")
collect=$(to_seconds "$(ask 'live interval (1h)' 1h)")
retry=$(to_seconds "$(ask 'retry window (7d)' 7d)")
start=$(to_epoch "$(ask 'data start date (YYYY-MM-DD)' 2025-01-26)")
workers=$(ask 'worker threads' 2)
rate=$(ask 'requests per second' 4)
players=$(ask 'priority players, comma separated or none' none)
priority_interval=$(to_seconds "$(ask 'priority interval (10m)' 10m)")
retention=$(to_seconds "$(ask 'priority retention (24h)' 24h)")
priority_top=$(ask 'priority top players by overall xp' 5)

echo "web" >&2
site_title=$(ask 'site title' "vidyascape tracker")
report=$(to_seconds "$(ask 'report interval (1m)' 1m)")
top_n=$(ask 'ranking entries' 5)
base_url=$(ask 'public base url' "http://localhost:8000")

echo "db" >&2
host=$(ask 'database host (empty for default)' "")
port=$(ask 'database port' 5432)
database=$(ask 'database name' xp_tracker)
user=$(ask 'database user' xp_tracker)

if [ "$players" = "none" ] || [ -z "$players" ]; then
    players_yaml="[]"
else
    players_yaml="[$(printf '%s' "$players" | sed 's/, */, /g')]"
fi
account_yaml="[$(printf '%s' "$account_types" | sed 's/, */, /g')]"

cp "$defaults/collector.yaml" config/collector.yaml
sed -i \
    -e "s/^account_types: .*/account_types: $account_yaml/" \
    -e "/^defaults:/,/^priority:/ s/^  interval_seconds: .*/  interval_seconds: $interval/" \
    -e "/^defaults:/,/^priority:/ s/^  collect_interval_seconds: .*/  collect_interval_seconds: $collect/" \
    -e "/^defaults:/,/^priority:/ s/^  retry_window_seconds: .*/  retry_window_seconds: $retry/" \
    -e "/^defaults:/,/^priority:/ s/^  data_start_epoch: .*/  data_start_epoch: $start/" \
    -e "/^defaults:/,/^priority:/ s/^  workers: .*/  workers: $workers/" \
    -e "/^defaults:/,/^priority:/ s/^  rate: .*/  rate: $rate/" \
    -e "/^priority:/,\$ s/^  players: .*/  players: $players_yaml/" \
    -e "/^priority:/,\$ s/^  interval_seconds: .*/  interval_seconds: $priority_interval/" \
    -e "/^priority:/,\$ s/^  retention_seconds: .*/  retention_seconds: $retention/" \
    -e "/^priority:/,\$ s/^  top: .*/  top: $priority_top/" \
    config/collector.yaml

cp "$defaults/web.yaml" config/web.yaml
sed -i \
    -e "s|^  site_title: .*|  site_title: \"$(escape_sed "$site_title")\"|" \
    -e "s/^  report_interval_seconds: .*/  report_interval_seconds: $report/" \
    -e "s/^  top_n: .*/  top_n: $top_n/" \
    -e "s|^  base_url: .*|  base_url: \"$(escape_sed "$base_url")\"|" \
    config/web.yaml

cp "$defaults/db.yaml" config/db.yaml
if [ -z "$host" ]; then
    host_yaml='""'
else
    host_yaml=$host
fi
sed -i \
    -e "s/^host: .*/host: $host_yaml/" \
    -e "s/^port: .*/port: $port/" \
    -e "s/^database: .*/database: $database/" \
    -e "s/^user: .*/user: $user/" \
    config/db.yaml

echo
echo "wrote config/collector.yaml config/web.yaml config/db.yaml"
echo "restart the containers to apply: ./scripts/update.sh"
