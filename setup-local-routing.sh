#!/usr/bin/env bash
# Self-hosted routing for fuelrun, so journeys never leave the machine.
#
# Picks whichever engine the machine can actually run:
#   Docker present  -> OSRM        (fastest routing, heaviest setup)
#   Java present    -> GraphHopper (one JAR, no Docker needed)
#   Neither         -> tells you what to install and stops
#
# Afterwards:  python3 fuelrun.py --osrm http://localhost:5000 --offline ...
set -euo pipefail

REGION="${1:-scotland}"            # scotland | great-britain
DIR="$(cd "$(dirname "$0")" && pwd)/local-routing"
mkdir -p "$DIR"
cd "$DIR"

case "$REGION" in
  scotland)      URL="https://download.geofabrik.de/europe/great-britain/scotland-latest.osm.pbf" ;;
  great-britain) URL="https://download.geofabrik.de/europe/great-britain-latest.osm.pbf" ;;
  *) echo "Usage: $0 [scotland|great-britain]"; exit 1 ;;
esac
PBF="$(basename "$URL")"

echo "=============================================================="
echo " fuelrun: self-hosted routing"
echo " Region: $REGION"
echo " Folder: $DIR"
echo "=============================================================="

# ---------------------------------------------------------------- map data
if [ -f "$PBF" ]; then
  echo "[1/3] Map data already here ($(du -h "$PBF" | cut -f1))."
else
  echo "[1/3] Downloading map data. Great Britain is about 2.2 GB, Scotland far less."
  curl -L --progress-bar -o "$PBF.part" "$URL"
  mv "$PBF.part" "$PBF"
  echo "      Got $(du -h "$PBF" | cut -f1)."
fi

# ---------------------------------------------------------------- engine
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  ENGINE=osrm
elif command -v java >/dev/null 2>&1; then
  ENGINE=graphhopper
else
  cat <<'MSG'

[2/3] No routing engine available.

You need ONE of these. Pick whichever you'd rather have on the machine.

  Java  (lighter, recommended)
    macOS:   brew install openjdk@21
    Ubuntu:  sudo apt install default-jre
    Windows: https://adoptium.net

  Docker (heavier, faster routing)
    https://docs.docker.com/get-docker/

Install one, then run this script again. The map data above is already
downloaded, so it picks up where it left off.
MSG
  exit 1
fi
echo "[2/3] Using $ENGINE."

# ---------------------------------------------------------------- build and run
if [ "$ENGINE" = "osrm" ]; then
  BASE="${PBF%.osm.pbf}"
  if [ ! -f "$BASE.osrm.mldgr" ]; then
    echo "      Processing the map. This takes a while and uses a lot of memory."
    docker run --rm -v "$DIR:/data" osrm/osrm-backend \
      osrm-extract -p /opt/car.lua "/data/$PBF"
    docker run --rm -v "$DIR:/data" osrm/osrm-backend osrm-partition "/data/$BASE.osrm"
    docker run --rm -v "$DIR:/data" osrm/osrm-backend osrm-customize "/data/$BASE.osrm"
  fi
  echo "[3/3] Starting OSRM on http://localhost:5000"
  echo "      Leave this window open. Ctrl-C to stop."
  exec docker run --rm -t -i -p 5000:5000 -v "$DIR:/data" osrm/osrm-backend \
    osrm-routed --algorithm mld "/data/$BASE.osrm"
fi

if [ "$ENGINE" = "graphhopper" ]; then
  GH_VER="10.0"
  JAR="graphhopper-web-$GH_VER.jar"
  if [ ! -f "$JAR" ]; then
    echo "      Fetching GraphHopper $GH_VER"
    curl -L --progress-bar -o "$JAR" \
      "https://repo1.maven.org/maven2/com/graphhopper/graphhopper-web/$GH_VER/$JAR"
  fi
  if [ ! -f config.yml ]; then
    cat > config.yml <<YML
graphhopper:
  datareader.file: $PBF
  graph.location: graph-cache
  profiles:
    - name: car
      vehicle: car
      weighting: fastest
  profiles_ch:
    - profile: car
server:
  application_connectors:
    - type: http
      port: 5000
      bind_host: 127.0.0.1
  admin_connectors:
    - type: http
      port: 5001
      bind_host: 127.0.0.1
YML
  fi
  echo "[3/3] Starting GraphHopper on http://localhost:5000"
  echo "      First run builds a routing graph and takes several minutes."
  echo "      Leave this window open. Ctrl-C to stop."
  exec java -Xmx4g -jar "$JAR" server config.yml
fi
