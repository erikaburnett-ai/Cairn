#!/usr/bin/env bash
# Download the UK postcode file once, then resolve postcodes forever offline.
#
# Source: Ordnance Survey Code-Point Open, via getthedata.com.
# Open Government Licence. No registration, no account, no key.
#
# After this, any UK postcode you type is looked up on disk and nothing
# about where you're going leaves the machine.
set -euo pipefail

CACHE="$(cd "$(dirname "$0")" && pwd)/cache"
mkdir -p "$CACHE"
cd "$CACHE"

if [ -f postcodes.csv ]; then
  echo "Already have it: $(du -h postcodes.csv | cut -f1), $(wc -l < postcodes.csv | tr -d ' ') postcodes."
  echo "Delete cache/postcodes.csv and re-run to refresh."
  exit 0
fi

echo "Downloading UK postcodes (Ordnance Survey Code-Point Open, OGL)..."
curl -L --progress-bar -o postcodes.zip \
  "https://www.getthedata.com/downloads/open_postcode_geo.csv.zip"

echo "Unpacking..."
unzip -o -q postcodes.zip
rm -f postcodes.zip

# The archive ships as open_postcode_geo.csv
if [ -f open_postcode_geo.csv ]; then
  mv open_postcode_geo.csv postcodes.csv
fi

if [ ! -f postcodes.csv ]; then
  echo "Something unexpected in the archive. Contents:"
  ls -la
  exit 1
fi

echo
echo "Done. $(du -h postcodes.csv | cut -f1), $(wc -l < postcodes.csv | tr -d ' ') postcodes."
echo "Postcodes now resolve offline. Try:"
echo "  python3 cairn.py --from 'G1 1AB' --to 'KW14 8AA' --offline"
