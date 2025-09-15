#!/bin/bash

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


mkdir -p ./storage

# Initialize profile IDs
mapfile -t ids <<< "$profile_id"
num_profile_id=${#ids[@]}
export num_profile_id

# Export individual profile IDs
for i in "${!ids[@]}"; do
  n=$((i + 1))
  export "profile_id$n=${ids[$i]}"
done

echo "[+] Number Profiles ID: $num_profile_id"

# Initialize arrays for domains
declare -A all_domains_map  # Using associative array to track unique domains
declare -a final_domains     # Final list of domains after filtering

# Function to fetch domains from a profile
fetch_domains_from_profile() {
    local profile="$1"
    local response
    
    echo "[*] Fetching domains from profile: $profile"
    
    response=$(curl -s -X GET "https://api.nextdns.io/profiles/${profile}/analytics/domains?status=default%2Callowed&from=-30d&limit=1000" \
        -H "X-Api-Key: $nextdns_api")
    
    # Extract domains from JSON response using grep and sed
    # This handles the JSON structure without requiring jq
    echo "$response" | grep -o '"domain":"[^"]*"' | sed 's/"domain":"\([^"]*\)"/\1/'
}

# Fetch domains from all profiles
echo "[+] Fetching domains from all profiles..."
for profile in "${ids[@]}"; do
    domains=$(fetch_domains_from_profile "$profile")
    
    # Add domains to the map (automatically handles duplicates)
    while IFS= read -r domain; do
        if [[ -n "$domain" ]]; then
            all_domains_map["$domain"]=1
        fi
    done <<< "$domains"
done

echo "[+] Total unique domains collected: ${#all_domains_map[@]}"

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
        
        # Check if domain matches or is a subdomain of exclude pattern
        if [[ "$domain" == "$exclude" ]] || [[ "$domain" == *".$exclude" ]]; then
            return 0  # Should exclude
        fi
    done
    
    return 1  # Should not exclude
}

# Filter domains based on exclude list
echo "[+] Filtering domains based on exclude list..."
for domain in "${!all_domains_map[@]}"; do
    if ! should_exclude "$domain"; then
        final_domains+=("$domain")
    fi
done

# Add domains from include list (if not already present)
echo "[+] Adding domains from include list..."
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

echo "[+] Total domains after filtering: ${#final_domains[@]}"

# Initialize result files
> ./storage/cf_domain.txt
> ./storage/not_cf_domain.txt

# Function to check if domain uses Cloudflare
check_cloudflare() {
    local domain="$1"
    local response
    local timeout=10
    
    # Try to fetch the cdn-cgi/trace endpoint
    response=$(curl -s --connect-timeout "$timeout" --max-time "$timeout" \
        "https://${domain}/cdn-cgi/trace" 2>/dev/null)
    
    # Check if response contains both required lines
    if echo "$response" | grep -q "warp=off" && echo "$response" | grep -q "gateway=off"; then
        echo "$domain" >> ./storage/cf_domain.txt
        echo "[CF] $domain"
    else
        echo "$domain" >> ./storage/not_cf_domain.txt
        echo "[NO-CF] $domain"
    fi
}

# Export the function so it can be used by parallel processes
export -f check_cloudflare

# Process domains in parallel (50 at a time)
echo "[+] Checking domains for Cloudflare CDN (50 parallel)..."

# Using a simple parallel processing approach with background jobs
max_parallel=50
current_jobs=0

for domain in "${final_domains[@]}"; do
    # Wait if we've reached the max parallel limit
    while [[ $(jobs -r | wc -l) -ge $max_parallel ]]; do
        sleep 0.1
    done
    
    # Launch background job
    check_cloudflare "$domain" &
done

# Wait for all remaining jobs to complete
wait

# Final statistics
cf_count=$(wc -l < ./storage/cf_domain.txt 2>/dev/null || echo 0)
not_cf_count=$(wc -l < ./storage/not_cf_domain.txt 2>/dev/null || echo 0)

echo "================================================"
echo "[+] Processing complete!"
echo "[+] Domains with Cloudflare CDN: $cf_count"
echo "[+] Domains without Cloudflare CDN: $not_cf_count"
echo "[+] Results saved to:"
echo "    - ./storage/cf_domain.txt"
echo "    - ./storage/not_cf_domain.txt"
echo "================================================"