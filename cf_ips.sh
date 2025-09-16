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
    
    response=$(curl -s --connect-timeout "$timeout" --max-time "$timeout" \
        "https://${domain}/cdn-cgi/trace" 2>/dev/null | tr -d '\000')
    if [[ -n "$response" ]] && echo "$response" | grep -q "warp=off" 2>/dev/null; then
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

# Count CF domain
if [[ -f ./storage/cf_domain.txt ]]; then
    tr -d '\000' < ./storage/cf_domain.txt > ./storage/cf_domain_clean.txt
    mv ./storage/cf_domain_clean.txt ./storage/cf_domain.txt
    cf_count=$(wc -l < ./storage/cf_domain.txt 2>/dev/null || echo 0)
else
    cf_count=0
fi

green_log "================================================"
green_log "Domains with Cloudflare CDN: $cf_count"
green_log "================================================"

# Store fastest IPs for each profile
declare -A fastest_ips

# Function to get fastest IP from profile rewrites
get_fastest_ip_from_profile() {
    local profile="$1"
    local response
    
    echo "[*] Getting fastest IP from profile: $profile"
    
    response=$(curl -s -X GET "https://api.nextdns.io/profiles/${profile}/rewrites" \
        -H "X-Api-Key: $nextdns_api")
    
    # Extract IP for nextdns.cloudflare.fastest.ip.com
    local fastest_ip=$(echo "$response" | grep -o '"name":"nextdns.cloudflare.fastest.ip.com"[^}]*' | \
        grep -o '"content":"[^"]*"' | sed 's/"content":"\([^"]*\)"/\1/')
    
    if [[ -n "$fastest_ip" ]]; then
        fastest_ips["$profile"]="$fastest_ip"
        green_log "[+] Profile $profile fastest IP: $fastest_ip"
    else
        red_log "[-] No fastest IP found for profile $profile"
        exit 1
    fi
}

# Get fastest IPs for all profiles
for profile in "${ids[@]}"; do
    get_fastest_ip_from_profile "$profile"
done

# Function to make API request with retry logic
make_api_request() {
    local method="$1"
    local url="$2"
    local data="$3"
    local max_retries=2
    local retry_count=0
    
    while [[ $retry_count -lt $max_retries ]]; do
        if [[ "$method" == "DELETE" ]]; then
            response=$(curl -s -w "\n%{http_code}" -X DELETE "$url" \
                -H "X-Api-Key: $nextdns_api")
        elif [[ "$method" == "POST" ]]; then
            response=$(curl -s -w "\n%{http_code}" -X POST "$url" \
                -H "X-Api-Key: $nextdns_api" \
                -H "Content-Type: application/json" \
                -d "$data")
        else
            response=$(curl -s -w "\n%{http_code}" -X GET "$url" \
                -H "X-Api-Key: $nextdns_api")
        fi
        
        http_code=$(echo "$response" | tail -n 1)
        body=$(echo "$response" | head -n -1)
        
        if [[ "$http_code" -ge 200 && "$http_code" -lt 300 ]]; then
            echo "$body"
            return 0
        else
            retry_count=$((retry_count + 1))
            if [[ $retry_count -lt $max_retries ]]; then
                red_log "[-] API request failed (HTTP $http_code). Waiting 2 minutes before retry..."
                sleep 120
            else
                red_log "[-] API request failed after $max_retries attempts. Exiting."
                exit 1
            fi
        fi
    done
}

# Process past CF domain list
declare -A past_domains_map
if [[ -n "$cf_domain" ]]; then
    echo "[*] Processing past CF domain list..."
    while IFS= read -r domain; do
        domain=$(echo "$domain" | tr -d '\r' | xargs)
        if [[ -n "$domain" ]]; then
            past_domains_map["$domain"]=1
        fi
    done <<< "$cf_domain"
    echo "[+] Past CF domains count: ${#past_domains_map[@]}"
fi

# Read current CF domains
declare -A current_domains_map
if [[ -f ./storage/cf_domain.txt ]]; then
    while IFS= read -r domain; do
        domain=$(echo "$domain" | tr -d '\r' | xargs)
        if [[ -n "$domain" ]]; then
            current_domains_map["$domain"]=1
        fi
    done < ./storage/cf_domain.txt
fi
echo "[+] Current CF domains count: ${#current_domains_map[@]}"

# Function to get domain ID from rewrites
get_domain_id() {
    local profile="$1"
    local domain="$2"
    local fastest_ip="${fastest_ips[$profile]}"
    
    response=$(make_api_request "GET" "https://api.nextdns.io/profiles/${profile}/rewrites")
    
    # Find domain with matching name and fastest IP
    domain_id=$(echo "$response" | \
        grep -o "\"id\":\"[^\"]*\",\"name\":\"$domain\"[^}]*\"content\":\"$fastest_ip\"" | \
        grep -o '"id":"[^"]*"' | \
        sed 's/"id":"\([^"]*\)"/\1/' | \
        head -n 1)
    
    echo "$domain_id"
}

# Remove domains that are in past but not in current
if [[ -n "$cf_domain" ]]; then
    echo "[*] Checking for domains to remove..."
    for domain in "${!past_domains_map[@]}"; do
        if [[ -z "${current_domains_map[$domain]}" ]]; then
            echo "[*] Removing domain: $domain"
            
            for profile in "${ids[@]}"; do
                domain_id=$(get_domain_id "$profile" "$domain")
                
                if [[ -n "$domain_id" ]]; then
                    echo "[*] Deleting $domain (ID: $domain_id) from profile $profile"
                    make_api_request "DELETE" "https://api.nextdns.io/profiles/${profile}/rewrites/${domain_id}"
                    sleep 0.5
                fi
            done
        fi
    done
fi

# Add domains that are in current but not in past
echo "[*] Checking for domains to add..."
for domain in "${!current_domains_map[@]}"; do
    if [[ -z "${past_domains_map[$domain]}" ]] || [[ -z "$cf_domain" ]]; then
        echo "[*] Adding domain: $domain"
        
        for profile in "${ids[@]}"; do
            fastest_ip="${fastest_ips[$profile]}"
            if [[ -n "$fastest_ip" ]]; then
                json_data="{\"name\":\"$domain\",\"content\":\"$fastest_ip\"}"
                echo "[*] Adding $domain to profile $profile with IP $fastest_ip"
                make_api_request "POST" "https://api.nextdns.io/profiles/${profile}/rewrites" "$json_data"
                sleep 0.5
            fi
        done
    fi
done

green_log "[+] NextDNS rewrites synchronization completed"

# Update GitHub repository variable
if [[ -n "$GITHUB_TOKEN" ]] && [[ -n "$GITHUB_REPOSITORY" ]]; then
    echo "[*] Updating GitHub repository variable CF_DOMAIN..."
    
    # Read all domains from cf_domain.txt
    if [[ -f ./storage/cf_domain.txt ]]; then
        cf_domains_content=$(cat ./storage/cf_domain.txt)
    else
        cf_domains_content=""
    fi
    
    # Prepare JSON payload
    json_payload=$(jq -n --arg value "$cf_domains_content" '{name: "CF_DOMAIN", value: $value}')
    
    # Get repository owner and name
    repo_owner=$(echo "$GITHUB_REPOSITORY" | cut -d'/' -f1)
    repo_name=$(echo "$GITHUB_REPOSITORY" | cut -d'/' -f2)
    
    # Update or create repository variable
    response=$(curl -s -w "\n%{http_code}" -X PATCH \
        "https://api.github.com/repos/${repo_owner}/${repo_name}/actions/variables/CF_DOMAIN" \
        -H "Accept: application/vnd.github+json" \
        -H "Authorization: Bearer $GITHUB_TOKEN" \
        -H "X-GitHub-Api-Version: 2022-11-28" \
        -d "$json_payload")
    
    http_code=$(echo "$response" | tail -n 1)
    
    if [[ "$http_code" == "404" ]]; then
        # Variable doesn't exist, create it
        echo "[*] Creating new GitHub variable CF_DOMAIN..."
        response=$(curl -s -w "\n%{http_code}" -X POST \
            "https://api.github.com/repos/${repo_owner}/${repo_name}/actions/variables" \
            -H "Accept: application/vnd.github+json" \
            -H "Authorization: Bearer $GITHUB_TOKEN" \
            -H "X-GitHub-Api-Version: 2022-11-28" \
            -d "$json_payload")
        
        http_code=$(echo "$response" | tail -n 1)
    fi
    
    if [[ "$http_code" -ge 200 && "$http_code" -lt 300 ]]; then
        green_log "[+] GitHub variable CF_DOMAIN updated successfully"
    else
        red_log "[-] Failed to update GitHub variable CF_DOMAIN (HTTP $http_code)"
    fi
else
    echo "[!] Skipping GitHub update (GITHUB_TOKEN or GITHUB_REPOSITORY not set)"
fi

green_log "================================================"
green_log "Script execution completed successfully!"
green_log "================================================"