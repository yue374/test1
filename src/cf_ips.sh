#!/bin/bash
source src/utils.sh

mapfile -t ids <<< "$profile_id"

# Count how many IDs
num_profile_id=${#ids[@]}
export num_profile_id

# Export each as profile_idN
for i in "${!ids[@]}"; do
  n=$((i + 1))
  export "profile_id$n=${ids[$i]}"
done

# Debug print (you can remove this block)
echo "num_profile_id=$num_profile_id"
for i in $(seq 1 $num_profile_id); do
  eval echo "profile_id$i=\$profile_id$i"
done
