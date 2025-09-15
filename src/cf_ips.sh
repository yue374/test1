#!/bin/bash
source src/utils.sh

mkdir /storage

# Count how many Profiles IDs input
mapfile -t ids <<< "$profile_id"
num_profile_id=${#ids[@]}
export num_profile_id
for i in "${!ids[@]}"; do
  n=$((i + 1))
  export "profile_id$n=${ids[$i]}"
done
echo "[+] Number Profiles ID: $num_profile_id"

# Request Domains in NextDNS API
declare -A domain_set

for i in $(seq 1 "$num_profile_id"); do
    profile_var="profile_id$i"
    profile_id="${!profile_var}"

    echo "Fetching domains for profile: $profile_id" >&2

    response=$(curl -s -X GET \
      "https://api.nextdns.io/profiles/$profile_id/analytics/domains?status=default%2Callowed&from=-30d&limit=1000" \
      -H "X-Api-Key: $nextdns_api")

    # Extract domains from JSON
    domains=$(echo "$response" | jq -r '.data[].domain' 2>/dev/null || true)

    for d in $domains; do
        domain_set["$d"]=1
    done
done

# Remove excluded domains and subdomains
if [[ -n "${exclude_domain:-}" ]]; then
    while IFS= read -r ex; do
        [[ -z "$ex" ]] && continue
        for d in "${!domain_set[@]}"; do
            if [[ "$d" == *".$ex" || "$d" == "$ex" ]]; then
                unset "domain_set[$d]"
            fi
        done
    done <<< "$exclude_domain"
fi

# Add include domains
if [[ -n "${include_domain:-}" ]]; then
    while IFS= read -r inc; do
        [[ -z "$inc" ]] && continue
        domain_set["$inc"]=1
    done <<< "$include_domain"
fi

# Prepare output files
cf_file="/storage/cf_domain.txt"
not_cf_file="/storage/not_cf_domain.txt"
> "$cf_file"
> "$not_cf_file"

# Function to check Cloudflare
check_cf() {
    local d="$1"
    trace=$(curl -s --max-time 10 "https://$d/cdn-cgi/trace" || true)

    if [[ "$trace" == *"warp=off"* && "$trace" == *"gateway=off"* ]]; then
        echo "$d" >> "$cf_file"
    else
        echo "$d" >> "$not_cf_file"
    fi
}

export -f check_cf
export cf_file not_cf_file

# Run checks in parallel (50 at once)
printf "%s\n" "${!domain_set[@]}" | xargs -n1 -P50 -I{} bash -c 'check_cf "$@"' _ {}

echo "Done."
echo "Cloudflare domains saved to $cf_file"
echo "Non-Cloudflare domains saved to $not_cf_file"