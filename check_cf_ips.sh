#!/bin/bash

# Colored output logs
green_log() {
    echo -e "\e[32m$1\e[0m"
}
red_log() {
    echo -e "\e[31m$1\e[0m"
}

# Check github connect
gh_connect=$(curl -fsSL "https://raw.githubusercontent.com/$repository/refs/heads/main/src/utils.sh" 2>/dev/null)
if [[ -n "$gh_connect" ]]; then
    green_log "✅ Github connect OK!"
else
    red_log "❌ Github connect not stable"
    exit 1
fi

# Check NextDNS API, Profile ID
if [[ -z "${nextdns_api}" ]]; then
    red_log "❌ Missing NextDNS API"
    exit 1
fi
if [[ -z "${profile_id}" ]]; then
    red_log "❌ Missing NextDNS Profile ID"
    exit 1
fi
if curl -s --max-time 5 "https://api.nextdns.io/profiles/" > /dev/null 2>&1; then
    green_log "✅ Connect to NextDNS API OK!"
else
    red_log "⚠️ Can't connect to NextDNS server"
    exit 1
fi
