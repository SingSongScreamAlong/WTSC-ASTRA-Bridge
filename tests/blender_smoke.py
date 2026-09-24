"""Run with Blender 5.2: Blender -b --factory-startup --python-exit-code 99 --python tests/blender_smoke.py."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import bpy


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "blender_addon"))
bpy.ops.preferences.addon_enable(module="astra_bridge")
import astra_bridge as bridge  # noqa: E402


def queue(root: Path, command: dict) -> dict:
    inbox = root / "commands" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{command['id']}.json").write_text(json.dumps(command), encoding="utf-8")
    bridge.process_inbox()
    result_path = root / "results" / f"{command['id']}.json"
    assert result_path.exists(), f"Missing result for {command['id']}"
    return json.loads(result_path.read_text(encoding="utf-8"))


with tempfile.TemporaryDirectory(prefix="astra_blender_smoke_") as directory:
    root = Path(directory)
    bpy.context.preferences.addons["astra_bridge"].preferences.repo_root = str(root)
    bpy.ops.wm.save_as_mainfile(filepath=str(root / "scene.blend"))
    assert bpy.data.objects.get("Cube") is not None

    batch = {
        "id": "smoke-layout-001",
        "op": "batch.execute",
        "args": {
            "commands": [
                {"op": "object.create", "args": {"type": "triangle", "name": "SMOKE_TRIANGLE", "location": [-3, 0, 1]}},
                {"op": "object.create", "args": {"type": "sphere", "name": "SMOKE_BALL", "location": [0, 0, 0.75]}},
                {"op": "object.create", "args": {"type": "cube", "name": "SMOKE_BOX", "location": [3, 0, 1]}},
            ]
        },
    }
    result = queue(root, batch)
    assert result["status"] == "success", result
    assert result["checkpoint"].startswith("runtime/checkpoints/"), result
    assert result["requires_save"] is True and result["scene_saved"] is False
    tri = bpy.data.objects["SMOKE_TRIANGLE"]
    assert tri.type == "MESH"
    assert len(tri.data.vertices) == 3 and len(tri.data.polygons) == 1
    assert bpy.data.objects.get("SMOKE_BALL") is not None
    assert bpy.data.objects.get("SMOKE_BOX") is not None
    assert len(list((root / "runtime" / "checkpoints").glob("*.blend"))) == 1

    # Re-delivering an already completed ID must not mutate the scene again.
    queue(root, batch)
    assert bpy.data.objects.get("SMOKE_TRIANGLE.001") is None
    assert len(list((root / "runtime" / "checkpoints").glob("*.blend"))) == 1

    # A replacement within one batch may reuse the removed object's name.
    rebuilt = queue(
        root,
        {"id": "smoke-rebuild-001", "op": "batch.execute", "args": {"commands": [
            {"op": "object.delete", "args": {"name": "SMOKE_BOX"}},
            {"op": "object.create", "args": {"type": "cube", "name": "SMOKE_BOX",
                                              "location": [4, 0, 1]}},
        ]}},
    )
    assert rebuilt["status"] == "success", rebuilt
    assert tuple(bpy.data.objects["SMOKE_BOX"].location) == (4.0, 0.0, 1.0)

    invalid = {
        "id": "smoke-invalid-001",
        "op": "batch.execute",
        "args": {
            "commands": [
                {"op": "object.create", "args": {"type": "cube", "name": "MUST_NOT_EXIST"}},
                {"op": "object.transform", "args": {"name": "MISSING_OBJECT", "location": [1, 0, 0]}},
            ]
        },
    }
    failed = queue(root, invalid)
    assert failed["status"] == "error", failed
    assert bpy.data.objects.get("MUST_NOT_EXIST") is None

    # Inject a Blender-side failure after earlier steps changed the scene.
    # The batch must restore both the created object and the deleted default cube.
    original_apply = bridge._apply_step
    step_count = 0

    def fail_third_step(step, undo, deferred):
        global step_count
        step_count += 1
        if step_count == 3:
            raise RuntimeError("injected Blender failure")
        return original_apply(step, undo, deferred)

    bridge._apply_step = fail_third_step
    try:
        rollback = queue(
            root,
            {
                "id": "smoke-rollback-001",
                "op": "batch.execute",
                "args": {"commands": [
                    {"op": "object.create", "args": {"type": "cube", "name": "SHOULD_ROLL_BACK"}},
                    {"op": "object.delete", "args": {"name": "Cube"}},
                    {"op": "object.create", "args": {"type": "cube", "name": "NEVER_CREATED"}},
                ]},
            },
        )
    finally:
        bridge._apply_step = original_apply
    assert rollback["status"] == "error", rollback
    assert bpy.data.objects.get("SHOULD_ROLL_BACK") is None
    assert bpy.data.objects.get("Cube") is not None
    assert bpy.data.objects["Cube"].users_collection

    # An interrupted command is quarantined instead of replayed.
    inflight = root / "runtime" / "inflight"
    inflight.mkdir(parents=True, exist_ok=True)
    (inflight / "smoke-interrupted-001.json").write_text('{"started_at":"2026-01-01T00:00:00Z"}')
    interrupted = queue(
        root,
        {"id": "smoke-interrupted-001", "op": "object.create",
         "args": {"type": "cube", "name": "MUST_NOT_REPLAY"}},
    )
    assert interrupted["recovery_required"] is True, interrupted
    assert bpy.data.objects.get("MUST_NOT_REPLAY") is None

    mismatch_path = root / "commands" / "inbox" / "mismatch.json"
    mismatch_path.write_text(json.dumps({"id": "another-id", "op": "object.create",
                                         "args": {"type": "cube", "name": "MUST_NOT_RUN"}}))
    bridge.process_inbox()
    assert json.loads((root / "results" / "mismatch.json").read_text())["status"] == "error"
    assert bpy.data.objects.get("MUST_NOT_RUN") is None

    mesh_result = queue(
        root,
        {
            "id": "smoke-mesh-001",
            "op": "object.create",
            "args": {
                "type": "mesh",
                "name": "SMOKE_MESH",
                "vertices": [[0, 0, 0], [1, 0, 0], [0, 0, 1]],
                "faces": [[0, 1, 2]],
            },
        },
    )
    assert mesh_result["status"] == "success", mesh_result
    assert len(bpy.data.objects["SMOKE_MESH"].data.polygons) == 1

    bridge.execute({"id": "hide", "op": "object.hide", "args": {"name": "SMOKE_MESH"}}, root)
    assert bpy.data.objects["SMOKE_MESH"].hide_render
    bridge.execute({"id": "show", "op": "object.show", "args": {"name": "SMOKE_MESH"}}, root)
    assert not bpy.data.objects["SMOKE_MESH"].hide_render

    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = 64
    scene.render.resolution_y = 64
    scene.render.image_settings.file_format = "JPEG"
    old_frame = scene.frame_current
    preview = bridge.execute(
        {"id": "smoke-preview-001", "op": "render.preview", "args": {"resolution_percentage": 20, "frame": 2}},
        root,
    )
    assert (root / preview["preview"]).exists(), preview
    assert scene.render.image_settings.file_format == "JPEG"
    assert scene.frame_current == old_frame

    status = bridge.execute({"id": "status", "op": "bridge.status", "args": {}}, root)
    assert status["bridge_version"] == "0.2.0", status
    assert status["sync"]["health"] == "missing", status
    (root / "runtime" / "bridge_status.json").write_text(json.dumps({
        "state": "ready", "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "last_success_at": datetime.now(timezone.utc).isoformat(),
    }))
    assert bridge.sync_status(root)["health"] == "ready"
    inspection = bridge.execute({"id": "inspect", "op": "scene.inspect", "args": {"limit": 2}}, root)
    assert inspection["object_count"] >= 6, inspection

    # The manual button must work even when automatic polling is paused.
    prefs = bpy.context.preferences.addons["astra_bridge"].preferences
    prefs.auto_process = False
    manual = {"id": "smoke-manual-001", "op": "bridge.ping", "args": {}}
    (root / "commands" / "inbox" / "smoke-manual-001.json").write_text(json.dumps(manual))
    bpy.ops.astra.process_now()
    assert (root / "results" / "smoke-manual-001.json").exists()
    prefs.auto_process = True

    saved = queue(root, {"id": "smoke-save-001", "op": "scene.save", "args": {}})
    assert saved["status"] == "success" and saved["scene_saved"] is True
    assert saved["requires_save"] is False

    other_scene = bpy.data.scenes.new("ASTRA_OTHER_SCENE")
    other_mesh = bpy.data.meshes.new("OTHER_SCENE_MESH")
    other_obj = bpy.data.objects.new("OTHER_SCENE_ASTRO", other_mesh)
    other_scene.collection.objects.link(other_obj)
    other_obj[bridge.OWNER_PROP] = True
    bridge.execute({"id": "reset", "op": "scene.reset", "args": {}}, root)
    assert bpy.data.objects.get("SMOKE_TRIANGLE") is None
    assert bpy.data.objects.get("SMOKE_MESH") is None
    assert bpy.data.objects.get("Cube") is not None
    assert bpy.data.objects.get("OTHER_SCENE_ASTRO") is not None
    print("ASTRA_BLENDER_SMOKE_OK", bpy.app.version_string)
