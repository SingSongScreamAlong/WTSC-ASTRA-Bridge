# macOS setup

## Before you start

The GitHub repository should be **private** before any private story assets,
previews, or production information are sent through it.

## 1. Clone the repository

Using Terminal:

```bash
cd ~/Documents
git clone https://github.com/SingSongScreamAlong/WTSC-ASTRA-Bridge.git
cd WTSC-ASTRA-Bridge
```

If Git asks you to authenticate, use your normal GitHub credential flow.

## 2. Link the add-on into Blender

Launch Blender at least once, then close it. From the repo:

```bash
python3 tools/setup_mac.py
```

The helper finds the newest Blender user configuration folder and creates a
symlink to `blender_addon/astra_bridge`. This means future Git updates to the
add-on land directly in the installed source.

If you have several Blender versions, specify one:

```bash
python3 tools/setup_mac.py --blender-version 4.5
```

## 3. Enable ASTRA Bridge

Open Blender:

1. Open **Preferences > Add-ons**.
2. Find **ASTRA Bridge** and enable it.
3. Open the add-on's preferences.
4. Set **Bridge Repo** to the local cloned repo folder.
5. Leave **Auto Process Commands** and **Checkpoint Before Changes** enabled.

The 3D Viewport sidebar will now have an **ASTRA** tab.

## 4. Start the local sync daemon

From Finder you can double-click `run_bridge.command` after macOS allows it.

If macOS blocks the file because it is not executable, run once:

```bash
chmod +x run_bridge.command
./run_bridge.command
```

Or simply:

```bash
python3 bridge/astra_bridge.py
```

Keep the terminal window open while using the bridge.

## 5. First round-trip test

A `bridge.ping` command is already queued in `commands/inbox/`.

When all three pieces are active:

- the daemon pulls the command,
- Blender processes it,
- Blender writes `results/setup-ping-001.json`,
- the daemon pushes the result back to GitHub.

Then return to the ChatGPT conversation and say **"bridge is running"**.

## What is never auto-committed

The sync daemon stages only:

- `commands/`
- `results/`
- `previews/`

It does not auto-commit source code, local configuration, credentials, or
Blender project files. `.blend` files are explicitly ignored.
