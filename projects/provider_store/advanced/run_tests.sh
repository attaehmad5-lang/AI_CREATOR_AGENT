#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/../../.."
python -W error -m unittest discover -s tests -p "test_advps_core.py" -v
python -W error -m unittest discover -s tests -p "test_advanced_store_bridge.py" -v
python -W error -m compileall -q projects/provider_store/advanced/advps
