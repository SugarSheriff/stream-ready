"""Publish a small test chair to the real Miris, using your saved Sign in with Miris.

Run headless through tools/live_publish.ps1, which isolates Blender's user folders.
Creates one asset named "Stream Ready live test" in your workspace (probably one credit).
"""

import os
import sys
import tempfile
import time
import traceback

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="stream_ready_live_")


def png(name, size, rgba):
    image = bpy.data.images.new(name, size, size)
    image.pixels = list(rgba) * (size * size)
    image.filepath_raw = os.path.join(TMP, f"{name}.png")
    image.file_format = "PNG"
    image.save()
    return image


def build_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)
    units = bpy.context.scene.unit_settings
    units.system, units.scale_length, units.length_unit = "METRIC", 1.0, "METERS"

    mat = bpy.data.materials.new("Oak")
    if bpy.app.version < (5, 0, 0):
        mat.use_nodes = True
    tree = mat.node_tree
    bsdf = next(n for n in tree.nodes if n.type == "BSDF_PRINCIPLED")
    tex = tree.nodes.new("ShaderNodeTexImage")
    tex.image = png("oak_basecolor", 64, (0.55, 0.36, 0.2, 1.0))
    tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])

    # A simple chair: seat, back, four legs, joined into one mesh (real-world size, meters).
    parts = [((0, 0, 0.45), (0.22, 0.22, 0.02)), ((0, -0.2, 0.75), (0.22, 0.02, 0.28))]
    parts += [((x, y, 0.22), (0.02, 0.02, 0.22)) for x in (-0.19, 0.19) for y in (-0.19, 0.19)]
    for location, scale in parts:
        bpy.ops.mesh.primitive_cube_add(location=location)
        bpy.context.active_object.scale = scale
    for obj in bpy.data.objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = bpy.data.objects[0]
    bpy.ops.object.transform_apply(scale=True)
    bpy.ops.object.join()
    chair = bpy.context.active_object
    chair.name = "Chair"
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.uv.smart_project()
    bpy.ops.object.mode_set(mode="OBJECT")
    chair.data.materials.append(mat)

    world = bpy.data.worlds.new("Studio")
    bpy.context.scene.world = world
    if bpy.app.version < (5, 0, 0):
        world.use_nodes = True
    wtree = world.node_tree
    background = next(n for n in wtree.nodes if n.type == "BACKGROUND")
    env = wtree.nodes.new("ShaderNodeTexEnvironment")
    env.image = png("studio_sky", 32, (0.8, 0.85, 0.9, 1.0))
    wtree.links.new(env.outputs["Color"], background.inputs["Color"])
    bpy.context.view_layer.update()


def main():
    print(f"Blender {bpy.app.version_string}")
    bpy.ops.extensions.package_install_files(filepath=os.path.join(REPO, "dist", "stream-ready-extension.zip"),
                                             repo="user_default", enable_on_install=True)
    module = next(n for n in bpy.context.preferences.addons.keys() if n.endswith(".stream_ready"))
    state = sys.modules[f"{module}.state"]
    properties = sys.modules[f"{module}.properties"]
    prefs = bpy.context.preferences.addons[module].preferences
    prefs.poll_timeout = 900

    kind, _, label = properties.credential(prefs)
    print(f"credential: {kind or 'none'} ({label})")
    if kind != "oauth":
        print("not signed in; run python tools/miris_login.py first")
        return 1
    if bpy.ops.stream_ready.test_connection() != {"FINISHED"}:
        return 1

    build_scene()
    settings = bpy.context.scene.stream_ready
    settings.asset_name, settings.tags, settings.scope = "Stream Ready live test", "stream-ready, test", "VISIBLE"
    settings.create_viewer_key = False
    bpy.ops.stream_ready.preflight()
    for f in state.findings:
        print(f"  preflight {f.severity:5} {f.code}  {f.target}")

    if bpy.ops.stream_ready.publish() != {"FINISHED"}:
        return 1
    seen = 0
    deadline = time.monotonic() + 1000
    while state.publish.running and time.monotonic() < deadline:
        time.sleep(1)
        lines = state.publish.lines()
        for line in lines[seen:]:
            print("  " + line, flush=True)
        seen = len(lines)
    for line in state.publish.lines()[seen:]:
        print("  " + line)
    result = state.publish.result or {}
    print(f"\nSTATUS: {state.publish.status}  asset={result.get('asset_id')}  state={result.get('state')}  "
          f"job={result.get('job_process_status')}")
    for f in result.get("findings") or []:
        print(f"  server finding: {f.code}: {f.message}")
    return 0 if state.publish.status in ("preview", "ready", "processing") else 1


try:
    code = main()
except Exception:
    traceback.print_exc()
    code = 1
sys.exit(code)
