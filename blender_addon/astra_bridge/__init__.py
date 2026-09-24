from __future__ import annotations

bl_info = {
    "name": "ASTRA Bridge",
    "author": "WTSC",
    "version": (0, 2, 0),
    "blender": (5, 2, 0),
    "location": "View3D > Sidebar > ASTRA",
    "description": "Safe JSON command bridge for When The Sky Clears",
    "category": "System",
}

import hashlib
import json
import math
import os
import re
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel

TIMER_SECONDS = 1.5
HEARTBEAT_SECONDS = 10.0
MAX_COMMANDS_PER_POLL = 10
MAX_MESH_VERTICES = 10000
MAX_MESH_EDGES = 20000
MAX_MESH_FACES = 20000
MAX_BATCH_STEPS = 100
OWNER_PROP = "astra_bridge_owned"
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MUTATING_OPS = {
    "scene.save",
    "scene.reset",
    "object.create",
    "object.transform",
    "object.hide",
    "object.show",
    "object.delete",
    "camera.create",
    "camera.set",
    "batch.execute",
}
BATCH_OPS = MUTATING_OPS - {"scene.save", "batch.execute"}

_TIMER_REGISTERED = False
_STARTED_AT = datetime.now(timezone.utc).isoformat()
_STATUS = {
    "state": "starting",
    "started_at": _STARTED_AT,
    "last_command_id": None,
    "last_command_status": None,
    "last_command_at": None,
    "last_error": None,
    "last_mutation_requires_save": None,
}
_LAST_HEARTBEAT = 0.0


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
        "processing": root / "commands" / "processing",
        "processed": root / "commands" / "processed",
        "results": root / "results",
        "previews": root / "previews",
        "checkpoints": root / "runtime" / "checkpoints",
        "inflight": root / "runtime" / "inflight",
        "runtime": root / "runtime",
    }
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    return paths


def finite_float(value, label="number") -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def vec3(value, default=None):
    if value is None:
        return default
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("Expected a 3-item vector")
    return tuple(finite_float(x, "vector component") for x in value)


def rotation_radians(args: dict, key="rotation_degrees"):
    value = args.get(key)
    if value is None:
        return None
    degrees = vec3(value)
    return tuple(math.radians(v) for v in degrees)


def positive_float(value, label: str) -> float:
    result = finite_float(value, label)
    if result <= 0:
        raise ValueError(f"{label} must be positive")
    return result


def bounded_int(value, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{label} must be an integer from {low} to {high}")
    return value


def safe_id(value: object) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value) or value in {".", ".."}:
        raise ValueError("Command id must use 1–128 letters, numbers, periods, dashes, or underscores")
    return value


def public_path(value: str) -> str:
    """Keep Blender-relative paths, but do not publish the user's local directories."""
    if value.startswith("//"):
        return value
    return Path(value).name if Path(value).is_absolute() else value


def public_error(value: object) -> str:
    message = str(value)
    # Results are synchronized to GitHub, while detailed tracebacks remain in
    # Blender's local console. Suppress absolute paths from OS/Blender errors.
    return re.sub(r"(?<![A-Za-z0-9.])/(?:[^\s\"'<>]+)", "<local-path>", message)


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_json(path: Path, value: dict) -> None:
    """Publish a result without ever replacing a result for an earlier command."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def command_hash(command: dict) -> str:
    canonical = json.dumps(command, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def archive_command(path: Path, processed: Path) -> Path:
    destination = processed / path.name
    if destination.exists():
        destination = processed / f"{path.stem}.duplicate-{uuid.uuid4().hex[:8]}.json"
    path.rename(destination)
    return destination


def object_payload(obj: bpy.types.Object) -> dict:
    try:
        visible_viewport = obj.visible_get(view_layer=bpy.context.view_layer)
    except (RuntimeError, ReferenceError):
        visible_viewport = not obj.hide_viewport
    if obj.rotation_mode == "QUATERNION":
        display_rotation = obj.rotation_quaternion.to_euler()
    elif obj.rotation_mode == "AXIS_ANGLE":
        from mathutils import Quaternion
        axis_angle = obj.rotation_axis_angle
        display_rotation = Quaternion(axis_angle[1:4], axis_angle[0]).to_euler()
    else:
        display_rotation = obj.rotation_euler
    payload = {
        "name": obj.name,
        "type": obj.type,
        "location": [round(v, 6) for v in obj.location],
        "rotation_degrees": [round(math.degrees(v), 6) for v in display_rotation],
        "rotation_mode": obj.rotation_mode,
        "scale": [round(v, 6) for v in obj.scale],
        "visible_viewport": bool(visible_viewport),
        "visible_render": not obj.hide_render,
        "bridge_owned": bool(obj.get(OWNER_PROP, False)),
        "collections": [collection.name for collection in obj.users_collection],
        "modifiers": [m.name for m in obj.modifiers],
        "materials": [slot.material.name if slot.material else None for slot in obj.material_slots],
    }
    if obj.type == "MESH":
        payload["mesh"] = {
            "vertices": len(obj.data.vertices),
            "edges": len(obj.data.edges),
            "faces": len(obj.data.polygons),
        }
    if obj.type == "CAMERA":
        payload["lens"] = round(obj.data.lens, 6)
    return payload


def aim_camera(camera_obj: bpy.types.Object, target) -> None:
    if isinstance(target, str):
        target_obj = bpy.data.objects.get(target)
        if target_obj is None:
            raise ValueError(f"Target object not found: {target}")
        target_vec = target_obj.matrix_world.translation
    else:
        from mathutils import Vector
        target_vec = Vector(vec3(target))

    direction = target_vec - camera_obj.location
    if direction.length == 0:
        raise ValueError("Camera target cannot equal camera location")
    camera_obj.rotation_mode = "XYZ"
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
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    stem = Path(bpy.data.filepath).stem
    command_id = safe_id(command["id"])
    target = paths["checkpoints"] / f"{stem}_{stamp}_{command_id}_{uuid.uuid4().hex[:8]}.blend"
    outcome = bpy.ops.wm.save_as_mainfile(filepath=str(target), copy=True)
    if "FINISHED" not in outcome or not target.exists():
        raise RuntimeError("Blender did not finish the checkpoint copy")
    return f"runtime/checkpoints/{target.name}"


def _mesh_geometry(kind: str, args: dict) -> tuple[list, list, list]:
    if kind == "cube":
        vertices = [
            (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
            (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
        ]
        faces = [
            (3, 2, 1, 0), (4, 5, 6, 7), (0, 1, 5, 4),
            (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7),
        ]
        edges = []
    elif kind == "plane":
        vertices = [(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)]
        faces = [(0, 1, 2, 3)]
        edges = []
    elif kind == "triangle":
        # An upright, filled XZ triangle with its front normal toward -Y.
        vertices = [(-1, 0, -1), (1, 0, -1), (0, 0, 1)]
        faces = [(0, 1, 2)]
        edges = []
    elif kind == "sphere":
        segments, rings = 32, 16
        vertices = [(0.0, 0.0, 1.0)]
        for ring in range(1, rings):
            polar = math.pi * ring / rings
            for segment in range(segments):
                azimuth = 2 * math.pi * segment / segments
                vertices.append((math.sin(polar) * math.cos(azimuth),
                                 math.sin(polar) * math.sin(azimuth),
                                 math.cos(polar)))
        bottom = len(vertices)
        vertices.append((0.0, 0.0, -1.0))
        faces = []
        for segment in range(segments):
            following = (segment + 1) % segments
            faces.append((0, 1 + segment, 1 + following))
        for ring in range(rings - 2):
            first = 1 + ring * segments
            second = first + segments
            for segment in range(segments):
                following = (segment + 1) % segments
                faces.append((first + segment, second + segment,
                              second + following, first + following))
        first = 1 + (rings - 2) * segments
        for segment in range(segments):
            following = (segment + 1) % segments
            faces.append((bottom, first + following, first + segment))
        edges = []
    elif kind == "mesh":
        vertices = args.get("vertices")
        edges = args.get("edges", [])
        faces = args.get("faces", [])
    else:
        raise ValueError(f"Unsupported mesh type: {kind}")
    return validate_mesh_geometry(vertices, edges, faces)


def validate_mesh_geometry(vertices, edges, faces) -> tuple[list, list, list]:
    if not isinstance(vertices, (list, tuple)) or not 1 <= len(vertices) <= MAX_MESH_VERTICES:
        raise ValueError(f"Mesh vertices must contain 1–{MAX_MESH_VERTICES} XYZ points")
    clean_vertices = [vec3(vertex) for vertex in vertices]
    if not isinstance(edges, (list, tuple)) or len(edges) > MAX_MESH_EDGES:
        raise ValueError(f"Mesh edges must contain at most {MAX_MESH_EDGES} pairs")
    if not isinstance(faces, (list, tuple)) or len(faces) > MAX_MESH_FACES:
        raise ValueError(f"Mesh faces must contain at most {MAX_MESH_FACES} polygons")
    if not edges and not faces:
        raise ValueError("Mesh needs at least one edge or face")

    def clean_index(index):
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(clean_vertices):
            raise ValueError("Mesh index is outside the vertex array")
        return index

    clean_edges = []
    for edge in edges:
        if not isinstance(edge, (list, tuple)) or len(edge) != 2:
            raise ValueError("Each mesh edge must have two vertex indices")
        pair = tuple(clean_index(index) for index in edge)
        if pair[0] == pair[1]:
            raise ValueError("Mesh edge cannot connect a vertex to itself")
        clean_edges.append(pair)

    clean_faces = []
    for face in faces:
        if not isinstance(face, (list, tuple)) or not 3 <= len(face) <= 256:
            raise ValueError("Each mesh face must have 3–256 vertex indices")
        polygon = tuple(clean_index(index) for index in face)
        if len(set(polygon)) != len(polygon):
            raise ValueError("Mesh face contains duplicate vertex indices")
        first = clean_vertices[polygon[0]]
        non_collinear = False
        for middle_index in range(1, len(polygon) - 1):
            middle = clean_vertices[polygon[middle_index]]
            last = clean_vertices[polygon[middle_index + 1]]
            ab = [middle[i] - first[i] for i in range(3)]
            ac = [last[i] - first[i] for i in range(3)]
            cross = (ab[1] * ac[2] - ab[2] * ac[1],
                     ab[2] * ac[0] - ab[0] * ac[2],
                     ab[0] * ac[1] - ab[1] * ac[0])
            if sum(component * component for component in cross) > 1e-20:
                non_collinear = True
                break
        if not non_collinear:
            raise ValueError("Mesh face has zero area")
        clean_faces.append(polygon)
    return clean_vertices, clean_edges, clean_faces


def _unique_name(base: str, occupied: set[str]) -> str:
    if base not in occupied:
        return base
    for index in range(1, 10000):
        candidate = f"{base[:55]}.{index:03d}"
        if candidate not in occupied:
            return candidate
    raise ValueError("No available object name")


def _object_name(value, default: str, occupied: set[str]) -> str:
    if value is None:
        return _unique_name(default, occupied)
    if not isinstance(value, str) or not value.strip() or len(value) > 60 or "\x00" in value:
        raise ValueError("Object name must be a nonempty string of at most 60 characters")
    if value in occupied:
        raise ValueError(f"Object name already exists: {value}")
    return value


def _target(value, catalog: dict) -> str | tuple | None:
    if value is None:
        return None
    if isinstance(value, str):
        if value not in catalog:
            raise ValueError(f"Target object not found: {value}")
        return value
    return vec3(value)


def prepare_mutations(command: dict) -> list[dict]:
    op = command["op"]
    if op == "batch.execute":
        args = command.get("args") or {}
        if not isinstance(args, dict):
            raise ValueError("Batch args must be an object")
        raw_steps = args.get("commands")
        if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_BATCH_STEPS:
            raise ValueError(f"Batch commands must contain 1–{MAX_BATCH_STEPS} steps")
    else:
        raw_steps = [command]

    # The catalog models the result of earlier steps before any Blender data changes.
    catalog = {
        obj.name: {
            "type": obj.type,
            "owned": bool(obj.get(OWNER_PROP, False)),
            "in_scene": True,
            "shared_scene": len(obj.users_scene) > 1,
        }
        for obj in bpy.context.scene.objects
    }
    occupied = {obj.name for obj in bpy.data.objects}
    active_camera = bpy.context.scene.camera.name if bpy.context.scene.camera else None
    normalized = []
    for index, step in enumerate(raw_steps):
        if not isinstance(step, dict):
            raise ValueError(f"Batch step {index + 1} must be an object")
        step_op = step.get("op")
        if step_op not in BATCH_OPS:
            raise ValueError(f"Unsupported batch mutation at step {index + 1}: {step_op}")
        args = step.get("args") or {}
        if not isinstance(args, dict):
            raise ValueError(f"Args at step {index + 1} must be an object")
        prepared = {"op": step_op, "args": {}}
        clean = prepared["args"]

        if step_op in {"object.create", "camera.create"}:
            kind = "camera" if step_op == "camera.create" else str(args.get("type", "cube")).lower()
            if kind in {"uv_sphere"}:
                kind = "sphere"
            if kind == "custom_mesh":
                kind = "mesh"
            if kind not in {"cube", "plane", "sphere", "triangle", "mesh", "empty", "camera"}:
                raise ValueError(f"Unsupported object type: {kind}")
            if step_op == "object.create" and kind == "camera":
                raise ValueError("Use camera.create for cameras")
            default = ("ASTRA_CAMERA" if kind == "camera" else kind.title())
            if op == "batch.execute":
                default = f"ASTRA_{command['id'][:24]}_{index + 1:03d}"
            name = _object_name(args.get("name"), default, occupied)
            clean.update({"name": name, "type": kind,
                          "location": vec3(args.get("location"), (0.0, -10.0, 2.0) if kind == "camera" else (0.0, 0.0, 0.0)),
                          "rotation": rotation_radians(args),
                          "scale": vec3(args.get("scale"), (1.0, 1.0, 1.0))})
            if kind in {"cube", "plane", "sphere", "triangle", "mesh"}:
                clean["geometry"] = _mesh_geometry(kind, args)
            if kind == "camera":
                clean["lens"] = positive_float(args.get("lens", 50.0), "lens")
                clean["target"] = _target(args.get("target"), catalog)
                clean["make_active"] = args.get("make_active", True)
                if not isinstance(clean["make_active"], bool):
                    raise ValueError("make_active must be a boolean")
                if clean["make_active"]:
                    active_camera = name
            catalog[name] = {"type": "CAMERA" if kind == "camera" else "EMPTY" if kind == "empty" else "MESH",
                             "owned": True, "in_scene": True, "shared_scene": False}
            occupied.add(name)

        elif step_op in {"object.transform", "object.hide", "object.show", "object.delete"}:
            name = args.get("name")
            if not isinstance(name, str) or name not in catalog:
                raise ValueError(f"Object not found: {name}")
            clean["name"] = name
            if step_op == "object.transform":
                clean["location"] = vec3(args.get("location"))
                clean["rotation"] = rotation_radians(args)
                clean["scale"] = vec3(args.get("scale"))
            elif step_op in {"object.hide", "object.show"}:
                clean["viewport"] = args.get("viewport", True)
                clean["render"] = args.get("render", True)
                if not isinstance(clean["viewport"], bool) or not isinstance(clean["render"], bool):
                    raise ValueError("viewport and render must be booleans")
                clean["hidden"] = False if step_op == "object.show" else args.get("hidden", True)
                if not isinstance(clean["hidden"], bool):
                    raise ValueError("hidden must be a boolean")
            else:
                catalog.pop(name)
                occupied.discard(name)
                if active_camera == name:
                    active_camera = None

        elif step_op == "scene.reset":
            if args.get("scope", "bridge") != "bridge":
                raise ValueError("scene.reset only supports scope='bridge'")
            removed = [name for name, item in catalog.items()
                       if item["owned"] and item["in_scene"] and not item["shared_scene"]]
            skipped_shared = [name for name, item in catalog.items()
                              if item["owned"] and item["in_scene"] and item["shared_scene"]]
            clean["names"] = removed
            clean["skipped_shared"] = skipped_shared
            for name in removed:
                catalog.pop(name)
                occupied.discard(name)
            if active_camera in removed:
                active_camera = None

        elif step_op == "camera.set":
            name = args.get("name") or active_camera
            if not isinstance(name, str) or name not in catalog or catalog[name]["type"] != "CAMERA":
                raise ValueError(f"Camera not found: {name}")
            clean.update({"name": name,
                          "location": vec3(args.get("location")),
                          "rotation": rotation_radians(args),
                          "target": _target(args.get("target"), catalog),
                          "make_active": args.get("make_active", True)})
            if not isinstance(clean["make_active"], bool):
                raise ValueError("make_active must be a boolean")
            if "lens" in args:
                clean["lens"] = positive_float(args["lens"], "lens")
            if clean["make_active"]:
                active_camera = name

        normalized.append(prepared)
    return normalized


def _create_object(args: dict) -> bpy.types.Object:
    kind = args["type"]
    name = args["name"]
    data = None
    obj = None
    try:
        if kind in {"cube", "plane", "sphere", "triangle", "mesh"}:
            data = bpy.data.meshes.new(f"{name}_MESH")
            vertices, edges, faces = args["geometry"]
            data.from_pydata(vertices, edges, faces)
            if data.validate():
                raise ValueError("Blender rejected part of the mesh geometry")
            data.update()
            if kind == "sphere":
                for polygon in data.polygons:
                    polygon.use_smooth = True
        elif kind == "camera":
            data = bpy.data.cameras.new(f"{name}_DATA")
            data.lens = args["lens"]
        obj = bpy.data.objects.new(name, data)
        if obj.name != name:
            raise ValueError(f"Blender could not reserve object name: {name}")
        bpy.context.scene.collection.objects.link(obj)
        if kind == "empty":
            obj.empty_display_type = "PLAIN_AXES"
        obj[OWNER_PROP] = True
        obj.location = args["location"]
        if args["rotation"] is not None:
            obj.rotation_mode = "XYZ"
            obj.rotation_euler = args["rotation"]
        obj.scale = args["scale"]
        bpy.context.view_layer.update()
        return obj
    except Exception:
        if obj is not None and obj.name in bpy.data.objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        if data is not None and data.users == 0:
            if kind == "camera":
                bpy.data.cameras.remove(data)
            else:
                bpy.data.meshes.remove(data)
        raise


def _snapshot_object(obj: bpy.types.Object) -> dict:
    return {
        "location": tuple(obj.location),
        "rotation": tuple(obj.rotation_euler),
        "rotation_mode": obj.rotation_mode,
        "rotation_quaternion": tuple(obj.rotation_quaternion),
        "rotation_axis_angle": tuple(obj.rotation_axis_angle),
        "scale": tuple(obj.scale),
        "hide_viewport": obj.hide_viewport,
        "hide_render": obj.hide_render,
        "lens": obj.data.lens if obj.type == "CAMERA" else None,
    }


def _restore_object(obj: bpy.types.Object, snapshot: dict) -> None:
    obj.location = snapshot["location"]
    obj.rotation_euler = snapshot["rotation"]
    obj.rotation_quaternion = snapshot["rotation_quaternion"]
    obj.rotation_axis_angle = snapshot["rotation_axis_angle"]
    obj.rotation_mode = snapshot["rotation_mode"]
    obj.scale = snapshot["scale"]
    obj.hide_viewport = snapshot["hide_viewport"]
    obj.hide_render = snapshot["hide_render"]
    if obj.type == "CAMERA":
        obj.data.lens = snapshot["lens"]


def _detach_object(obj: bpy.types.Object, undo: list, deferred: list) -> None:
    collections = tuple(obj.users_collection)
    old_camera = bpy.context.scene.camera
    old_name = obj.name
    tombstone = f"__ASTRA_REMOVED_{uuid.uuid4().hex}"

    def restore():
        obj.name = old_name
        if obj.name != old_name:
            raise RuntimeError(f"Could not restore object name: {old_name}")
        for collection in collections:
            if obj.name not in collection.objects:
                collection.objects.link(obj)
        bpy.context.scene.camera = old_camera

    undo.append(restore)
    for collection in collections:
        collection.objects.unlink(obj)
    if bpy.context.scene.camera == obj:
        bpy.context.scene.camera = None
    obj.name = tombstone
    if obj.name != tombstone:
        raise RuntimeError("Blender could not reserve a temporary deletion name")
    deferred.append(obj)


def _apply_step(step: dict, undo: list, deferred: list) -> dict:
    op, args = step["op"], step["args"]
    scene = bpy.context.scene
    if op in {"object.create", "camera.create"}:
        obj = _create_object(args)

        def remove_created():
            if obj.name in bpy.data.objects:
                bpy.data.objects.remove(obj, do_unlink=True)

        undo.append(remove_created)
        if args["type"] == "camera":
            old_camera = scene.camera
            undo.append(lambda old=old_camera: setattr(scene, "camera", old))
            if args["target"] is not None:
                aim_camera(obj, args["target"])
            if args["make_active"]:
                scene.camera = obj
        return object_payload(obj)

    if op in {"object.transform", "object.hide", "object.show", "camera.set"}:
        obj = bpy.data.objects[args["name"]]
        snapshot = _snapshot_object(obj)
        undo.append(lambda target=obj, saved=snapshot: _restore_object(target, saved))
        if op in {"object.transform", "camera.set"}:
            if args["location"] is not None:
                obj.location = args["location"]
            if args["rotation"] is not None:
                obj.rotation_mode = "XYZ"
                obj.rotation_euler = args["rotation"]
            if op == "object.transform" and args["scale"] is not None:
                obj.scale = args["scale"]
        if op in {"object.hide", "object.show"}:
            if args["viewport"]:
                obj.hide_viewport = args["hidden"]
            if args["render"]:
                obj.hide_render = args["hidden"]
        if op == "camera.set":
            old_camera = scene.camera
            undo.append(lambda old=old_camera: setattr(scene, "camera", old))
            if "lens" in args:
                obj.data.lens = args["lens"]
            if args["target"] is not None:
                bpy.context.view_layer.update()
                aim_camera(obj, args["target"])
            if args["make_active"]:
                scene.camera = obj
        bpy.context.view_layer.update()
        return object_payload(obj)

    if op == "object.delete":
        obj = bpy.data.objects[args["name"]]
        _detach_object(obj, undo, deferred)
        return {"name": args["name"], "deleted": True}

    if op == "scene.reset":
        removed = []
        for name in args["names"]:
            obj = bpy.data.objects.get(name)
            if obj is not None and obj.users_collection:
                _detach_object(obj, undo, deferred)
                removed.append(name)
        return {"scope": "bridge", "deleted": removed, "count": len(removed),
                "skipped_shared": args["skipped_shared"]}

    raise ValueError(f"Unsupported mutation: {op}")


class RecoveryRequired(RuntimeError):
    """The current scene may need manual inspection or checkpoint recovery."""


def _run_mutations(command: dict, root: Path, steps: list[dict]) -> tuple[dict, str | None]:
    checkpoint = checkpoint_if_needed(root, command)
    undo = []
    deferred = []
    initial_pointers = {obj.as_pointer() for obj in bpy.data.objects}
    initial_camera = bpy.context.scene.camera
    results = []
    try:
        for step in steps:
            results.append(_apply_step(step, undo, deferred))
    except Exception as exc:
        rollback_errors = []
        for restore in reversed(undo):
            try:
                restore()
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        try:
            bpy.context.scene.camera = initial_camera
            for obj in list(bpy.data.objects):
                if obj.as_pointer() not in initial_pointers:
                    bpy.data.objects.remove(obj, do_unlink=True)
            bpy.context.view_layer.update()
        except Exception as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        if rollback_errors:
            raise RecoveryRequired(f"{exc}; rollback also failed: {'; '.join(rollback_errors)}") from exc
        raise

    # Deletions were already unlinked from every collection. Purge their
    # datablocks after all operations have succeeded; orphan cleanup failure
    # cannot expose a partially edited scene.
    cleanup_warnings = []
    for obj in deferred:
        try:
            if obj.name in bpy.data.objects:
                bpy.data.objects.remove(obj, do_unlink=True)
        except Exception as exc:
            cleanup_warnings.append(f"Could not purge {obj.name}: {exc}")
    payload = {"steps": results, "count": len(results)} if command["op"] == "batch.execute" else results[0]
    if cleanup_warnings:
        payload["cleanup_warnings"] = cleanup_warnings
    return payload, checkpoint


def sync_status(root: Path) -> dict:
    source = root / "runtime" / "bridge_status.json"
    if not source.exists():
        return {"health": "missing", "heartbeat_at": None, "stale": True,
                "message": "Start the Git sync bridge"}
    try:
        report = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("Status must be an object")
        heartbeat = report.get("heartbeat_at")
        if not isinstance(heartbeat, str):
            raise ValueError("Heartbeat timestamp is missing")
        heartbeat_time = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
        if heartbeat_time.tzinfo is None:
            raise ValueError("Heartbeat timestamp lacks timezone")
        age = max(0, int((datetime.now(timezone.utc) - heartbeat_time).total_seconds()))
        stale = age > 30
        state = report.get("state")
        if state not in {"ready", "syncing", "error"}:
            state = "unknown"
        health = "stale" if stale else state
        result = {
            "health": health,
            "state": state,
            "heartbeat_at": heartbeat,
            "heartbeat_age_seconds": age,
            "stale": stale,
            "last_success_at": report.get("last_success_at") if isinstance(report.get("last_success_at"), str) else None,
            "consecutive_failures": report.get("consecutive_failures") if isinstance(report.get("consecutive_failures"), int) else None,
        }
        if state == "error":
            result["message"] = "Git sync error; see local runtime/bridge_status.json"
        elif stale:
            result["message"] = "Git sync heartbeat is stale"
        return result
    except (OSError, ValueError, TypeError):
        return {"health": "invalid", "heartbeat_at": None, "stale": True,
                "message": "Git sync status is unreadable"}


def bridge_status(root: Path) -> dict:
    paths = exchange_paths(root)
    prefs = addon_prefs()
    return {
        "bridge_version": "0.2.0",
        "blender_version": bpy.app.version_string,
        "state": _STATUS["state"],
        "started_at": _STATUS["started_at"],
        "heartbeat_at": _STATUS.get("heartbeat_at"),
        "last_command_id": _STATUS["last_command_id"],
        "last_command_status": _STATUS["last_command_status"],
        "last_command_at": _STATUS["last_command_at"],
        "last_error": public_error(_STATUS["last_error"]) if _STATUS["last_error"] else None,
        "last_mutation_requires_save": _STATUS["last_mutation_requires_save"],
        "scene_dirty": bool(bpy.data.is_dirty),
        "auto_process": bool(prefs and prefs.auto_process),
        "pending": len(list(paths["inbox"].glob("*.json"))),
        "processing": len(list(paths["processing"].glob("*.json"))),
        "scene": bpy.context.scene.name if bpy.context.scene else None,
        "blend_file": Path(bpy.data.filepath).name if bpy.data.filepath else None,
        "sync": sync_status(root),
    }


def write_heartbeat(root: Path, force: bool = False) -> None:
    global _LAST_HEARTBEAT
    now = time.monotonic()
    if not force and now - _LAST_HEARTBEAT < HEARTBEAT_SECONDS:
        return
    _STATUS["heartbeat_at"] = utc_now()
    atomic_json(root / "runtime" / "heartbeat.json", bridge_status(root))
    _LAST_HEARTBEAT = now


def _inspect_scene(args: dict) -> dict:
    scene = bpy.context.scene
    include_objects = args.get("include_objects", True)
    if not isinstance(include_objects, bool):
        raise ValueError("include_objects must be a boolean")
    limit = bounded_int(args.get("limit", 100), "limit", 1, 1000)
    offset = bounded_int(args.get("offset", 0), "offset", 0, 1000000)
    objects = sorted(scene.objects, key=lambda obj: obj.name)
    payload = {
        "scene": scene.name,
        "frame_current": scene.frame_current,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "render_engine": scene.render.engine,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage],
        "render_format": scene.render.image_settings.file_format,
        "render_output": public_path(scene.render.filepath),
        "camera": scene.camera.name if scene.camera else None,
        "object_count": len(objects),
        "bridge_object_count": sum(bool(obj.get(OWNER_PROP, False)) for obj in objects),
        "collections": [collection.name for collection in bpy.data.collections],
        "selected_objects": [obj.name for obj in bpy.context.selected_objects],
        "blend_file": Path(bpy.data.filepath).name if bpy.data.filepath else None,
        "scene_dirty": bool(bpy.data.is_dirty),
    }
    if include_objects:
        payload["objects"] = [object_payload(obj) for obj in objects[offset:offset + limit]]
        payload["offset"] = offset
        payload["limit"] = limit
        payload["has_more"] = offset + limit < len(objects)
    return payload


def _render_preview(command: dict, root: Path, args: dict) -> dict:
    scene = bpy.context.scene
    camera_name = args.get("camera")
    camera = bpy.data.objects.get(camera_name) if isinstance(camera_name, str) else scene.camera
    if camera is None or camera.type != "CAMERA" or camera.name not in scene.objects:
        raise RuntimeError(f"Scene has no available camera: {camera_name}")
    percentage = bounded_int(args.get("resolution_percentage", 50), "resolution_percentage", 1, 100)
    frame = args.get("frame", scene.frame_current)
    frame = bounded_int(frame, "frame", -1048574, 1048574)
    paths = exchange_paths(root)
    preview = paths["previews"] / f"{safe_id(command['id'])}.png"
    old_path = scene.render.filepath
    old_percentage = scene.render.resolution_percentage
    old_format = scene.render.image_settings.file_format
    old_camera = scene.camera
    old_frame = scene.frame_current
    try:
        scene.camera = camera
        scene.frame_set(frame)
        scene.render.filepath = str(preview)
        scene.render.resolution_percentage = percentage
        scene.render.image_settings.file_format = "PNG"
        outcome = bpy.ops.render.render(write_still=True)
        if "FINISHED" not in outcome or not preview.exists():
            raise RuntimeError("Blender did not write the preview image")
    finally:
        scene.render.filepath = old_path
        scene.render.resolution_percentage = old_percentage
        scene.render.image_settings.file_format = old_format
        scene.camera = old_camera
        scene.frame_set(old_frame)
    return {
        "preview": f"previews/{preview.name}",
        "camera": camera.name,
        "frame": frame,
        "resolution_percentage": percentage,
        "bytes": preview.stat().st_size,
    }


def _execute_readonly(command: dict, root: Path) -> dict:
    op = command["op"]
    args = command.get("args") or {}
    if not isinstance(args, dict):
        raise ValueError("Command args must be an object")
    if op == "bridge.ping":
        return {
            "bridge_version": "0.2.0",
            "blender_version": bpy.app.version_string,
            "scene": bpy.context.scene.name if bpy.context.scene else None,
            "blend_file": Path(bpy.data.filepath).name if bpy.data.filepath else None,
            "status": bridge_status(root),
        }
    if op == "bridge.status":
        return bridge_status(root)
    if op == "scene.inspect":
        return _inspect_scene(args)
    if op == "object.inspect":
        name = args.get("name")
        obj = bpy.context.scene.objects.get(str(name)) if name else bpy.context.active_object
        if obj is None or obj.name not in bpy.context.scene.objects:
            raise ValueError(f"Object not found: {name}")
        return object_payload(obj)
    if op == "collection.inspect":
        name = args.get("name")
        if name:
            collection = bpy.data.collections.get(str(name))
            if collection is None:
                raise ValueError(f"Collection not found: {name}")
            return {"name": collection.name,
                    "objects": [obj.name for obj in collection.objects],
                    "children": [child.name for child in collection.children]}
        return {"collections": [
            {"name": collection.name, "objects": len(collection.objects),
             "children": [child.name for child in collection.children]}
            for collection in bpy.data.collections
        ]}
    if op == "render.preview":
        return _render_preview(command, root, args)
    raise ValueError(f"Unsupported operation: {op}")


def execute_with_checkpoint(command: dict, root: Path) -> tuple[dict, str | None]:
    op = command["op"]
    if op == "scene.save":
        if not bpy.data.filepath:
            raise RuntimeError("Current Blender file has never been saved. Save it once manually first.")
        checkpoint = checkpoint_if_needed(root, command)
        outcome = bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath)
        if "FINISHED" not in outcome:
            raise RuntimeError("Blender did not save the scene")
        return {"saved": True, "blend_file": Path(bpy.data.filepath).name}, checkpoint
    if op in MUTATING_OPS:
        steps = prepare_mutations(command)
        return _run_mutations(command, root, steps)
    return _execute_readonly(command, root), None


def execute(command: dict, root: Path) -> dict:
    """Execute a command directly; the queue uses execute_with_checkpoint."""
    return execute_with_checkpoint(command, root)[0]


def _checkpoint_candidates(root: Path, command_id: str) -> list[str]:
    paths = exchange_paths(root)
    matches = sorted(paths["checkpoints"].glob(f"*_{command_id}_*.blend"), reverse=True)
    return [f"runtime/checkpoints/{path.name}" for path in matches[:3]]


def _result_error(command_id: str, op, message: str, digest=None,
                  recovery_required=False) -> dict:
    result = {
        "id": command_id,
        "op": op,
        "status": "error",
        "completed_at": utc_now(),
        "error": public_error(message),
        "recovery_required": recovery_required,
    }
    if digest:
        result["command_hash"] = digest
    return result


def _publish_conflict(paths: dict, command_id: str, digest: str, message: str) -> None:
    conflict = paths["results"] / f"{command_id}.conflict-{digest[:12]}.json"
    if not conflict.exists():
        publish_json(conflict, _result_error(command_id, None, message, digest))


def _prior_command_hash(paths: dict, command_id: str, result: dict) -> str | None:
    digest = result.get("command_hash")
    if isinstance(digest, str):
        return digest
    # v0.1 results lacked a hash. Compare the already archived source if present.
    old_command = paths["processed"] / f"{command_id}.json"
    if old_command.exists():
        try:
            prior = json.loads(old_command.read_text(encoding="utf-8"))
            if isinstance(prior, dict):
                prior["id"] = command_id
                return command_hash(prior)
        except (OSError, ValueError):
            pass
    return None


def process_one(path: Path, root: Path) -> None:
    """Process a claimed command or quarantine an interrupted command."""
    paths = exchange_paths(root)
    fallback_id = path.stem
    try:
        fallback_id = safe_id(fallback_id)
    except ValueError:
        fallback_id = f"invalid-{hashlib.sha256(path.name.encode()).hexdigest()[:12]}"
    command_id = fallback_id
    op = None
    digest = None
    try:
        command = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(command, dict):
            raise ValueError("Command must be a JSON object")
        command_id = safe_id(command.get("id") or path.stem)
        if command_id != path.stem:
            raise ValueError("Command id must match the JSON filename")
        command["id"] = command_id
        op = command.get("op")
        if not isinstance(op, str) or not op:
            raise ValueError("Command is missing 'op'")
        digest = command_hash(command)
    except Exception as exc:
        result_path = paths["results"] / f"{fallback_id}.json"
        traceback.print_exc()
        error = _result_error(fallback_id, op, str(exc))
        if result_path.exists():
            _publish_conflict(paths, fallback_id,
                              hashlib.sha256(path.name.encode()).hexdigest(),
                              f"Rejected malformed command: {exc}")
        else:
            publish_json(result_path, error)
        archive_command(path, paths["processed"])
        _STATUS.update({"last_command_id": fallback_id, "last_command_status": "error",
                        "last_command_at": utc_now(), "last_error": str(exc)})
        write_heartbeat(root, force=True)
        return

    result_path = paths["results"] / f"{command_id}.json"
    marker_path = paths["inflight"] / f"{command_id}.json"
    if result_path.exists():
        try:
            prior_result = json.loads(result_path.read_text(encoding="utf-8"))
            prior_digest = _prior_command_hash(paths, command_id, prior_result)
            if prior_digest is not None and prior_digest != digest:
                _publish_conflict(paths, command_id, digest,
                                  "Command id was already used for different content; existing result preserved")
                _STATUS.update({"last_command_id": command_id, "last_command_status": "conflict",
                                "last_command_at": utc_now(), "last_error": "Command id conflict"})
        except (OSError, ValueError) as exc:
            _publish_conflict(paths, command_id, digest,
                              f"Existing result is unreadable; command was not replayed: {exc}")
        marker_path.unlink(missing_ok=True)
        archive_command(path, paths["processed"])
        write_heartbeat(root, force=True)
        return

    if marker_path.exists():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            marker = {}
        error = _result_error(
            command_id, op,
            "A previous Blender run stopped after this command started. Inspect the scene or restore its checkpoint before retrying with a new id.",
            digest, recovery_required=True,
        )
        error["checkpoint_candidates"] = _checkpoint_candidates(root, command_id)
        error["started_at"] = marker.get("started_at")
        publish_json(result_path, error)
        archive_command(path, paths["processed"])
        _STATUS.update({"last_command_id": command_id, "last_command_status": "recovery_required",
                        "last_command_at": utc_now(), "last_error": error["error"]})
        write_heartbeat(root, force=True)
        return

    atomic_json(marker_path, {
        "id": command_id, "op": op, "command_hash": digest,
        "started_at": utc_now(),
        "blend_file": bpy.data.filepath or None,
    })
    _STATUS.update({"state": "busy", "current_command_id": command_id})
    write_heartbeat(root, force=True)
    try:
        payload, checkpoint = execute_with_checkpoint(command, root)
        result = {
            "id": command_id,
            "op": op,
            "status": "success",
            "completed_at": utc_now(),
            "command_hash": digest,
            "checkpoint_created": bool(checkpoint),
            "checkpoint": checkpoint,
            "result": payload,
        }
        if op in MUTATING_OPS:
            result["scene_saved"] = op == "scene.save"
            result["requires_save"] = op != "scene.save"
    except Exception as exc:
        traceback.print_exc()
        result = _result_error(command_id, op, str(exc), digest,
                               recovery_required=isinstance(exc, RecoveryRequired))
        result["checkpoint_candidates"] = _checkpoint_candidates(root, command_id)
    publish_json(result_path, result)
    marker_path.unlink(missing_ok=True)
    archive_command(path, paths["processed"])
    _STATUS.update({"state": "ready", "current_command_id": None,
                    "last_command_id": command_id, "last_command_status": result["status"],
                    "last_command_at": result["completed_at"],
                    "last_error": result.get("error")})
    if result["status"] == "success" and op in MUTATING_OPS:
        _STATUS["last_mutation_requires_save"] = op != "scene.save"
    write_heartbeat(root, force=True)


def _inbox_file_ready(path: Path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except (OSError, ValueError):
        # An in-progress checkout may expose a partial JSON file briefly.
        try:
            return time.time() - path.stat().st_mtime >= 2.0
        except OSError:
            return False


def process_inbox(force: bool = False) -> int:
    root = repo_root()
    prefs = addon_prefs()
    if root is None or prefs is None or (not prefs.auto_process and not force):
        return 0
    paths = exchange_paths(root)
    count = 0
    for path in sorted(paths["processing"].glob("*.json")):
        if count >= MAX_COMMANDS_PER_POLL:
            return count
        process_one(path, root)
        count += 1
    for path in sorted(paths["inbox"].glob("*.json")):
        if count >= MAX_COMMANDS_PER_POLL:
            break
        if not _inbox_file_ready(path):
            continue
        claimed = paths["processing"] / path.name
        if claimed.exists():
            continue
        try:
            path.rename(claimed)
        except FileNotFoundError:
            continue
        process_one(claimed, root)
        count += 1
    return count


def timer_callback():
    try:
        root = repo_root()
        prefs = addon_prefs()
        if root is not None:
            if prefs and prefs.auto_process:
                process_inbox()
                if _STATUS["state"] != "busy":
                    _STATUS["state"] = "ready"
            else:
                _STATUS["state"] = "paused"
            write_heartbeat(root)
    except Exception as exc:
        _STATUS.update({"state": "error", "last_error": str(exc)})
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
        count = process_inbox(force=True)
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
        sync = sync_status(root) if configured else None

        row = layout.row()
        row.label(text="Status:")
        if not configured:
            label, icon = "SET REPO PATH", "ERROR"
        elif _STATUS["state"] == "error":
            label, icon = "ADD-ON ERROR", "ERROR"
        elif sync["health"] == "ready" and prefs and prefs.auto_process:
            label, icon = "READY", "CHECKMARK"
        elif sync["health"] == "syncing":
            label, icon = "SYNCING", "FILE_REFRESH"
        elif sync["health"] == "error":
            label, icon = "SYNC ERROR", "ERROR"
        elif sync["health"] in {"missing", "stale", "invalid"}:
            label, icon = "SYNC OFFLINE", "ERROR"
        else:
            label, icon = "POLLING PAUSED", "PAUSE"
        row.label(text=label, icon=icon)

        if prefs:
            layout.prop(prefs, "auto_process", text="Bridge Active")
            layout.prop(prefs, "checkpoint_before_mutation", text="Auto Checkpoint")

        layout.operator("astra.process_now", icon="FILE_REFRESH")
        layout.operator("astra.open_preferences", icon="PREFERENCES")

        if configured:
            paths = exchange_paths(root)
            pending = len(list(paths["inbox"].glob("*.json")))
            layout.label(text=f"Pending commands: {pending}")
            layout.label(text=f"Bridge state: {_STATUS['state'].upper()}")
            layout.label(text=f"Git sync: {sync['health'].upper()}")
            if _STATUS["last_command_id"]:
                layout.label(text=f"Last: {_STATUS['last_command_id']} ({_STATUS['last_command_status']})")
            if _STATUS["last_mutation_requires_save"]:
                layout.label(text="Scene has unsaved ASTRA changes", icon="ERROR")
            if _STATUS.get("heartbeat_at"):
                layout.label(text=f"Heartbeat: {_STATUS['heartbeat_at'][11:19]} UTC")
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
    _STATUS["state"] = "ready"
    if not bpy.app.timers.is_registered(timer_callback):
        bpy.app.timers.register(timer_callback, first_interval=TIMER_SECONDS, persistent=True)
        _TIMER_REGISTERED = True


def unregister():
    global _TIMER_REGISTERED
    root = repo_root()
    _STATUS["state"] = "stopped"
    if root is not None:
        try:
            write_heartbeat(root, force=True)
        except OSError:
            pass
    if bpy.app.timers.is_registered(timer_callback):
        bpy.app.timers.unregister(timer_callback)
    _TIMER_REGISTERED = False
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
