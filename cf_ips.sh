#!/bin/bash

mkdir -p ./storage

# Colored output logs
green_log() {
    echo -e "\e[32m$1\e[0m"
}
red_log() {
    echo -e "\e[31m$1\e[0m"
}

# Count how many Profiles IDs input
mapfile -t ids <<< "$profile_id"
num_profile_id=${#ids[@]}
export num_profile_id
for i in "${!ids[@]}"; do
  n=$((i + 1))
  export "profile_id$n=${ids[$i]}"
done
echo "[+] Number Profiles ID: $num_profile_id"

# Initialize arrays for domains
declare -A all_domains_map
declare -a final_domains

# Function to fetch domains from a profile
fetch_domains_from_profile() {
    local profile="$1"
    local response
    echo "[*] Fetching domains from profile: $profile"
    response=$(curl -s -X GET "https://api.nextdns.io/profiles/${profile}/analytics/domains?status=default%2Callowed&from=-30d&limit=1000" \
        -H "X-Api-Key: $nextdns_api")
    echo "$response" | grep -o '"domain":"[^"]*"' | sed 's/"domain":"\([^"]*\)"/\1/'
}

for profile in "${ids[@]}"; do
    domains=$(fetch_domains_from_profile "$profile")
    while IFS= read -r domain; do
        if [[ -n "$domain" ]]; then
            all_domains_map["$domain"]=1
        fi
    done <<< "$domains"
done

echo "[+] Domains collected from NextDNS: ${#all_domains_map[@]}"

# Convert exclude_domain and include_domain to arrays
mapfile -t exclude_list <<< "$exclude_domain"
mapfile -t include_list <<< "$include_domain"

# Function to check if a domain should be excluded
should_exclude() {
    local domain="$1"
    for exclude in "${exclude_list[@]}"; do
        exclude=$(echo "$exclude" | tr -d '\r' | xargs)  # Clean whitespace
        if [[ -z "$exclude" ]]; then
            continue
        fi
        if [[ "$domain" == "$exclude" ]] || [[ "$domain" == *".$exclude" ]]; then
            return 0
        fi
    done
    
    return 1
}

# Filter domains based on exclude list
for domain in "${!all_domains_map[@]}"; do
    if ! should_exclude "$domain"; then
        final_domains+=("$domain")
    fi
done

# Add domains from include list
declare -A final_domains_map
for domain in "${final_domains[@]}"; do
    final_domains_map["$domain"]=1
done
for include in "${include_list[@]}"; do
    include=$(echo "$include" | tr -d '\r' | xargs)  # Clean whitespace
    if [[ -n "$include" ]] && [[ -z "${final_domains_map[$include]}" ]]; then
        final_domains+=("$include")
        final_domains_map["$include"]=1
    fi
done
echo "[+] Domains after filtering: ${#final_domains[@]}"

> ./storage/cf_domain.txt

# Check if domain uses Cloudflare
check_cloudflare() {
    local domain="$1"
    local response
    local timeout=10
    response=$(curl -s --connect-timeout "$timeout" --max-time "$timeout" \
        "https://${domain}/cdn-cgi/trace" 2>/dev/null)
    if echo "$response" | grep -q "warp=off" 2>/dev/null; then
        echo "$domain" >> ./storage/cf_domain.txt
    fi
}
export -f check_cloudflare

# Process domains in parallel
max_parallel=50
current_jobs=0
for domain in "${final_domains[@]}"; do
    while [[ $(jobs -r | wc -l) -ge $max_parallel ]]; do
        sleep 0.1
    done
    check_cloudflare "$domain" &
done
wait
cf_count=$(wc -l < ./storage/cf_domain.txt 2>/dev/null || echo 0)

green_log "================================================"
green_log "Domains with Cloudflare CDN: $cf_count"
green_log "================================================"