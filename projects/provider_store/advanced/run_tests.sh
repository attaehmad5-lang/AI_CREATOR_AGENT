#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
python -W error -m unittest discover -s tests -v
python -W error -m unittest discover -s ../.. -p "test_advps_core.py" -v
python -W error -m compileall -q advps
