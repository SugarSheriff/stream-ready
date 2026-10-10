"""Headless end-to-end test inside a real Blender. Not part of `unittest discover`.

Installs dist/stream-ready-extension.zip the way a user would, builds a scene with one
known problem per rule, checks the findings and the one-click fixes, exports real USD
and inspects it with OpenUSD, then publishes to the mock Miris server with faults
injected. Run through tests/run_blender_smoke.ps1, which isolates Blender's user folders.
"""

import os
import shutil
import sys
import tempfile
import time
import traceback

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from mock.mock_miris import MirisState, serve_in_thread  # noqa: E402

failures = []
TMP = tempfile.mkdtemp(prefix="stream_ready_smoke_")


def check(condition, message):
    print(("  ok    " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


def install():
    zip_path = os.path.join(REPO, "dist", "stream-ready-extension.zip")
    bpy.ops.extensions.package_install_files(filepath=zip_path, repo="user_default", enable_on_install=True)
    module = next(name for name in bpy.context.preferences.addons.keys() if name.endswith(".stream_ready"))
    print(f"installed as {module}")
    return module


def png_on_disk(name, size=8):
    image = bpy.data.images.new(name, size, size)
    image.pixels = [0.6, 0.4, 0.2, 1.0] * (size * size)
    image.filepath_raw = os.path.join(TMP, f"{name}.png")
    image.file_format = "PNG"
    image.save()
    return image


def new_material(name):
    mat = bpy.data.materials.new(name)
    if bpy.app.version < (5, 0, 0):  # 5.0+ materials always have nodes
        mat.use_nodes = True
    return mat


def textured_material(name, image):
    mat = new_material(name)
    tree = mat.node_tree
    bsdf = next(n for n in tree.nodes if n.type == "BSDF_PRINCIPLED")
    tex = tree.nodes.new("ShaderNodeTexImage")
    tex.image = image
    tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    return mat


def cube(name, location=(0, 0, 0), scale=(1, 1, 1)):
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = scale
    return obj


def world_with_hdri(intensity):
    """An HDRI World with the Miris guide's "Blender Light Intensity" Multiply node."""
    world = bpy.data.worlds.new("Studio")
    bpy.context.scene.world = world
    if bpy.app.version < (5, 0, 0):
        world.use_nodes = True
    tree = world.node_tree
    background = next(n for n in tree.nodes if n.type == "BACKGROUND")
    env = tree.nodes.new("ShaderNodeTexEnvironment")
    env.image = png_on_disk("studio_hdri", 16)
    mult = tree.nodes.new("ShaderNodeVectorMath")
    mult.operation = "MULTIPLY"
    mult.label = "Blender Light Intensity"
    mult.inputs[1].default_value = intensity
    tree.links.new(env.outputs["Color"], mult.inputs[0])
    tree.links.new(mult.outputs["Vector"], background.inputs["Color"])
    return world


def clear_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)


def build_bad_scene():
    clear_scene()
    units = bpy.context.scene.unit_settings
    units.scale_length, units.length_unit = 0.01, "CENTIMETERS"

    chair = cube("Chair")
    gone = bpy.data.images.new("wood_missing", 4, 4)
    gone.source, gone.filepath = "FILE", "//does_not_exist.png"
    mat = textured_material("Wood", gone)
    noise = mat.node_tree.nodes.new("ShaderNodeTexNoise")
    bsdf = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    mat.node_tree.links.new(noise.outputs["Fac"], bsdf.inputs["Roughness"])
    chair.data.materials.append(mat)

    glow = cube("Glow", location=(3, 0, 0))
    emit = new_material("Glow")
    tree = emit.node_tree
    for node in list(tree.nodes):
        if node.type != "OUTPUT_MATERIAL":
            tree.nodes.remove(node)
    emission = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(emission.outputs["Emission"], tree.nodes["Material Output"].inputs["Surface"])
    glow.data.materials.append(emit)

    cube("Mirror", location=(-3, 0, 0), scale=(-1, 1, 1))
    world_with_hdri((1.0, 1.0, 0.5))  # tinted: the guide requires equal components
    bpy.context.view_layer.update()


def build_good_scene():
    clear_scene()
    units = bpy.context.scene.unit_settings
    units.system, units.scale_length, units.length_unit = "METRIC", 1.0, "METERS"
    chair = cube("Chair", scale=(0.25, 0.25, 0.45))
    chair.data.materials.append(textured_material("Oak", png_on_disk("oak_basecolor")))
    world_with_hdri((1.0, 1.0, 1.0))
    bpy.context.view_layer.update()


def main():
    print(f"Blender {bpy.app.version_string}, online_access={getattr(bpy.app, 'online_access', 'n/a')}")
    module = install()
    state = sys.modules[f"{module}.state"]
    prefs = bpy.context.preferences.addons[module].preferences
    settings = bpy.context.scene.stream_ready

    print("\n[preflight: one problem per rule]")
    build_bad_scene()
    bpy.ops.stream_ready.preflight()
    for f in state.findings:
        print(f"        {f.severity:5} {f.code:28} {f.target}")
    got = {f.code for f in state.findings}
    expected = {"units.not_meters", "MISSING_REFERENCED_FILE", "material.procedural",
                "material.unsupported_shader", "transform.negative_scale", "lighting.unequal_rgb"}
    check(got == expected, f"finding codes match (missing {expected - got or '{}'}, extra {got - expected or '{}'})")

    print("\n[publish is blocked by errors]")
    prefs.api_key = "never-used"
    try:
        bpy.ops.stream_ready.publish()
        check(False, "publish refused while preflight has errors")
    except RuntimeError as exc:
        check("Fix errors before publishing" in str(exc), f"publish refused: {str(exc).strip()}")

    print("\n[one-click fix]")
    bpy.ops.stream_ready.fix_units()
    units = bpy.context.scene.unit_settings
    check("units.not_meters" not in {f.code for f in state.findings}, "Fix Units clears units.not_meters")
    check((units.system, units.scale_length, units.length_unit) == ("METRIC", 1.0, "METERS"), "units are Metric / 1.0 / Meters")

    print("\n[export only: real USD, inspected with OpenUSD]")
    build_good_scene()
    settings.asset_name, settings.scope = "oak chair", "VISIBLE"
    settings.export_dir = os.path.join(TMP, "export")
    check(bpy.ops.stream_ready.export() == {"FINISHED"}, "export succeeded with a clean preflight")
    folder = os.path.join(TMP, "export", "oak_chair")
    files = sorted(os.path.relpath(os.path.join(d, f), folder).replace(os.sep, "/")
                   for d, _, fs in os.walk(folder) for f in fs)
    print("        files: " + ", ".join(files))
    check("oak_chair.usd" in files, "root USD named after the asset, .usd extension per the guide")
    check(any(f.startswith("textures/") and "oak_basecolor" in f for f in files), "texture copied into textures/")

    from pxr import Usd, UsdGeom, UsdLux
    stage = Usd.Stage.Open(os.path.join(folder, "oak_chair.usd"))
    check(UsdGeom.GetStageMetersPerUnit(stage) == 1.0,
          f"metersPerUnit is 1.0 (got {UsdGeom.GetStageMetersPerUnit(stage)})")
    check(stage.HasAuthoredMetadata(UsdGeom.Tokens.metersPerUnit), "metersPerUnit is authored, not left to USD's cm fallback")
    check(bool(stage.GetDefaultPrim()), f"defaultPrim set: {stage.GetDefaultPrim().GetPath()}")
    domes = [p for p in stage.Traverse() if p.IsA(UsdLux.DomeLight)]
    check(len(domes) == 1, "World converted to one DomeLight")
    if domes:
        dome = UsdLux.DomeLight(domes[0])
        color = tuple(dome.GetColorAttr().Get())
        check(color == (1562.0, 1562.0, 1562.0), f"DomeLight color calibrated to intensity x 1562: {color}")
        check(dome.GetTextureFileAttr().Get().path.startswith("./textures/"), "DomeLight HDRI copied and relative")
    world_tree = bpy.context.scene.world.node_tree
    mult = next(n for n in world_tree.nodes if n.label == "Blender Light Intensity")
    check(tuple(mult.inputs[1].default_value) == (1.0, 1.0, 1.0), "the scene's own lighting was not touched")
    textures = [a.Get().path for p in stage.Traverse() for a in p.GetAttributes()
                if a.GetTypeName() == "asset" and a.Get() and a.Get().path]
    print("        asset paths: " + ", ".join(textures))
    check(textures and all(not os.path.isabs(t) for t in textures), "every asset path is relative")
    bundle = sys.modules[f"{module}.core.bundle"]
    report = bundle.check(folder)
    for f in report["findings"]:
        print(f"        {f.severity:5} {f.code:28} {f.message}")
    check(report["used_pxr"], "bundle check used OpenUSD inside Blender")
    check(not [f for f in report["findings"] if f.severity == "error"], "export passes the Miris folder rules")
    check(report["size_m"] and abs(report["size_m"][2] - 0.9) < 1e-3, f"measured height 0.9 m: {report['size_m']}")

    print("\n[publish through injected faults]")
    st = MirisState("smoke-key", processing_polls=2, log=lambda m: None)
    server, url = serve_in_thread(st)
    prefs.api_url, prefs.api_key = url, "smoke-key"
    settings.create_viewer_key = True
    check(bpy.ops.stream_ready.test_connection() == {"FINISHED"}, "Test Connection succeeds against the mock")
    st.fail_api, st.throttle_storage = 1, 1
    check(bpy.ops.stream_ready.publish() == {"FINISHED"}, "publish started")
    deadline = time.monotonic() + 60
    while state.publish.running and time.monotonic() < deadline:
        time.sleep(0.1)
    for line in state.publish.lines():
        print("        " + line)
    result = state.publish.result or {}
    check(state.publish.status == "preview", f"publish finished as a preview, like the Miris portal: {state.publish.status}")
    check(bool((result.get("viewer_key") or {}).get("key")), "share key created and held in memory")
    check(not st.leaked_api_key, "API key never sent to the storage host")
    uploaded = sorted(k.split("/", 2)[2] for (_, k) in st.objects)
    check(uploaded == files, f"server received exactly the exported files: {uploaded}")
    bpy.ops.stream_ready.copy(what="embed")
    clip = bpy.context.window_manager.clipboard
    check(result.get("asset_id", "?") in clip or clip == "", "embed snippet names the asset (clipboard may be empty headless)")
    server.shutdown()

    print("\n[cancel]")
    st = MirisState("smoke-key", throttle_storage=10**9, retry_after=5, log=lambda m: None)
    server, url = serve_in_thread(st)
    prefs.api_url = url
    bpy.ops.stream_ready.publish()
    time.sleep(0.5)
    bpy.ops.stream_ready.cancel()
    deadline = time.monotonic() + 15
    while state.publish.running and time.monotonic() < deadline:
        time.sleep(0.05)
    check(state.publish.status == "cancelled", f"cancel stops a publish stuck in backoff: {state.publish.status}")
    asset = next(iter(st.assets.values()), {})
    check(asset.get("status") == "canceled", "cancelled upload is marked canceled on Miris")
    server.shutdown()

    print("\n[unregister]")
    bpy.ops.preferences.addon_disable(module=module)
    check(not hasattr(bpy.types.Scene, "stream_ready"), "disabling the add-on cleans up Scene.stream_ready")


try:
    main()
except Exception:
    traceback.print_exc()
    failures.append("unhandled exception")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'PASSED' if not failures else 'FAILED'}: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
