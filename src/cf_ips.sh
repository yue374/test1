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


#!/usr/bin/env bash
set -euo pipefail

# Ensure storage exists
mkdir -p /storage

# Split profile IDs into array
mapfile -t ids <<< "$profile_id"
num_profile_id=${#ids[@]}
export num_profile_id
for i in "${!ids[@]}"; do
  n=$((i + 1))
  export "profile_id$n=${ids[$i]}"
done
echo "[+] Number Profiles ID: $num_profile_id"

# Collect all domains
all_domains=()

for pid in "${ids[@]}"; do
  echo "[*] Fetching domains for profile: $pid"
  json=$(curl -s -X GET \
    "https://api.nextdns.io/profiles/$pid/analytics/domains?status=default%2Callowed&from=-30d&limit=1000" \
    -H "X-Api-Key: $nextdns_api")

  # Extract domain list
  domains=$(jq -r '.data[].domain' <<< "$json" || true)
  all_domains+=($domains)
done

# Deduplicate
unique_domains=$(printf "%s\n" "${all_domains[@]}" | sort -u)

# Apply exclude filter (remove domain + subdomains)
if [[ -n "${exclude_domain:-}" ]]; then
  while IFS= read -r ex; do
    [[ -z "$ex" ]] && continue
    unique_domains=$(grep -v -E "(^|\\.)${ex//./\\.}$" <<< "$unique_domains" || true)
  done <<< "$exclude_domain"
fi

# Apply include filter (ensure included domains exist)
if [[ -n "${include_domain:-}" ]]; then
  unique_domains=$(printf "%s\n%s\n" "$unique_domains" "$include_domain" | sort -u)
fi

echo "[+] Total domains after filtering: $(wc -l <<< "$unique_domains")"

# Cloudflare check function
check_cf() {
  domain="$1"
  if out=$(curl -s --max-time 10 "https://$domain/cdn-cgi/trace" 2>/dev/null); then
    if grep -q "warp=off" <<< "$out" && grep -q "gateway=off" <<< "$out"; then
      echo "$domain" >> /storage/cf_domain.txt
    else
      echo "$domain" >> /storage/not_cf_domain.txt
    fi
  else
    echo "$domain" >> /storage/not_cf_domain.txt
  fi
}

export -f check_cf
rm -f /storage/cf_domain.txt /storage/not_cf_domain.txt

# Run checks in parallel (50 at a time)
printf "%s\n" "$unique_domains" | xargs -n1 -P50 bash -c 'check_cf "$@"' _

echo "[+] Done. Results saved:"
echo "  - /storage/cf_domain.txt"
echo "  - /storage/not_cf_domain.txt"
