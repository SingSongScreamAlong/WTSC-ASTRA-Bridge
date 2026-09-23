#!/bin/bash
set -u
cd "$(dirname "$0")"
echo "Starting WTSC ASTRA Bridge..."
echo "Leave this window open while using Blender."
echo
exec /usr/bin/env python3 bridge/astra_bridge.py --repo "$(pwd)"
