#!/bin/sh
mkdir -p /tmp/ical
cd /tmp/ical

ICAL_URL=$(jq -r '.ical_url' /data/options.json)
URL="${ICAL_URL/\{year\}/$(date +%Y)}"

fetch() {
  echo "[$(date '+%H:%M:%S')] Fetching: $URL"
  if curl -fsSL "$URL" -o calendar.ics; then
    echo "[$(date '+%H:%M:%S')] OK - next run in 12h"
  else
    echo "[$(date '+%H:%M:%S')] FAILED - retrying in 12h"
  fi
}

fetch
while true; do
#wait 12 hours
  sleep 43200
  fetch
done &

python3 -m http.server 6868
