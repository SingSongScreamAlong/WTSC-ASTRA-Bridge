# ASTRA v0.2 command protocol

Each request is one UTF-8 JSON file in `commands/inbox/`. The file name and `id` must match; use a new ID for every request. Blender writes `results/<id>.json` and moves the command to `commands/processed/`. The Git sync process carries these files between the Mac and GitHub.

```json
{
  "id": "shot-001-preview-01",
  "op": "bridge.ping",
  "args": {}
}
```

IDs may use letters, digits, periods, underscores and hyphens, up to 128 characters; start with a letter or digit. Do not reuse an ID for changed content. Existing result IDs are never executed a second time; an interrupted in-flight command requires inspection and recovery rather than automatic replay.

## Create and edit objects

`object.create` supports `cube`, `plane`, `sphere` (`uv_sphere`), `empty`, `triangle`, and `mesh` (`custom_mesh`). Optional common fields: `name`, `location`, `rotation_degrees`, and `scale`, with three numeric values in each vector. A real triangle is one mesh object with one triangular face.

For custom geometry, use local-space vertices, optional edges, and faces of vertex indices:

```json
{
  "id": "village-roof-test-01",
  "op": "object.create",
  "args": {
    "type": "mesh",
    "name": "VillageRoofGable",
    "vertices": [[-1, 0, 0], [1, 0, 0], [0, 0, 2]],
    "faces": [[0, 1, 2]],
    "location": [0, 0, 0]
  }
}
```

Other object operations:

| Operation | Arguments | Effect |
| --- | --- | --- |
| `object.transform` | `name`; optional `location`, `rotation_degrees`, `scale` | Set selected transforms. |
| `object.inspect` | Optional `name`; otherwise the active object | Return transforms, visibility, materials and geometry summary. |
| `object.hide` | `name`; optional `viewport` and `render` booleans | Hide in selected channels; both default to true. |
| `object.show` | Same as `object.hide` | Show in selected channels. |
| `object.delete` | `name` | Remove the named object. |
| `scene.reset` | No arguments | Remove bridge-created objects in the active scene; leave pre-existing and other-scene objects alone. Shared objects are reported and skipped. |

`camera.create` and `camera.set` accept optional `name`, `location`, `rotation_degrees`, `target` (object name or XYZ), `lens`, and `make_active`. `camera.set` uses the active scene camera when `name` is omitted. `collection.inspect` accepts an optional collection `name`.

## Batch several edits

Use one `batch.execute` file for a single user request that changes multiple objects. `args.commands` is an ordered array of `{"op": "...", "args": {...}}` steps. See [triangle_ball_box.json](examples/triangle_ball_box.json) for a complete example.

```json
{
  "id": "shot-001-layout-01",
  "op": "batch.execute",
  "args": {
    "commands": [
      {"op": "object.create", "args": {"type": "triangle", "name": "TRIANGLE"}},
      {"op": "object.create", "args": {"type": "sphere", "name": "BALL", "location": [0, 0, 0.75]}},
      {"op": "object.create", "args": {"type": "cube", "name": "BOX", "location": [3, 0, 1]}}
    ]
  }
}
```

The bridge validates the entire batch before changing the scene, makes one unique local checkpoint, and reports ordered step results. If a step fails during execution, it rolls the batch back. Batches accept scene, object and camera edits; `scene.save`, inspection, rendering and nested batches are separate requests because their outside effects cannot be rolled back with scene changes.

A successful edit is applied to the **open Blender scene**. Its result reports `requires_save: true`: it is not durable in the working `.blend` until you save. After inspecting and accepting a batch, send `scene.save` with a fresh ID; that result reports `scene_saved: true`. If Blender stops before that save, inspect the reopened scene and checkpoint before deciding what to submit again.

## Inspect, save and preview

| Operation | Arguments | Result |
| --- | --- | --- |
| `bridge.ping` | None | Add-on and Blender version plus scene identity. |
| `bridge.status` | None | Add-on and Git sync health, heartbeat age, queue count, last activity and unsaved-scene state. |
| `scene.inspect` | Optional `include_objects` (default true), `offset`, `limit` | Scene, frame, camera, render and paged object details. |
| `scene.save` | None | Save the active `.blend`; it must already have a file path. |
| `render.preview` | Optional `resolution_percentage` (default 50), `frame`, `camera` | Render a PNG to `previews/<id>.png`; restore the scene's previous render settings afterward. |

For *When The Sky Clears*, inspect the shot first, make a layout batch, inspect named objects, request a low-resolution preview of the intended frame, then save the accepted shot. Use a new ID for each adjustment and preview. Production `.blend` files and local checkpoints stay on the Mac; previews are exchanged through GitHub, so keep the repository private before sending unreleased frames.

## Safety and recovery

Only allow-listed operations are executed; commands cannot contain Python. Saved scenes can be checkpointed before mutation, with one checkpoint per command or batch. Checkpoints are local in `runtime/checkpoints/` and are excluded from Git. Results report errors. An interrupted command is not automatically executed again; inspect its result and checkpoint, then submit a fresh ID if another attempt is needed.
