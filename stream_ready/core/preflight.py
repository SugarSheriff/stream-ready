# SPDX-License-Identifier: GPL-3.0-or-later
"""Scene rules: the Miris Blender guide, checked automatically before export.

Rules run against a plain-dict snapshot (built by ../snapshot.py inside Blender, or by
hand in the tests), never against bpy directly. Snapshot shape:

    {
      "unit_system": "METRIC", "unit_scale": 1.0, "length_unit": "METERS",
      "bounds": [[minx, miny, minz], [maxx, maxy, maxz]] | None,   # world space, exported meshes
      "objects": [{"name", "type", "polygons", "scale": [x, y, z], "materials": [names]}],
      "materials": [{"name", "users": [object names], "shader": "principled"|"diffuse"|"other"|"none",
                     "unsupported_nodes": [labels],
                     "textures": [{"image", "missing", "packed", "tiled", "missing_tiles": [ints]}]}],
      "world": {"env_texture": name | None, "env_missing": bool,
                "blender_intensity": [r, g, b] | None, "background_strength": float},
    }

Upper-case codes match Miris's ingest error keys (see findings.py).
"""

from .findings import ERROR, INFO, WARN, Finding, sort
from .bundle import MAX_VOLUME_M3, MIN_THICKNESS_M, MIN_VOLUME_M3

# From the Miris Blender guide: "Export Light Intensity" = "Blender Light Intensity" x 1562.
EXPORT_INTENSITY_FACTOR = 1562.0
# A single object this large is more likely a centimeter asset imported as meters.
SUSPICIOUS_SIZE_M = 500.0
GUIDE_URL = "https://docs.miris.com/preparing-assets/blender"


def run(snap):
    findings = []
    findings += _units(snap)
    findings += _geometry(snap)
    findings += _materials(snap)
    findings += _lighting(snap)
    return sort(findings)


def _units(snap):
    out = []
    system, scale, length = snap.get("unit_system"), snap.get("unit_scale", 1.0), snap.get("length_unit")
    if system != "METRIC" or abs(scale - 1.0) > 1e-6 or length not in (None, "METERS"):
        out.append(Finding(
            "units.not_meters", WARN, "Scene",
            f"Scene units are {system.title() if system else '?'}, scale {scale:g}, length "
            f"{(length or '?').title()}. Miris's Blender guide requires Metric, Unit Scale 1, Meters, "
            "and a wrong unit scale is the classic cause of UNSUPPORTED_SCENE_SIZE.",
            "Set Scene Properties > Units to Metric / 1.0 / Meters (one click below).",
            autofix="stream_ready.fix_units",
        ))
    return out


def _geometry(snap):
    out = []
    meshes = [o for o in snap.get("objects", []) if o.get("type") == "MESH"]
    if not any(o.get("polygons", 0) > 0 for o in meshes):
        out.append(Finding(
            "MISSING_VISIBLE_USD_PRIMS", ERROR, "Scene",
            "Nothing to stream: no visible mesh with faces in the export scope.",
            "Select the objects to publish (or switch scope to Visible) and make sure they have faces.",
        ))
        return out

    for obj in meshes:
        sx, sy, sz = obj.get("scale", (1, 1, 1))
        if sx * sy * sz < 0:
            out.append(Finding(
                "transform.negative_scale", WARN, obj["name"],
                f"{obj['name']} has negative scale, which mirrors the mesh and flips its winding. "
                "Streaming clients that cull back faces will draw it inside out.",
                "Apply scale (Ctrl+A > Scale), then recalculate normals outside (Shift+N).",
                obj["name"],
            ))
        if obj.get("polygons", 0) == 0:
            out.append(Finding(
                "mesh.empty", INFO, obj["name"],
                f"{obj['name']} has no faces and will not render.",
                "Delete it or add geometry.",
                obj["name"],
            ))

    bounds = snap.get("bounds")
    if bounds:
        size = [bounds[1][i] - bounds[0][i] for i in range(3)]
        volume = 1.0
        for s in size:
            volume *= max(s, MIN_THICKNESS_M)
        dims = " x ".join(f"{s:,.3g} m" for s in size)
        if volume < MIN_VOLUME_M3:
            out.append(Finding(
                "UNSUPPORTED_SCENE_SIZE", ERROR, "Scene",
                f"The export is {dims}, under Miris's 1 cm³ minimum. Usually a model built in "
                "centimeters or millimeters but exported as meters.",
                "Scale the objects to real-world meters and apply scale (Ctrl+A > Scale).",
            ))
        elif volume > MAX_VOLUME_M3:
            out.append(Finding(
                "UNSUPPORTED_SCENE_SIZE", ERROR, "Scene",
                f"The export is {dims}, over Miris's 100 km³ maximum.",
                "Look for stray far-away geometry or a unit mix-up, then rescale to meters.",
            ))
        elif max(size) > SUSPICIOUS_SIZE_M:
            out.append(Finding(
                "units.suspicious_size", WARN, "Scene",
                f"The export is {dims}. Within Miris's limits, but that size is usually a centimeter "
                "asset imported as meters (100x too big).",
                "Check the source units; if it came from FBX or Maya, re-import with the right scale.",
            ))
    return out


def _materials(snap):
    out = []
    for mat in snap.get("materials", []):
        name = mat["name"]
        owner = (mat.get("users") or [""])[0]
        if mat.get("shader") not in ("principled", "diffuse"):
            out.append(Finding(
                "material.unsupported_shader", WARN, name,
                f"{name} is not built on a Principled BSDF or Diffuse BSDF, the two shaders Blender "
                "converts to USD Preview Surface. It will stream as a flat default material.",
                "Rebuild it on a Principled BSDF, or bake its look to image textures.",
                owner,
            ))
        for label in sorted(set(mat.get("unsupported_nodes", []))):
            out.append(Finding(
                "material.procedural", WARN, name,
                f"{name} uses a {label} node. USD Preview Surface has no procedural textures, so "
                "that part of the look is dropped on export.",
                "Bake the procedural result to an image texture and plug that in instead.",
                owner,
            ))
        for tex in mat.get("textures", []):
            target = f"{name} / {tex['image']}"
            if tex.get("missing") and not tex.get("packed"):
                out.append(Finding(
                    "MISSING_REFERENCED_FILE", ERROR, target,
                    f"{tex['image']} points at a file that does not exist, so the export cannot copy it "
                    "into the upload.",
                    "Relink it (File > External Data > Find Missing Files) or pack it into the .blend.",
                    owner,
                ))
            elif tex.get("missing_tiles"):
                tiles = ", ".join(str(t) for t in tex["missing_tiles"][:6])
                out.append(Finding(
                    "MISSING_REFERENCED_FILE", ERROR, target,
                    f"{tex['image']} is a UDIM set with tiles missing on disk ({tiles}). Miris needs every tile.",
                    "Restore the missing tiles or remove them from the image's tile list.",
                    owner,
                ))
    return out


def _lighting(snap):
    world = snap.get("world") or {}
    env = world.get("env_texture")
    if not env:
        return [Finding(
            "lighting.no_hdri", INFO, "World",
            "The World has no Environment Texture, so the asset streams under Miris's default lighting.",
            f"Optional: load an HDRI as the World's Environment Texture ({GUIDE_URL}).",
        )]
    if world.get("env_missing"):
        return [Finding(
            "MISSING_REFERENCED_FILE", ERROR, f"World / {env}",
            f"The World HDRI {env} points at a file that does not exist.",
            "Relink the HDRI or pack it into the .blend.",
        )]
    intensity = world.get("blender_intensity")
    if intensity and max(intensity) - min(intensity) > 1e-6:
        return [Finding(
            "lighting.unequal_rgb", WARN, "World",
            f"Blender Light Intensity is {_vec(intensity)}. The Miris guide requires all three components "
            "to be equal; the add-on exports their average, so the stream will not carry this tint.",
            "Set all three components to the same value.",
        )]
    return []


def dome_light_color(world):
    """The DomeLight color to write into the exported USD.

    The Miris guide calibrates HDRI lighting as Blender intensity x 1562, carried in the
    DomeLight's color. Getting that through Blender's exporter depends on node wiring
    (in testing, Blender 4.2 wrote (1, 1, 1) when the export multiplier was not the node
    feeding the Background), so the add-on writes the value itself after export. The
    scene's own lighting is never touched. Source of the intensity, in order: a node labeled
    "Blender Light Intensity" (the guide's setup), else the Background node's Strength.
    """
    world = world or {}
    if not world.get("env_texture"):
        return None
    base = world.get("blender_intensity") or [world.get("background_strength", 1.0)] * 3
    level = sum(base) / 3 * EXPORT_INTENSITY_FACTOR  # equal components, per the guide
    return [level, level, level]


def _vec(v):
    return "(" + ", ".join(f"{c:g}" for c in v) + ")"
