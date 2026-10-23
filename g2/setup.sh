#!/usr/bin/env bash
# Fetch the third-party G2 reference projects into g2/vendor (gitignored).
set -e
cd "$(dirname "$0")"
mkdir -p vendor
clone() { [ -d "vendor/$2" ] || git clone -q "https://github.com/$1/$2.git" "vendor/$2"; echo "$2: $(git -C vendor/$2 rev-parse --short HEAD)"; }
clone gpsnmeajp men-g2-ble-gateway
clone Commute773 g2-kit-unofficial
clone i-soxi even-g2-protocol
clone ithinkthisiscool open-g2-hub
echo "python deps: aiohttp fastmcp>=3.0 Pillow PyYAML bleak"
