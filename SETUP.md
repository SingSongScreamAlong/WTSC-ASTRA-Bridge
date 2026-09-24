# macOS setup for Blender 5.2.2

## Install once

Clone the repository into `~/Documents`, open Blender 5.2.2 once, then link the add-on:

```bash
cd ~/Documents
git clone https://github.com/SingSongScreamAlong/WTSC-ASTRA-Bridge.git
cd WTSC-ASTRA-Bridge
python3 tools/setup_mac.py --blender-version 5.2
```

The helper creates a symlink from Blender's `5.2/scripts/addons/astra_bridge` folder to the clone. A later `git pull` updates that same source; there is no Python code to paste into Blender.

In Blender, open **Preferences → Add-ons**, enable **ASTRA Bridge**, and set **Bridge Repo** to your cloned `WTSC-ASTRA-Bridge` folder. Keep **Auto Process Commands** and **Checkpoint Before Changes** enabled. Save Preferences.

Save your Blender scene once before sending changes. This gives the bridge a `.blend` path for local checkpoint copies.

Start the sync process from the clone:

```bash
./run_bridge.command
```

Leave that Terminal window open while using the bridge. The ASTRA tab in Blender's 3D Viewport shows the local queue and health status. Send a fresh `bridge.ping` command with a new ID to verify the round trip; the original setup ping was already processed.

## Updating an existing installation

Follow [UPGRADE.md](UPGRADE.md). The installed symlink should already point to the clone, so you do not need to reinstall the add-on for each code update.

## Security and local files

The Git sync process stages only `commands/`, `results/`, and `previews/`. It does not auto-commit code, credentials, checkpoints, downloaded models, or `.blend` files. The repository is currently public, so make it private before sending unreleased story content through the command bus.
