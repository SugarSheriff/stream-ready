# SPDX-License-Identifier: GPL-3.0-or-later
"""USD export with the settings from the Miris Blender guide, then a post-export fix-up.

The USD exporter's options have been renamed between Blender releases (for example
export_textures became export_textures_mode in 4.4), so the call is built from what the
running Blender's operator actually accepts instead of a hard-coded keyword list.
"""

import os
import re

import bpy

try:
    from pxr import Gf, Usd, UsdGeom, UsdLux
except ImportError:  # every supported Blender ships pxr; this only guards odd builds
    Gf = Usd = UsdGeom = UsdLux = None


def safe_name(name):
    return re.sub(r"[^\w.-]+", "_", name).strip("._") or "asset"


def export_usd(context, folder, name, scope, dome_color=None):
    """Export to <folder>/<name>.usd with textures copied into <folder>/textures/."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, safe_name(name) + ".usd")  # the guide asks for .usd, not .usdc
    props = {p.identifier: p for p in bpy.ops.wm.usd_export.get_rna_type().properties}

    wanted = {
        "filepath": path,
        "check_existing": False,
        "selected_objects_only": scope == "SELECTED",
        "visible_objects_only": True,
        "export_animation": False,
        "evaluation_mode": "RENDER",
        "export_materials": True,
        "generate_preview_surface": True,  # "USD Preview Surface recommended"
        "relative_paths": True,  # avoids MISSING_REFERENCED_FILE from absolute paths
        "convert_world_material": True,  # required: the World becomes a DomeLight
        "export_cameras": False,  # a streamed asset brings no camera
        # Copy textures into textures/ next to the USD ("Export Textures: New path").
        "export_textures": True,  # Blender <= 4.3
        "export_textures_mode": "NEW",  # Blender 4.4+
        "overwrite_textures": True,
        # Newer Blenders expose unit conversion on export (used only where it exists);
        # fix_stage() guarantees the result either way.
        "convert_scene_units": "METERS",
    }
    kwargs = {k: v for k, v in wanted.items() if _accepts(props.get(k), v)}
    if "export_textures_mode" in kwargs:
        kwargs.pop("export_textures", None)

    result = bpy.ops.wm.usd_export(**kwargs)
    if "FINISHED" not in result or not os.path.exists(path):
        raise RuntimeError("USD export failed; see the system console for the exporter's message")
    notes = fix_stage(path, context.scene.unit_settings.scale_length, dome_color)
    return path, notes


def _accepts(prop, value):
    if prop is None:
        return False
    if prop.type == "ENUM":  # an enum value this Blender doesn't know would raise TypeError
        return value in {item.identifier for item in prop.enum_items}
    return True


def fix_stage(path, scale_length, dome_color=None):
    """Post-export fixes the exporter can't be talked into. Returns notes for the log.

    * metersPerUnit: Blender 4.2 authors it, but if an exporter ever leaves it unset,
      USD's fallback is 0.01 (centimeters). A correctly built 1 m chair would then read
      as 1 cm, which is exactly the unit mix-up behind Miris's UNSUPPORTED_SCENE_SIZE.
      Cheap to guarantee, so it is guaranteed.
    * DomeLight color: the Miris-calibrated HDRI intensity (see preflight.dome_light_color).
    """
    notes = []
    if Usd is None:
        return notes
    stage = Usd.Stage.Open(path)
    if not stage.HasAuthoredMetadata(UsdGeom.Tokens.metersPerUnit):
        UsdGeom.SetStageMetersPerUnit(stage, scale_length)
        notes.append(f"authored metersPerUnit = {scale_length:g} (the exporter left it unset; USD would assume centimeters)")
    if not stage.GetDefaultPrim():
        top = list(stage.GetPseudoRoot().GetChildren())
        if len(top) == 1:
            stage.SetDefaultPrim(top[0])
            notes.append(f"set defaultPrim to {top[0].GetPath()}")
    if dome_color:
        for prim in stage.Traverse():
            if prim.IsA(UsdLux.DomeLight):
                UsdLux.DomeLight(prim).CreateColorAttr().Set(Gf.Vec3f(*dome_color))
                notes.append(f"DomeLight color = {dome_color[0]:g} (Blender intensity x 1562, per the Miris guide)")
    if notes:
        stage.GetRootLayer().Save()
    return notes
