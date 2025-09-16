#!/bin/bash

green_log() {
    echo -e "\e[32m$1\e[0m"
}
red_log() {
    echo -e "\e[31m$1\e[0m"
}
yellow_log() {
    echo -e "\e[33m$1\e[0m"
}

mapfile -t ids <<< "$profile_id"
num_profile_id=${#ids[@]}
echo "[+] Number Profiles ID: $num_profile_id"

fetch_rewrites_from_profile() {
    local profile="$1"
    curl -s -X GET "https://api.nextdns.io/profiles/${profile}/rewrites" \
        -H "X-Api-Key: $nextdns_api"
}

extract_fastest_ip() {
    local json="$1"
    echo "$json" | grep -o '"name":"nextdns\.cloudflare\.fastest\.ip\.com"[^}]*' \
        | grep -o '"content":"[^"]*"' \
        | sed 's/"content":"\([^"]*\)"/\1/'
}

extract_domains_with_ip() {
    local json="$1"
    local target_ip="$2"

    echo "$json" | python3 -c "
import json,sys
try:
    data=json.load(sys.stdin)
    for item in data.get('data', []):
        if item.get('content') == '$target_ip':
            print(f\"{item['name']}:{item['id']}\")
except:
    pass
" 2>/dev/null
}

delete_rewrite() {
    local profile="$1"
    local domain_id="$2"
    local domain_name="$3"
    local max_retries=5
    local attempt=1

    if [[ "$domain_name" == "nextdns.cloudflare.fastest.ip.com" ]]; then
        return 0
    fi

    while (( attempt <= max_retries )); do
        response=$(curl -s -X DELETE "https://api.nextdns.io/profiles/${profile}/rewrites/${domain_id}" \
            -H "X-Api-Key: $nextdns_api" \
            -w "\n%{http_code}")

        http_code=$(echo "$response" | tail -n1)

        if [[ "$http_code" == "200" || "$http_code" == "204" ]]; then
            sleep 1
            return 0
        else
            if (( attempt < max_retries )); then
                sleep 20
            fi
        fi
        ((attempt++))
    done
    return 1
}


for profile in "${ids[@]}"; do

    rewrites_json=$(fetch_rewrites_from_profile "$profile")
    fastest_ip=$(extract_fastest_ip "$rewrites_json")

    if [[ -z "$fastest_ip" ]]; then
        yellow_log "[!] No fastest IP found for profile $profile"
        continue
    fi

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
