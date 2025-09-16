#!/bin/bash
mkdir -p ./storage

# Colored output logs
green_log() {
    echo -e "\e[32m$1\e[0m"
}
red_log() {
    echo -e "\e[31m$1\e[0m"
}
yellow_log() {
    echo -e "\e[33m$1\e[0m"
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
    
    # Remove null bytes and extract domains
    echo "$response" \
        | tr -d '\000' \
        | grep -o '"domain":"[^"]*"' \
        | sed 's/"domain":"\([^"]*\)"/\1/'
}

# Fetch domains from all profiles
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

# Clear the output file
> ./storage/cf_domain.txt

# Check if domain uses Cloudflare
check_cloudflare() {
    local domain="$1"
    local response
    local timeout=10
    
    # Fetch the response and remove null bytes
    response=$(curl -s --connect-timeout "$timeout" --max-time "$timeout" \
        "https://${domain}/cdn-cgi/trace" 2>/dev/null | tr -d '\000')
    
    # Check for warp=off in the cleaned response
    if [[ -n "$response" ]] && echo "$response" | grep -q "warp=off" 2>/dev/null; then
        # Use printf to ensure clean output without null bytes
        printf "%s\n" "$domain" >> ./storage/cf_domain.txt
    fi
}

export -f check_cloudflare

# Process domains in parallel
max_parallel=50

for domain in "${final_domains[@]}"; do
    while [[ $(jobs -r | wc -l) -ge $max_parallel ]]; do
        sleep 0.1
    done
    
    check_cloudflare "$domain" &
done

# Wait for all jobs to complete
wait

# Count results safely by removing any null bytes from the file first
if [[ -f ./storage/cf_domain.txt ]]; then
    # Clean the file from any null bytes and count lines
    tr -d '\000' < ./storage/cf_domain.txt > ./storage/cf_domain_clean.txt
    mv ./storage/cf_domain_clean.txt ./storage/cf_domain.txt
    cf_count=$(wc -l < ./storage/cf_domain.txt 2>/dev/null || echo 0)
else
    cf_count=0
fi

green_log "================================================"
green_log "Domains with Cloudflare CDN: $cf_count"
green_log "================================================"

# Store fastest IPs and past CF domains for each profile
declare -A profile_fastest_ips
declare -A profile_past_domains
declare -A profile_domain_ids

# Function to fetch rewrites from a profile
fetch_rewrites_from_profile() {
    local profile="$1"
    local response
    local retry_count=0
    local max_retries=2
    
    while [[ $retry_count -lt $max_retries ]]; do
        response=$(curl -s -X GET "https://api.nextdns.io/profiles/${profile}/rewrites" \
            -H "X-Api-Key: $nextdns_api")
        
        if [[ $? -eq 0 ]] && [[ -n "$response" ]]; then
            echo "$response"
            return 0
        fi
        
        retry_count=$((retry_count + 1))
        if [[ $retry_count -lt $max_retries ]]; then
            red_log "[!] Failed to fetch rewrites for profile $profile. Retrying in 2 minutes..."
            sleep 120
        fi
    done
    
    red_log "[!] Failed to fetch rewrites for profile $profile after $max_retries attempts"
    return 1
}

# Function to extract fastest IP from rewrites response
extract_fastest_ip() {
    local json="$1"
    local fastest_ip
    
    # Find the nextdns.cloudflare.fastest.ip.com entry and extract its IP
    fastest_ip=$(echo "$json" | grep -o '"name":"nextdns\.cloudflare\.fastest\.ip\.com"[^}]*' | \
        grep -o '"content":"[^"]*"' | sed 's/"content":"\([^"]*\)"/\1/')
    
    echo "$fastest_ip"
}

# Function to extract domains with specific IP from rewrites
extract_domains_with_ip() {
    local json="$1"
    local target_ip="$2"
    local domains=""
    

    # Fallback to grep/sed if Python is not available
    echo "$json" | tr ',' '\n' | grep -B1 "\"content\":\"$target_ip\"" | \
        grep '"name":"' | grep -v 'nextdns.cloudflare.fastest.ip.com' | \
        sed 's/.*"name":"\([^"]*\)".*/\1/'

}

# Fetch rewrites for all profiles and extract fastest IPs and past CF domains
echo "[*] Fetching rewrites from all profiles..."

for profile in "${ids[@]}"; do
    echo "[*] Processing profile: $profile"
    
    rewrites_json=$(fetch_rewrites_from_profile "$profile")
    if [[ $? -ne 0 ]]; then
        red_log "[!] Skipping profile $profile due to API errors"
        continue
    fi
    
    # Extract fastest IP for this profile
    fastest_ip=$(extract_fastest_ip "$rewrites_json")
    
    if [[ -z "$fastest_ip" ]]; then
        yellow_log "[!] No fastest IP found for profile $profile"
        continue
    fi
    
    profile_fastest_ips["$profile"]="$fastest_ip"
    green_log "[+] Profile $profile - Fastest IP: $fastest_ip"
    
    # Extract past CF domains
    while IFS=: read -r domain domain_id; do
        if [[ -n "$domain" ]]; then
            profile_past_domains["${profile}:${domain}"]="1"
            profile_domain_ids["${profile}:${domain}"]="$domain_id"
        fi
    done <<< "$(extract_domains_with_ip "$rewrites_json" "$fastest_ip")"
done

# Load current CF domains
declare -A current_cf_domains
if [[ -f ./storage/cf_domain.txt ]]; then
    while IFS= read -r domain; do
        if [[ -n "$domain" ]]; then
            current_cf_domains["$domain"]="1"
        fi
    done < ./storage/cf_domain.txt
fi

# Function to delete a rewrite
delete_rewrite() {
    local profile="$1"
    local domain_id="$2"
    local domain_name="$3"
    local retry_count=0
    local max_retries=2
    
    while [[ $retry_count -lt $max_retries ]]; do
        response=$(curl -s -X DELETE "https://api.nextdns.io/profiles/${profile}/rewrites/${domain_id}" \
            -H "X-Api-Key: $nextdns_api" \
            -w "\n%{http_code}")
        
        http_code=$(echo "$response" | tail -n1)
        
        if [[ "$http_code" == "200" ]] || [[ "$http_code" == "204" ]]; then
            sleep 0.5  # 500ms delay
            return 0
        fi
        
        retry_count=$((retry_count + 1))
        if [[ $retry_count -lt $max_retries ]]; then
            red_log "[!] Failed to delete $domain_name. Waiting 2 minutes before retry..."
            sleep 120
        fi
    done
    
    red_log "[!] Failed to delete $domain_name after $max_retries attempts"
    return 1
}

# Function to add a rewrite
add_rewrite() {
    local profile="$1"
    local domain="$2"
    local ip="$3"
    local retry_count=0
    local max_retries=2
    local json_payload="{\"name\":\"${domain}\",\"content\":\"${ip}\"}"
    
    while [[ $retry_count -lt $max_retries ]]; do
        response=$(curl -s -X POST "https://api.nextdns.io/profiles/${profile}/rewrites" \
            -H "X-Api-Key: $nextdns_api" \
            -H "Content-Type: application/json" \
            -d "$json_payload" \
            -w "\n%{http_code}")
        
        http_code=$(echo "$response" | tail -n1)
        
        if [[ "$http_code" == "200" ]] || [[ "$http_code" == "201" ]]; then
            sleep 0.5
            return 0
        fi
        
        retry_count=$((retry_count + 1))
        if [[ $retry_count -lt $max_retries ]]; then
            red_log "[!] Failed to add $domain. Waiting 2 minutes before retry..."
            sleep 120
        fi
    done
    
    red_log "[!] Failed to add $domain after $max_retries attempts"
    return 1
}

# Process deletions domains
yellow_log "================================================"
yellow_log "Processing domain deletions..."
yellow_log "================================================"

declare -a domains_to_delete
for profile in "${ids[@]}"; do
    if [[ -z "${profile_fastest_ips[$profile]}" ]]; then
        continue
    fi
    
    for key in "${!profile_past_domains[@]}"; do
        if [[ "$key" == "${profile}:"* ]]; then
            domain="${key#${profile}:}"
            if [[ -z "${current_cf_domains[$domain]}" ]]; then
                domains_to_delete+=("${profile}:${domain}")
            fi
        fi
    done
done

if [[ ${#domains_to_delete[@]} -gt 0 ]]; then
    echo "[*] Domains to delete: ${#domains_to_delete[@]}"
    for item in "${domains_to_delete[@]}"; do
        IFS=: read -r profile domain <<< "$item"
        domain_id="${profile_domain_ids[${profile}:${domain}]}"
        
        if [[ -n "$domain_id" ]]; then
            delete_rewrite "$profile" "$domain_id" "$domain"
            if [[ $? -ne 0 ]]; then
                red_log "[!] Stopping due to API errors"
                exit 1
            fi
        fi
    done
else
    echo "[*] No domains to delete"
fi

# Process additions domains
yellow_log "================================================"
yellow_log "Processing domain additions..."
yellow_log "================================================"

declare -a domains_to_add
for domain in "${!current_cf_domains[@]}"; do
    needs_add=1
    for profile in "${ids[@]}"; do
        if [[ -n "${profile_past_domains[${profile}:${domain}]}" ]]; then
            needs_add=0
            break
        fi
    done
    if [[ $needs_add -eq 1 ]]; then
        domains_to_add+=("$domain")
    fi
done

if [[ ${#domains_to_add[@]} -gt 0 ]]; then
    echo "[*] Domains to add: ${#domains_to_add[@]}"
    for domain in "${domains_to_add[@]}"; do
        for profile in "${ids[@]}"; do
            if [[ -n "${profile_fastest_ips[$profile]}" ]]; then
                add_rewrite "$profile" "$domain" "${profile_fastest_ips[$profile]}"
                if [[ $? -ne 0 ]]; then
                    red_log "[!] Stopping due to API errors"
                    exit 1
                fi
            fi
        done
    done
else
    echo "[*] No domains to add"
fi

green_log "================================================"
green_log "NextDNS Rewrites Complete!"
green_log "================================================"