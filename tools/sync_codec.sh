#!/bin/sh
# Copy the generated OEP v0 Python codec from the sibling oep-spec checkout.
set -e
here=$(cd "$(dirname "$0")/.." && pwd)
spec=${OEP_SPEC_DIR:-$here/../oep-spec}
cp "$spec/generated/oep-v0-py/oep_v0.py" "$here/src/oep_client/v0/codec.py"
printf 'oep-spec %s\n' "$(git -C "$spec" rev-parse --short HEAD)" > "$here/src/oep_client/v0/CODEC_SOURCE.txt"
echo "synced codec from oep-spec $(git -C "$spec" rev-parse --short HEAD)"
