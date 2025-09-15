#!/usr/bin/env bash

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


# Ensure storage dir exists (relative to script location)
mkdir -p ./storage

# Split profile_id into array
mapfile -t ids <<< "$profile_id"
num_profile_id=${#ids[@]}
echo "[+] Number Profiles ID: $num_profile_id"

# Request all profiles
all_domains=()
for id in "${ids[@]}"; do
  json=$(curl -s -X GET \
    "https://api.nextdns.io/profiles/$id/analytics/domains?status=default%2Callowed&from=-30d&limit=1000" \
    -H "X-Api-Key: $nextdns_api")

  # Extract domain list
  domains=$(jq -r '.data[].domain' <<<"$json")
  all_domains+=($domains)
done

# Deduplicate
unique_domains=$(printf "%s\n" "${all_domains[@]}" | sort -u)

# Apply exclude (remove root + subdomains)
if [[ -n "${exclude_domain:-}" ]]; then
  while IFS= read -r ex; do
    [[ -z "$ex" ]] && continue
    unique_domains=$(printf "%s\n" "$unique_domains" | grep -vE "(\.|^)$ex$")
  done <<< "$exclude_domain"
fi

# Apply include (force add)
if [[ -n "${include_domain:-}" ]]; then
  unique_domains=$(printf "%s\n%s\n" "$unique_domains" "$include_domain" | sort -u)
fi

# Save temporary list
domain_list=$(mktemp)
printf "%s\n" $unique_domains > "$domain_list"

# Functions to check domain
check_domain() {
  d="$1"
  res=$(curl -s --max-time 10 "https://$d/cdn-cgi/trace" || true)
  if grep -q "warp=off" <<<"$res" && grep -q "gateway=off" <<<"$res"; then
    echo "$d" >> ./storage/cf_domain.txt
  else
    echo "$d" >> ./storage/not_cf_domain.txt
  fi
}

export -f check_domain
rm -f ./storage/cf_domain.txt ./storage/not_cf_domain.txt

# Run parallel check (50 at once)
cat "$domain_list" | xargs -n1 -P50 bash -c 'check_domain "$@"' _

echo "[+] Done. Results saved in ./storage/cf_domain.txt and ./storage/not_cf_domain.txt"
