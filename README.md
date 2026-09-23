# WTSC ASTRA Bridge

A lightweight command bridge between ChatGPT/Astra and Blender for **When The Sky Clears**.

> Security note: never commit tokens, credentials, private local paths, `.blend` production files, or unreleased story assets to this repository.

## v0.1 goal

Prove a reliable round-trip control loop:

1. ChatGPT writes a JSON command into `commands/inbox/`.
2. A small local sync daemon pulls the repo to the Mac.
3. The Blender add-on polls the local inbox with `bpy.app.timers`.
4. Blender executes only allow-listed operations.
5. Blender writes a JSON result into `results/`.
6. The local daemon commits/pushes the result.
7. ChatGPT reads the result.

Initial commands: `bridge.ping`, `scene.inspect`, `scene.save`, `object.create`, `object.transform`, `object.inspect`, `camera.create`, `camera.set`, `collection.inspect`, and `render.preview`.
