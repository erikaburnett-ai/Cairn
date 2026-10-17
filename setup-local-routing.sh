#!/usr/bin/env bash
# Self-hosted routing for fuelrun, so journeys never leave the machine.
#
# Picks whichever engine the machine can actually run:
#   Docker present  -> OSRM        (fastest routing, heaviest setup)
#   Java present    -> GraphHopper (one JAR, no Docker needed)
#   Neither         -> tells you what to install and stops
#
# Afterwards:  python3 fuelrun.py --osrm http://localhost:8989 --offline ...
set -euo pipefail

REGION="${1:-scotland}"            # scotland | great-britain
DIR="$(cd "$(dirname "$0")" && pwd)/local-routing"
mkdir -p "$DIR"
cd "$DIR"

case "$REGION" in
  scotland)      URL="https://download.geofabrik.de/europe/united-kingdom/scotland-latest.osm.pbf" ;;
  england)       URL="https://download.geofabrik.de/europe/united-kingdom/england-latest.osm.pbf" ;;
  wales)         URL="https://download.geofabrik.de/europe/united-kingdom/wales-latest.osm.pbf" ;;
  ireland)       URL="https://download.geofabrik.de/europe/ireland-and-northern-ireland-latest.osm.pbf" ;;
  great-britain) URL="https://download.geofabrik.de/europe/great-britain-latest.osm.pbf" ;;
  *) echo "Usage: $0 [scotland|england|wales|ireland|great-britain]"; exit 1 ;;
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
  curl -L --fail --progress-bar -o "$PBF.part" "$URL" || {
    echo "      Download failed. Check the region name."; rm -f "$PBF.part"; exit 1; }
  if [ "$(wc -c < "$PBF.part")" -lt 1000000 ]; then
    echo "      That is not a map file. Geofabrik returned:"; head -c 200 "$PBF.part"; echo
    rm -f "$PBF.part"; exit 1
  fi
  mv "$PBF.part" "$PBF"
  echo "      Got $(du -h "$PBF" | cut -f1)."
fi

# ---------------------------------------------------------------- engine
# a JDK unpacked into the home folder needs no admin rights at all
for h in "$HOME"/.local/java/jdk-*/Contents/Home "$HOME"/.local/java/jdk-*; do
  [ -x "$h/bin/java" ] && export JAVA_HOME="$h" && export PATH="$h/bin:$PATH" && break
done

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  ENGINE=osrm
elif command -v java >/dev/null 2>&1; then
  ENGINE=graphhopper
else
  cat <<'MSG'

[2/3] No routing engine available.

You need ONE of these. Pick whichever you'd rather have on the machine.

  Java  (lighter, recommended, and no admin password needed)
    any mac: curl -L -o /tmp/jdk.tar.gz \
               "https://api.adoptium.net/v3/binary/latest/21/ga/mac/$(uname -m | sed s/arm64/aarch64/)/jdk/hotspot/normal/eclipse" \
             && mkdir -p ~/.local/java && tar xzf /tmp/jdk.tar.gz -C ~/.local/java
    or:      brew install openjdk@21
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
  echo "[3/3] Starting OSRM on http://localhost:8989"
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
  # GraphHopper 10 refuses to start without this one
  import.osm.ignored_highways: footway, cycleway, path, pedestrian, steps
  graph.encoded_values: car_access, car_average_speed, road_access, road_class, road_environment, max_speed, toll, hazmat
  profiles:
    - name: car
      custom_model_files: [car.json]
  # No contraction hierarchies: blocking an area needs a flexible query, and
  # ch.disable only works when the profile was not CH-prepared.
server:
  application_connectors:
    - type: http
      port: 8989
      bind_host: 127.0.0.1
  admin_connectors:
    - type: http
      port: 8990
      bind_host: 127.0.0.1
YML
  fi
  echo "[3/3] Starting GraphHopper on http://localhost:8989"
  echo "      First run builds a routing graph and takes several minutes."
  echo "      Leave this window open. Ctrl-C to stop."
  exec java -Xmx4g -jar "$JAR" server config.yml
fi
