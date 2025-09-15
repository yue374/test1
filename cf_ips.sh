#!/bin/bash

# Colored output logs
green_log() {
    echo -e "\e[32m$1\e[0m"
}
red_log() {
    echo -e "\e[31m$1\e[0m"
}

mkdir storage

# Count how many Profiles IDs input
mapfile -t ids <<< "$profile_id"
num_profile_id=${#ids[@]}
export num_profile_id
for i in "${!ids[@]}"; do
  n=$((i + 1))
  export "profile_id$n=${ids[$i]}"
done
echo "[+] Number Profiles ID: $num_profile_id"




# Parse profile IDs
mapfile -t ids <<< "$profile_id"
num_profile_id=${#ids[@]}
export num_profile_id

# Export individual profile IDs
for i in "${!ids[@]}"; do
    n=$((i + 1))
    export "profile_id$n=${ids[$i]}"
done

echo "[+] Number Profiles ID: $num_profile_id"

# Function to fetch domains from a profile
fetch_profile_domains() {
    local profile=$1
    local temp_file="/tmp/profile_${profile}.json"
    
    echo "[+] Fetching domains for profile: $profile"
    
    curl -s -X GET "https://api.nextdns.io/profiles/${profile}/analytics/domains?status=default%2Callowed&from=-30d&limit=1000" \
         -H "X-Api-Key: $nextdns_api" > "$temp_file"
    
    if [[ $? -eq 0 ]] && [[ -s "$temp_file" ]]; then
        # Extract domains from JSON response
        jq -r '.data[]?.domain // empty' "$temp_file" 2>/dev/null | grep -v '^$'
        rm -f "$temp_file"
    else
        echo "[-] Failed to fetch data for profile: $profile" >&2
        rm -f "$temp_file"
    fi
}

# Function to check if domain should be excluded
is_excluded() {
    local domain=$1
    
    if [[ -n "$exclude_domain" ]]; then
        while IFS= read -r exclude_pattern; do
            [[ -z "$exclude_pattern" ]] && continue
            # Remove any leading/trailing whitespace
            exclude_pattern=$(echo "$exclude_pattern" | xargs)
            
            # Check if domain ends with the exclude pattern (including exact match)
            if [[ "$domain" == "$exclude_pattern" ]] || [[ "$domain" == *".$exclude_pattern" ]]; then
                return 0  # Should be excluded
            fi
        done <<< "$exclude_domain"
    fi
    
    return 1  # Should not be excluded
}

# Function to check Cloudflare CDN
check_cloudflare() {
    local domain=$1
    local response
    
    # Make request to CDN trace endpoint with timeout
    response=$(curl -s --max-time 10 --connect-timeout 5 "https://$domain/cdn-cgi/trace" 2>/dev/null)
    
    # Check if response contains both required lines
    if echo "$response" | grep -q "warp=off" && echo "$response" | grep -q "gateway=off"; then
        return 0  # Is Cloudflare
    else
        return 1  # Not Cloudflare or error
    fi
}

# Function to process domains in parallel for Cloudflare detection
process_domains_parallel() {
    local domains_file=$1
    local max_jobs=50
    local cf_domains=()
    local not_cf_domains=()
    
    echo "[+] Checking Cloudflare CDN status for domains (parallel processing)..."
    
    # Function to be run in parallel
    check_domain_worker() {
        local domain=$1
        local worker_id=$2
        
        if check_cloudflare "$domain"; then
            echo "CF:$domain" > "/tmp/result_${worker_id}.txt"
        else
            echo "NOT_CF:$domain" > "/tmp/result_${worker_id}.txt"
        fi
    }
    
    export -f check_cloudflare
    export -f check_domain_worker
    
    # Process domains in batches
    local job_count=0
    local worker_id=0
    
    while IFS= read -r domain; do
        [[ -z "$domain" ]] && continue
        
        # Start background job
        check_domain_worker "$domain" "$worker_id" &
        
        ((job_count++))
        ((worker_id++))
        
        # Limit concurrent jobs
        if ((job_count >= max_jobs)); then
            wait  # Wait for all background jobs to complete
            job_count=0
            
            # Collect results
            for ((i=0; i<max_jobs; i++)); do
                if [[ -f "/tmp/result_${i}.txt" ]]; then
                    local result=$(cat "/tmp/result_${i}.txt")
                    if [[ "$result" == CF:* ]]; then
                        cf_domains+=("${result#CF:}")
                    else
                        not_cf_domains+=("${result#NOT_CF:}")
                    fi
                    rm -f "/tmp/result_${i}.txt"
                fi
            done
            
            worker_id=0
            echo "[+] Processed batch, continuing..."
        fi
    done < "$domains_file"
    
    # Wait for remaining jobs
    wait
    
    # Collect final results
    for ((i=0; i<worker_id; i++)); do
        if [[ -f "/tmp/result_${i}.txt" ]]; then
            local result=$(cat "/tmp/result_${i}.txt")
            if [[ "$result" == CF:* ]]; then
                cf_domains+=("${result#CF:}")
            else
                not_cf_domains+=("${result#NOT_CF:}")
            fi
            rm -f "/tmp/result_${i}.txt"
        fi
    done
    
    # Write results to files
    printf '%s\n' "${cf_domains[@]}" > /storage/cf_domain.txt
    printf '%s\n' "${not_cf_domains[@]}" > /storage/not_cf_domain.txt
    
    echo "[+] Cloudflare domains: ${#cf_domains[@]}"
    echo "[+] Non-Cloudflare domains: ${#not_cf_domains[@]}"
}

# Main execution
echo "[+] Starting domain collection..."

# Collect all domains from all profiles
all_domains_file="/tmp/all_domains.txt"
> "$all_domains_file"  # Clear the file

for profile in "${ids[@]}"; do
    fetch_profile_domains "$profile" >> "$all_domains_file"
done

# Remove duplicates
echo "[+] Removing duplicate domains..."
sort "$all_domains_file" | uniq > "/tmp/unique_domains.txt"

# Filter out excluded domains
echo "[+] Filtering excluded domains..."
filtered_domains_file="/tmp/filtered_domains.txt"
> "$filtered_domains_file"

while IFS= read -r domain; do
    [[ -z "$domain" ]] && continue
    
    if ! is_excluded "$domain"; then
        echo "$domain" >> "$filtered_domains_file"
    else
        echo "[-] Excluding domain: $domain"
    fi
done < "/tmp/unique_domains.txt"

# Add included domains
if [[ -n "$include_domain" ]]; then
    echo "[+] Adding included domains..."
    temp_include_file="/tmp/include_domains.txt"
    > "$temp_include_file"
    
    # Add include domains to temp file
    echo "$include_domain" >> "$temp_include_file"
    
    # Combine filtered domains with include domains and remove duplicates
    cat "$filtered_domains_file" "$temp_include_file" | sort | uniq > "/tmp/final_domains.txt"
    rm -f "$temp_include_file"
else
    cp "$filtered_domains_file" "/tmp/final_domains.txt"
fi

# Count final domains
final_count=$(wc -l < "/tmp/final_domains.txt")
echo "[+] Final domain count: $final_count"

# Check Cloudflare CDN status
if [[ $final_count -gt 0 ]]; then
    process_domains_parallel "/tmp/final_domains.txt"
else
    echo "[-] No domains to process"
    touch /storage/cf_domain.txt
    touch /storage/not_cf_domain.txt
fi

# Cleanup temporary files
rm -f /tmp/all_domains.txt /tmp/unique_domains.txt /tmp/filtered_domains.txt /tmp/final_domains.txt

echo "[+] Processing complete!"
echo "[+] Results saved to:"
echo "    - Cloudflare domains: /storage/cf_domain.txt"
echo "    - Non-Cloudflare domains: /storage/not_cf_domain.txt"