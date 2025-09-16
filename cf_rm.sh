#!/bin/bash
set -euo pipefail

mkdir -p ./storage

# ===================== #
# Colored output logs
# ===================== #
green_log() { echo -e "\e[32m$1\e[0m"; }
red_log()   { echo -e "\e[31m$1\e[0m"; }
yellow_log(){ echo -e "\e[33m$1\e[0m"; }

# ===================== #
# Config
# ===================== #
exclude_domain="nextdns.cloudflare.fastest.ip.com"

# ===================== #
# Check required env
# ===================== #
if [[ -z "${nextdns_api:-}" ]]; then
    red_log "❌ nextdns_api is not set"
    exit 1
fi

if [[ -z "${profile_id:-}" ]]; then
    red_log "❌ profile_id is not set"
    exit 1
fi

# ===================== #
# Load profile IDs
# ===================== #
mapfile -t ids <<< "$profile_id"

# ===================== #
# Process each profile
# ===================== #
for pid in "${ids[@]}"; do
    green_log "🔎 Fetching domains for profile: $pid"

    # Fetch domains
    domains=$(curl -s -X GET \
        "https://api.nextdns.io/profiles/$pid/analytics/domains?status=default%2Callowed&from=-30d&limit=1000" \
        -H "X-Api-Key: $nextdns_api" | jq -r '.data[].domain')

    # Filter out excluded domain
    mapfile -t filtered <<< "$(printf '%s\n' $domains | grep -v "^$exclude_domain$")"

    # Count and show before deleting
    count=${#filtered[@]}
    yellow_log "⚠️ Found $count domains to delete (excluding: $exclude_domain)"

    deleted=0
    failed=0

    for domain in "${filtered[@]}"; do
        attempt=1
        success=0
        while [ $attempt -le 5 ]; do
            status=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE \
                "https://api.nextdns.io/profiles/$pid/analytics/domains/$domain" \
                -H "X-Api-Key: $nextdns_api")

            if [ "$status" -eq 200 ]; then
                green_log "[$pid] ✅ Deleted domain: $domain"
                success=1
                ((deleted++))
                sleep 0.5
                break
            else
                red_log "[$pid] ❌ Failed to delete $domain (status $status), attempt $attempt/5"
                ((attempt++))
                sleep 5
            fi
        done

        if [ $success -eq 0 ]; then
            red_log "[$pid] ⚠️ Skipped $domain after 5 failed attempts"
            ((failed++))
        fi
    done

    # Summary per profile
    yellow_log "📊 Summary for $pid: Deleted=$deleted, Failed=$failed, Skipped=$exclude_domain"
done
