# Upgrade a working v0.1 bridge to v0.2

The current Mac installation uses `~/Documents/WTSC-ASTRA-Bridge` and a Blender 5.2 add-on symlink into that clone. The old cleanup commands have already completed successfully; there is no pending inbox batch to replay.

1. Save the current Blender scene. Close Blender and stop the bridge Terminal process with **Control-C**.
2. Open Terminal in `~/Documents/WTSC-ASTRA-Bridge` and update the clone:

   ```bash
   cd ~/Documents/WTSC-ASTRA-Bridge
   git pull --ff-only
   ```

3. Run `python3 tools/setup_mac.py --blender-version 5.2` only if the installer reports the add-on link is missing or points elsewhere. A working symlink updates automatically when the clone changes.
4. Reopen Blender 5.2.2, confirm the ASTRA add-on is enabled and **Bridge Repo** points to this clone, then run `./run_bridge.command` in Terminal.
5. Send a new `bridge.ping` command. Confirm its result reports `bridge_version` **0.2.0**, then use `scene.inspect` or `bridge.status` to inspect the scene and queue.

The upgrade does not rewrite existing scenes or replay anything in `commands/processed/`. Local checkpoints remain in `runtime/checkpoints/`. A failed or interrupted command is reported for recovery instead of being silently applied a second time.

The original test scene still has three box pieces named `TRIANGLE_LEFT`, `TRIANGLE_RIGHT`, and `TRIANGLE_BASE`. After the v0.2 ping succeeds, [replace_v01_triangle.json](examples/replace_v01_triangle.json) shows a single batch that deletes those pieces and creates one real triangle. It is an example only; it is not queued or run during installation.

After any layout batch, inspect or preview the open scene, then send a separate `scene.save` command for changes you want to keep in the `.blend` file.

**If `git pull` says that `run_bridge.command` has local changes:** the v0.1 setup required making that launcher executable on this Mac. Confirm this is the only local change with `git status --short`, then run `git restore -- run_bridge.command` and repeat `git pull --ff-only`. v0.2 records the executable bit in Git, so this fix is no longer needed.

The repository is public at present. Keep story assets and production previews outside the command bus until you make it private.
