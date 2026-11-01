#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

if [ ! -d config/defaults ]; then
    echo "missing config/defaults" >&2
    exit 1
fi

cp config/defaults/*.yaml config/
echo "config reset to defaults"
