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


# Read profile IDs into array
mapfile -t ids <<< "$profile_id"
num_profile_id=${#ids[@]}
echo "[+] Number Profiles ID: $num_profile_id"

# Temporary file to store all domains
tmpfile=$(mktemp)

# Fetch domains in parallel (50 at a time)
printf "%s\n" "${ids[@]}" | xargs -P50 -I{} bash -c '
  curl -s -X GET "https://api.nextdns.io/profiles/{}/analytics/domains?status=default%2Callowed&from=-30d&limit=1000" \
    -H "X-Api-Key: $nextdns_api" \
  | jq -r ".data[].domain"
' >> "$tmpfile"

# Merge domains & remove duplicates
mapfile -t all_domains < <(sort -u "$tmpfile")
rm -f "$tmpfile"

# Convert exclude/include domains into arrays
mapfile -t excludes <<< "$exclude_domain"
mapfile -t includes <<< "$include_domain"

# Remove excluded domains and their subdomains
filtered_domains=()
for domain in "${all_domains[@]}"; do
  skip=false
  for ex in "${excludes[@]}"; do
    if [[ "$domain" == *".$ex" || "$domain" == "$ex" ]]; then
      skip=true
      break
    fi
  done
  $skip || filtered_domains+=("$domain")
done

# Add includes (ensure uniqueness)
for inc in "${includes[@]}"; do
  if [[ ! " ${filtered_domains[*]} " =~ " $inc " ]]; then
    filtered_domains+=("$inc")
  fi
done

# Prepare output files
cf_file="/storage/cf_domain.txt"
not_cf_file="/storage/not_cf_domain.txt"
: > "$cf_file"
: > "$not_cf_file"

# Check Cloudflare status in parallel (50 at once)
printf "%s\n" "${filtered_domains[@]}" | xargs -P50 -I{} bash -c '
  resp=$(curl -s --max-time 10 "https://{}/cdn-cgi/trace" || true)
  if [[ "$resp" == *"warp=off"* && "$resp" == *"gateway=off"* ]]; then
    echo "{}" >> "'"$cf_file"'"
  else
    echo "{}" >> "'"$not_cf_file"'"
  fi
'

echo "[+] Done. Results:"
echo "  Cloudflare domains: $cf_file"
echo "  Non-Cloudflare domains: $not_cf_file"