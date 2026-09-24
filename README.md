# WTSC ASTRA Bridge

ASTRA Bridge carries structured commands between ChatGPT and Blender for **When The Sky Clears**. A local Git sync process pulls commands from GitHub; the Blender 5.2 add-on applies allow-listed scene operations and writes results and previews back to the repository. Blender never needs GitHub credentials or a network connection of its own.

The v0.2 upgrade adds real triangle and custom mesh objects, object cleanup, multi-step batches, one checkpoint per batch, queue recovery, health reporting, and frame-aware scene inspection and previews. See [COMMANDS.md](COMMANDS.md) for the command format and [UPGRADE.md](UPGRADE.md) to update an existing Mac installation.

## The command loop

1. Place a uniquely named JSON file in `commands/inbox/` and commit it to GitHub.
2. Run `run_bridge.command` in the local clone. It pulls commands and pushes results.
3. The Blender add-on polls the inbox on Blender's main thread.
4. Read `results/<id>.json`; rendered previews appear in `previews/`.

The add-on executes only known operations. Command files are data, never arbitrary Python. The sync process stages only the exchange folders; it does not publish source edits, `.blend` files, credentials, or local checkpoints. Keep production `.blend` files and downloaded model packs outside this repository.

## Start here

- New installation: [SETUP.md](SETUP.md)
- Existing v0.1 installation: [UPGRADE.md](UPGRADE.md)
- Command examples and safety rules: [COMMANDS.md](COMMANDS.md)

**Repository visibility:** This repository is currently public. Do not put private story material, sensitive scene metadata, or unreleased previews into commands/results until its visibility is changed to private.
