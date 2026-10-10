# SPDX-License-Identifier: GPL-3.0-or-later
"""Check an export folder the way Miris's ingest will, before any bytes leave the machine.

Inside Blender the `pxr` module (OpenUSD's Python bindings) ships with the app, so the
check opens the real stage: it resolves every dependency, evaluates visibility and
purpose, and measures world-space bounds. Without pxr (plain Python, CI) it falls back
to reading .usda text, which covers the test fixtures and catches the common mistakes.
"""

import glob
import os
import re

from .findings import ERROR, INFO, WARN, Finding, sort

USD_EXTS = {".usd", ".usda", ".usdc", ".usdz"}
MIN_VOLUME_M3 = 1e-6  # 1 cm^3
MAX_VOLUME_M3 = 1e11  # 100 km^3
# Flat geometry (a ground plane, a decal) has zero volume. Measure it as if it were 1 mm
# thick rather than rejecting it. ASSUMPTION: Miris's own measurement is not documented.
MIN_THICKNESS_M = 0.001

try:
    from pxr import Sdf, Usd, UsdGeom, UsdUtils
    HAVE_PXR = True
except ImportError:  # outside Blender
    HAVE_PXR = False


def collect(folder):
    """Every file Miris will receive, as (posix path relative to folder, size). Skips dotfiles."""
    out = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, folder).replace(os.sep, "/")
            out.append((rel, os.path.getsize(full)))
    return out


def check(folder, use_pxr=True):
    files = collect(folder)
    report = {
        "folder": folder,
        "files": files,
        "total_bytes": sum(size for _, size in files),
        "root": None,
        "meters_per_unit": None,
        "size_m": None,
        "used_pxr": False,
        "findings": [],
    }
    roots = [rel for rel, _ in files if "/" not in rel and os.path.splitext(rel)[1].lower() in USD_EXTS]
    if not roots:
        report["findings"] = [Finding(
            "MISSING_USD_FILE", ERROR, os.path.basename(folder) or folder,
            "No .usd, .usda, .usdc, or .usdz file at the top of the upload folder.",
            "Publish the folder that contains the USD file, not a subfolder like textures/.",
        )]
        return report
    if len(roots) > 1:
        report["findings"] = [Finding(
            "MORE_THAN_ONE_ROOT_USD_FILE", ERROR, ", ".join(roots),
            f"{len(roots)} USD files at the folder root; Miris needs exactly one entry point.",
            "Keep the main file at the root and move sublayers or payloads into a subfolder, "
            "updating the references to match.",
        )]
        return report

    report["root"] = roots[0]
    root_path = os.path.join(folder, roots[0])
    if use_pxr and HAVE_PXR:
        report["used_pxr"] = True
        _inspect_stage(root_path, folder, report)
    elif root_path.lower().endswith(".usda"):
        _inspect_usda_text(root_path, folder, report)
    else:
        report["findings"].append(Finding(
            "bundle.shallow_check", INFO, roots[0],
            "Binary USD and OpenUSD's Python module is not available here, so only the folder "
            "layout was checked.",
            "Run the check inside Blender, which ships OpenUSD.",
        ))
    report["findings"] = sort(report["findings"])
    return report


# --- full check (pxr) -------------------------------------------------------------------

def _inspect_stage(root_path, folder, report):
    out = report["findings"]
    name = os.path.basename(root_path)
    stage = Usd.Stage.Open(root_path)
    if stage is None:
        out.append(Finding("UNDEFINED", ERROR, name, "OpenUSD could not open the file.",
                           "Re-export; if it persists, run usdchecker on the file."))
        return

    _, _, unresolved = UsdUtils.ComputeAllDependencies(Sdf.AssetPath(root_path))
    for path in sorted(set(unresolved)):
        out.append(_missing(path, name))

    for prim in stage.Traverse():
        for attr in prim.GetAttributes():
            if attr.GetTypeName() != Sdf.ValueTypeNames.Asset:
                continue
            value = attr.Get()
            if value and value.path:
                _check_asset_path(value.path, value.resolvedPath, folder, f"{prim.GetPath()}.{attr.GetName()}", out)

    if not stage.GetDefaultPrim():
        out.append(Finding(
            "usd.no_default_prim", WARN, name,
            "The stage has no defaultPrim, so anything that references this file has to guess what to load.",
            "Re-export from Blender (it sets /root as the defaultPrim).",
        ))

    mpu = UsdGeom.GetStageMetersPerUnit(stage)
    report["meters_per_unit"] = mpu
    visible = [p for p in stage.Traverse() if _renders(p)]
    if not visible:
        out.append(Finding(
            "MISSING_VISIBLE_USD_PRIMS", ERROR, name,
            "No mesh or other geometry that is visible and has a render or default purpose.",
            "Check object visibility (viewport and render) and that the export scope includes your meshes.",
        ))
        return

    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
                              useExtentsHint=True)
    box = cache.ComputeWorldBound(stage.GetPseudoRoot()).ComputeAlignedRange()
    if not box.IsEmpty():
        size = box.GetSize()
        _check_size([size[0] * mpu, size[1] * mpu, size[2] * mpu], mpu, name, report)


def _renders(prim):
    if not prim.IsA(UsdGeom.Gprim):
        return False
    imageable = UsdGeom.Imageable(prim)
    if imageable.ComputeVisibility() == UsdGeom.Tokens.invisible:
        return False
    return imageable.ComputePurpose() in (UsdGeom.Tokens.default_, UsdGeom.Tokens.render)


# --- fallback check (.usda text) ----------------------------------------------------------

ASSET_RE = re.compile(r"@([^@]+)@")
MPU_RE = re.compile(r"metersPerUnit\s*=\s*([0-9.eE+-]+)")
POINTS_RE = re.compile(r"point3f\[\]\s+points\s*=\s*\[(.*?)\]", re.S)
TRIPLE_RE = re.compile(r"\(\s*([-0-9.eE+]+)\s*,\s*([-0-9.eE+]+)\s*,\s*([-0-9.eE+]+)\s*\)")
INVISIBLE_RE = re.compile(r'token\s+visibility\s*=\s*"invisible"')


def _inspect_usda_text(root_path, folder, report):
    out = report["findings"]
    name = os.path.basename(root_path)
    with open(root_path, encoding="utf-8") as f:
        text = f.read()

    base = os.path.dirname(root_path)
    for path in sorted(set(ASSET_RE.findall(text))):
        resolved = path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))
        _check_asset_path(path, resolved if os.path.exists(resolved) else "", folder, name, out)

    mpu_match = MPU_RE.search(text)
    mpu = float(mpu_match.group(1)) if mpu_match else 0.01  # USD's fallback is centimeters
    report["meters_per_unit"] = mpu

    # Rough but useful: a Mesh that is not marked invisible, and the extent of all points.
    has_mesh = "def Mesh" in text and not INVISIBLE_RE.search(text)
    if not has_mesh:
        out.append(Finding(
            "MISSING_VISIBLE_USD_PRIMS", ERROR, name,
            "No visible Mesh prim found in the file.",
            "Check object visibility and the export scope.",
        ))
        return
    points = [tuple(map(float, t)) for block in POINTS_RE.findall(text) for t in TRIPLE_RE.findall(block)]
    if points:
        size = [(max(p[i] for p in points) - min(p[i] for p in points)) * mpu for i in range(3)]
        _check_size(size, mpu, name, report)
    out.append(Finding(
        "bundle.shallow_check", INFO, name,
        "Checked by reading the .usda text (OpenUSD's Python module is not available here), "
        "so transforms and payloads were not evaluated.",
        "Run the check inside Blender for the full stage evaluation.",
    ))


# --- shared rules -----------------------------------------------------------------------

def _check_asset_path(path, resolved, folder, where, out):
    if "<UDIM>" in path or "<udim>" in path:
        pattern = (resolved or os.path.join(folder, path)).replace("<UDIM>", "1[0-9][0-9][0-9]").replace("<udim>", "1[0-9][0-9][0-9]")
        if not glob.glob(pattern):
            out.append(_missing(path, where, "No UDIM tiles found for this texture."))
        return
    if os.path.isabs(path) or re.match(r"^[A-Za-z]:[\\/]", path):
        out.append(Finding(
            "MISSING_REFERENCED_FILE", ERROR, where,
            f"{path} is an absolute path. It points at your disk, which Miris cannot see.",
            "Export with Relative Paths on and textures copied next to the USD (the add-on's export does both).",
        ))
        return
    if not resolved:
        out.append(_missing(path, where))
        return
    inside = os.path.normcase(os.path.abspath(resolved)).startswith(os.path.normcase(os.path.abspath(folder)) + os.sep)
    if not inside:
        out.append(Finding(
            "MISSING_REFERENCED_FILE", ERROR, where,
            f"{path} resolves outside the upload folder, so it will not be uploaded.",
            "Copy the file into the export folder (textures/ is conventional) and reference it relatively.",
        ))


def _missing(path, where, message=None):
    return Finding(
        "MISSING_REFERENCED_FILE", ERROR, where,
        message or f"{path} is referenced but does not exist.",
        "Relink the file in Blender (File > External Data > Find Missing Files) and re-export.",
    )


def _check_size(size_m, mpu, name, report):
    report["size_m"] = [round(s, 6) for s in size_m]
    volume = 1.0
    for s in size_m:
        volume *= max(s, MIN_THICKNESS_M)
    dims = " x ".join(_fmt_len(s) for s in size_m)
    unit_hint = ""
    if abs(mpu - 1.0) > 1e-9:
        unit_hint = f" The file says 1 unit = {mpu:g} m."
    if volume < MIN_VOLUME_M3:
        report["findings"].append(Finding(
            "UNSUPPORTED_SCENE_SIZE", ERROR, name,
            f"The asset is {dims}, smaller than the 1 cm³ minimum. Usually a centimeter/meter mix-up.{unit_hint}",
            "Scale the geometry to real-world meters and apply scale (changing metersPerUnit alone does not fix it).",
        ))
    elif volume > MAX_VOLUME_M3:
        report["findings"].append(Finding(
            "UNSUPPORTED_SCENE_SIZE", ERROR, name,
            f"The asset is {dims}, larger than the 100 km³ maximum.{unit_hint}",
            "Check for an inverted unit scale or stray far-away geometry, then rescale to meters.",
        ))


def _fmt_len(m):
    if m >= 1000:
        return f"{m / 1000:,.2f} km"
    if m >= 1:
        return f"{m:,.2f} m"
    if m >= 0.01:
        return f"{m * 100:.1f} cm"
    return f"{m * 1000:.2f} mm"
