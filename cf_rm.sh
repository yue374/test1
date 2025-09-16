#!/bin/bash
# ==================================================
# Remove all domains that point to fastest IP (nextdns.cloudflare.fastest.ip.com)
# ==================================================

green_log() {
    echo -e "\e[32m$1\e[0m"
}
red_log() {
    echo -e "\e[31m$1\e[0m"
}
yellow_log() {
    echo -e "\e[33m$1\e[0m"
}

# Read profile IDs
mapfile -t ids <<< "$profile_id"
num_profile_id=${#ids[@]}
echo "[+] Number Profiles ID: $num_profile_id"

# Function: fetch rewrites
fetch_rewrites_from_profile() {
    local profile="$1"
    curl -s -X GET "https://api.nextdns.io/profiles/${profile}/rewrites" \
        -H "X-Api-Key: $nextdns_api"
}

# Function: extract fastest IP
extract_fastest_ip() {
    local json="$1"
    echo "$json" | grep -o '"name":"nextdns\.cloudflare\.fastest\.ip\.com"[^}]*' \
        | grep -o '"content":"[^"]*"' \
        | sed 's/"content":"\([^"]*\)"/\1/'
}

# Function: extract domains mapped to target IP
extract_domains_with_ip() {
    local json="$1"
    local target_ip="$2"

    echo "$json" | python3 -c "
import json,sys
try:
    data=json.load(sys.stdin)
    for item in data.get('data', []):
        if item.get('content') == '$target_ip' and item.get('name') != 'nextdns.cloudflare.fastest.ip.com':
            print(f\"{item['name']}:{item['id']}\")
except:
    pass
" 2>/dev/null
}

# Function: delete rewrite with retries
delete_rewrite() {
    local profile="$1"
    local domain_id="$2"
    local domain_name="$3"
    local max_retries=5
    local attempt=1

    while (( attempt <= max_retries )); do
        response=$(curl -s -X DELETE "https://api.nextdns.io/profiles/${profile}/rewrites/${domain_id}" \
            -H "X-Api-Key: $nextdns_api" \
            -w "\n%{http_code}")

        http_code=$(echo "$response" | tail -n1)

        if [[ "$http_code" == "200" || "$http_code" == "204" ]]; then
            green_log "[+] Deleted $domain_name from profile $profile (attempt $attempt)"
            sleep 0.5  # delay 500ms before next delete
            return 0
        else
            red_log "[!] Failed to delete $domain_name (attempt $attempt/$max_retries, HTTP $http_code)"
            if (( attempt < max_retries )); then
                sleep 5
            fi
        fi
        ((attempt++))
    done

    red_log "[!] Giving up on deleting $domain_name after $max_retries attempts"
    return 1
}

# ==================================================
# Main logic
# ==================================================
for profile in "${ids[@]}"; do
    echo "[*] Processing profile: $profile"

    rewrites_json=$(fetch_rewrites_from_profile "$profile")
    fastest_ip=$(extract_fastest_ip "$rewrites_json")

    if [[ -z "$fastest_ip" ]]; then
        yellow_log "[!] No fastest IP found for profile $profile"
        continue
    fi

    echo "[+] Fastest IP for $profile: $fastest_ip"

    while IFS=: read -r domain domain_id; do
        if [[ -n "$domain_id" ]]; then
            delete_rewrite "$profile" "$domain_id" "$domain" || {
                red_log "[!] Stopping deletions for profile $profile due to repeated errors"
                break
            }
        fi
    done <<< "$(extract_domains_with_ip "$rewrites_json" "$fastest_ip")"

done

green_log "================================================"
green_log "Cleanup complete!"
green_log "================================================"
