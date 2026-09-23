bl_info = {
    "name": "ASTRA Bridge",
    "author": "WTSC",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > ASTRA",
    "description": "Safe JSON command bridge for When The Sky Clears",
    "category": "System",
}

from __future__ import annotations

import json
import math
import shutil
import traceback
from datetime import datetime, timezone
from pathlib import Path

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel

TIMER_SECONDS = 1.5
MUTATING_OPS = {
    "scene.save",
    "object.create",
    "object.transform",
    "camera.create",
    "camera.set",
}

_TIMER_REGISTERED = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def addon_prefs():
    addon = bpy.context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def repo_root() -> Path | None:
    prefs = addon_prefs()
    if not prefs or not prefs.repo_root:
        return None
    root = Path(bpy.path.abspath(prefs.repo_root)).expanduser()
    return root.resolve()


def exchange_paths(root: Path) -> dict[str, Path]:
    paths = {
        "inbox": root / "commands" / "inbox",
        "processed": root / "commands" / "processed",
        "results": root / "results",
        "previews": root / "previews",
        "checkpoints": root / "runtime" / "checkpoints",
    }
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    return paths


def vec3(value, default=None):
    if value is None:
        return default
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("Expected a 3-item vector")
    return tuple(float(x) for x in value)


def rotation_radians(args: dict, key="rotation_degrees"):
    value = args.get(key)
    if value is None:
        return None
    degrees = vec3(value)
    return tuple(math.radians(v) for v in degrees)


def object_payload(obj: bpy.types.Object) -> dict:
    return {
        "name": obj.name,
        "type": obj.type,
        "location": [round(v, 6) for v in obj.location],
        "rotation_degrees": [round(math.degrees(v), 6) for v in obj.rotation_euler],
        "scale": [round(v, 6) for v in obj.scale],
        "visible_viewport": not obj.hide_viewport,
        "visible_render": not obj.hide_render,
        "modifiers": [m.name for m in obj.modifiers],
        "materials": [slot.material.name if slot.material else None for slot in obj.material_slots],
    }


def aim_camera(camera_obj: bpy.types.Object, target) -> None:
    if isinstance(target, str):
        target_obj = bpy.data.objects.get(target)
        if target_obj is None:
            raise ValueError(f"Target object not found: {target}")
        target_vec = target_obj.matrix_world.translation
    else:
        target_vec = vec3(target)
        target_vec = bpy.mathutils.Vector(target_vec) if hasattr(bpy, "mathutils") else None
        if target_vec is None:
            from mathutils import Vector
            target_vec = Vector(vec3(target))

    direction = target_vec - camera_obj.location
    if direction.length == 0:
        raise ValueError("Camera target cannot equal camera location")
    camera_obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def checkpoint_if_needed(root: Path, command: dict) -> str | None:
    prefs = addon_prefs()
    if not prefs or not prefs.checkpoint_before_mutation:
        return None
    if command.get("op") not in MUTATING_OPS:
        return None
    if not bpy.data.filepath:
        return None

    paths = exchange_paths(root)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = Path(bpy.data.filepath).stem
    target = paths["checkpoints"] / f"{stem}_{stamp}.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(target), copy=True)
    return str(target)


def execute(command: dict, root: Path) -> dict:
    op = command.get("op")
    args = command.get("args") or {}

    if op == "bridge.ping":
        return {
            "bridge_version": "0.1.0",
            "blender_version": bpy.app.version_string,
            "scene": bpy.context.scene.name if bpy.context.scene else None,
            "blend_file": Path(bpy.data.filepath).name if bpy.data.filepath else None,
        }

    if op == "scene.inspect":
        scene = bpy.context.scene
        return {
            "scene": scene.name,
            "frame_current": scene.frame_current,
            "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
            "render_engine": scene.render.engine,
            "resolution": [scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage],
            "camera": scene.camera.name if scene.camera else None,
            "object_count": len(scene.objects),
            "collections": [c.name for c in bpy.data.collections],
            "selected_objects": [o.name for o in bpy.context.selected_objects],
            "blend_file": Path(bpy.data.filepath).name if bpy.data.filepath else None,
        }

    if op == "scene.save":
        if not bpy.data.filepath:
            raise RuntimeError("Current Blender file has never been saved. Save it once manually first.")
        bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath)
        return {"saved": True, "blend_file": Path(bpy.data.filepath).name}

    if op == "object.create":
        kind = str(args.get("type", "cube")).lower()
        location = vec3(args.get("location"), (0.0, 0.0, 0.0))
        if kind == "cube":
            bpy.ops.mesh.primitive_cube_add(location=location)
        elif kind == "plane":
            bpy.ops.mesh.primitive_plane_add(location=location)
        elif kind in {"uv_sphere", "sphere"}:
            bpy.ops.mesh.primitive_uv_sphere_add(location=location)
        elif kind == "empty":
            bpy.ops.object.empty_add(type="PLAIN_AXES", location=location)
        else:
            raise ValueError(f"Unsupported object type: {kind}")

        obj = bpy.context.active_object
        if not obj:
            raise RuntimeError("Blender did not create an active object")
        if args.get("name"):
            obj.name = str(args["name"])

        rot = rotation_radians(args)
        if rot is not None:
            obj.rotation_euler = rot
        scale = vec3(args.get("scale"))
        if scale is not None:
            obj.scale = scale
        return object_payload(obj)

    if op == "object.transform":
        name = args.get("name")
        obj = bpy.data.objects.get(str(name)) if name else None
        if obj is None:
            raise ValueError(f"Object not found: {name}")

        location = vec3(args.get("location"))
        if location is not None:
            obj.location = location
        rot = rotation_radians(args)
        if rot is not None:
            obj.rotation_euler = rot
        scale = vec3(args.get("scale"))
        if scale is not None:
            obj.scale = scale
        return object_payload(obj)

    if op == "object.inspect":
        name = args.get("name")
        obj = bpy.data.objects.get(str(name)) if name else bpy.context.active_object
        if obj is None:
            raise ValueError(f"Object not found: {name}")
        return object_payload(obj)

    if op == "camera.create":
        name = str(args.get("name", "ASTRA_CAMERA"))
        location = vec3(args.get("location"), (0.0, -10.0, 2.0))
        bpy.ops.object.camera_add(location=location)
        camera = bpy.context.active_object
        camera.name = name
        camera.data.name = f"{name}_DATA"
        camera.data.lens = float(args.get("lens", 50.0))
        rot = rotation_radians(args)
        if rot is not None:
            camera.rotation_euler = rot
        if args.get("target") is not None:
            aim_camera(camera, args["target"])
        if bool(args.get("make_active", True)):
            bpy.context.scene.camera = camera
        return object_payload(camera) | {"lens": camera.data.lens}

    if op == "camera.set":
        name = args.get("name") or (bpy.context.scene.camera.name if bpy.context.scene.camera else None)
        camera = bpy.data.objects.get(str(name)) if name else None
        if camera is None or camera.type != "CAMERA":
            raise ValueError(f"Camera not found: {name}")
        location = vec3(args.get("location"))
        if location is not None:
            camera.location = location
        rot = rotation_radians(args)
        if rot is not None:
            camera.rotation_euler = rot
        if "lens" in args:
            camera.data.lens = float(args["lens"])
        if args.get("target") is not None:
            aim_camera(camera, args["target"])
        if bool(args.get("make_active", True)):
            bpy.context.scene.camera = camera
        return object_payload(camera) | {"lens": camera.data.lens}

    if op == "collection.inspect":
        name = args.get("name")
        if name:
            collection = bpy.data.collections.get(str(name))
            if collection is None:
                raise ValueError(f"Collection not found: {name}")
            return {
                "name": collection.name,
                "objects": [o.name for o in collection.objects],
                "children": [c.name for c in collection.children],
            }
        return {
            "collections": [
                {
                    "name": c.name,
                    "objects": len(c.objects),
                    "children": [child.name for child in c.children],
                }
                for c in bpy.data.collections
            ]
        }

    if op == "render.preview":
        scene = bpy.context.scene
        if scene.camera is None:
            raise RuntimeError("Scene has no active camera")
        paths = exchange_paths(root)
        command_id = str(command["id"])
        preview = paths["previews"] / f"{command_id}.png"

        old_path = scene.render.filepath
        old_percentage = scene.render.resolution_percentage
        try:
            scene.render.filepath = str(preview)
            scene.render.resolution_percentage = int(args.get("resolution_percentage", 50))
            scene.render.image_settings.file_format = "PNG"
            bpy.ops.render.render(write_still=True)
        finally:
            scene.render.filepath = old_path
            scene.render.resolution_percentage = old_percentage

        return {
            "preview": f"previews/{preview.name}",
            "camera": scene.camera.name,
            "frame": scene.frame_current,
        }

    raise ValueError(f"Unsupported operation: {op}")


def process_one(path: Path, root: Path) -> None:
    paths = exchange_paths(root)
    try:
        command = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(command, dict):
            raise ValueError("Command must be a JSON object")
        command_id = str(command.get("id") or path.stem)
        command["id"] = command_id
        op = command.get("op")
        if not isinstance(op, str) or not op:
            raise ValueError("Command is missing 'op'")

        result_path = paths["results"] / f"{command_id}.json"
        if result_path.exists():
            # Idempotency: never execute the same command twice.
            result = json.loads(result_path.read_text(encoding="utf-8"))
        else:
            checkpoint = checkpoint_if_needed(root, command)
            payload = execute(command, root)
            result = {
                "id": command_id,
                "op": op,
                "status": "success",
                "completed_at": utc_now(),
                "checkpoint_created": bool(checkpoint),
                "result": payload,
            }
            result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    except Exception as exc:
        command_id = path.stem
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            command_id = str(parsed.get("id") or command_id) if isinstance(parsed, dict) else command_id
            op = parsed.get("op") if isinstance(parsed, dict) else None
        except Exception:
            op = None
        result = {
            "id": command_id,
            "op": op,
            "status": "error",
            "completed_at": utc_now(),
            "error": str(exc),
            "traceback": traceback.format_exc(limit=8),
        }
        (paths["results"] / f"{command_id}.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )

    destination = paths["processed"] / path.name
    if destination.exists():
        destination.unlink()
    shutil.move(str(path), str(destination))


def process_inbox() -> int:
    root = repo_root()
    prefs = addon_prefs()
    if root is None or prefs is None or not prefs.auto_process:
        return 0
    paths = exchange_paths(root)
    count = 0
    for path in sorted(paths["inbox"].glob("*.json")):
        process_one(path, root)
        count += 1
    return count


def timer_callback():
    try:
        process_inbox()
    except Exception:
        traceback.print_exc()
    return TIMER_SECONDS


class ASTRA_Preferences(AddonPreferences):
    bl_idname = __package__

    repo_root: StringProperty(
        name="Bridge Repo",
        description="Local path to the cloned WTSC-ASTRA-Bridge repository",
        subtype="DIR_PATH",
        default="",
    )
    auto_process: BoolProperty(
        name="Auto Process Commands",
        description="Poll the local inbox and execute allow-listed ASTRA commands",
        default=True,
    )
    checkpoint_before_mutation: BoolProperty(
        name="Checkpoint Before Changes",
        description="Save a local checkpoint copy before mutating commands when the .blend has been saved",
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "repo_root")
        layout.prop(self, "auto_process")
        layout.prop(self, "checkpoint_before_mutation")
        layout.label(text="Credentials stay outside Blender.")


class ASTRA_OT_ProcessNow(Operator):
    bl_idname = "astra.process_now"
    bl_label = "Process ASTRA Inbox"

    def execute(self, context):
        count = process_inbox()
        self.report({"INFO"}, f"Processed {count} command(s)")
        return {"FINISHED"}


class ASTRA_OT_OpenPreferences(Operator):
    bl_idname = "astra.open_preferences"
    bl_label = "ASTRA Preferences"

    def execute(self, context):
        bpy.ops.screen.userpref_show("INVOKE_DEFAULT")
        return {"FINISHED"}


class ASTRA_PT_Panel(Panel):
    bl_label = "ASTRA Bridge"
    bl_idname = "ASTRA_PT_bridge"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ASTRA"

    def draw(self, context):
        layout = self.layout
        prefs = addon_prefs()
        root = repo_root()
        configured = bool(root and root.exists())

        row = layout.row()
        row.label(text="Status:")
        row.label(text="READY" if configured else "SET REPO PATH", icon="CHECKMARK" if configured else "ERROR")

        if prefs:
            layout.prop(prefs, "auto_process", text="Bridge Active")
            layout.prop(prefs, "checkpoint_before_mutation", text="Auto Checkpoint")

        layout.operator("astra.process_now", icon="FILE_REFRESH")
        layout.operator("astra.open_preferences", icon="PREFERENCES")

        if configured:
            paths = exchange_paths(root)
            pending = len(list(paths["inbox"].glob("*.json")))
            layout.label(text=f"Pending commands: {pending}")
        else:
            layout.label(text="Set the cloned repo folder in Add-on Preferences.")


CLASSES = (
    ASTRA_Preferences,
    ASTRA_OT_ProcessNow,
    ASTRA_OT_OpenPreferences,
    ASTRA_PT_Panel,
)


def register():
    global _TIMER_REGISTERED
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    if not bpy.app.timers.is_registered(timer_callback):
        bpy.app.timers.register(timer_callback, first_interval=TIMER_SECONDS, persistent=True)
        _TIMER_REGISTERED = True


def unregister():
    global _TIMER_REGISTERED
    if bpy.app.timers.is_registered(timer_callback):
        bpy.app.timers.unregister(timer_callback)
    _TIMER_REGISTERED = False
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
