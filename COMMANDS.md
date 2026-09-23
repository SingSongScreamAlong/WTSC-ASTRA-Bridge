# ASTRA v0.1 Command Protocol

Each command is one JSON file in `commands/inbox/`.

Required fields:

```json
{
  "id": "unique-command-id",
  "op": "bridge.ping",
  "args": {}
}
```

The Blender add-on writes `results/<id>.json` and moves the original command
to `commands/processed/`.

## Supported operations

### bridge.ping
No args.

### scene.inspect
No args.

### scene.save
No args. The current .blend must already have been saved once.

### object.create
```json
{
  "id": "demo-create-cube",
  "op": "object.create",
  "args": {
    "type": "cube",
    "name": "ASTRA_TEST",
    "location": [0, 0, 0],
    "rotation_degrees": [0, 0, 0],
    "scale": [1, 1, 1]
  }
}
```

Allowed types in v0.1: `cube`, `plane`, `sphere`, `empty`.

### object.transform
Requires `name`; optional `location`, `rotation_degrees`, `scale`.

### object.inspect
Uses `args.name`, or the active Blender object if omitted.

### camera.create
Optional: `name`, `location`, `rotation_degrees`, `target`, `lens`,
`make_active`.

`target` may be an object name or an XYZ coordinate.

### camera.set
Uses `args.name`, or the active scene camera if omitted. Supports the same
transform/lens/target properties as `camera.create`.

### collection.inspect
Optional `args.name`. Omit it to summarize all collections.

### render.preview
Optional `args.resolution_percentage` (default 50). Writes a PNG into
`previews/`.

## Safety

The add-on does **not** expose arbitrary Python execution. Only allow-listed
operations can run. When enabled, a local checkpoint copy is made before
mutating commands if the current .blend file already has a saved path.
