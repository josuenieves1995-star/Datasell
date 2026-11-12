#!/usr/bin/env bash
# Copy the Datasell Python package into the Android app before building.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p android/app/src/main/python
rm -rf android/app/src/main/python/datasell
cp -r datasell android/app/src/main/python/datasell
find android/app/src/main/python -name "__pycache__" -prune -exec rm -rf {} +
