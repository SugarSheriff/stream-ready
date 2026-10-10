# SPDX-License-Identifier: GPL-3.0-or-later
"""Turn the live Blender scene into the plain-dict snapshot core.preflight expects.

This is the only module that reads scene data for preflight, which keeps the rules
themselves testable without Blender.
"""

import os

import bpy
from mathutils import Vector

# Nodes that render in Blender but have no USD Preview Surface equivalent, so they
# silently vanish on export.
PROCEDURAL_NODES = {
    "TEX_NOISE": "Noise Texture",
    "TEX_VORONOI": "Voronoi Texture",
    "TEX_WAVE": "Wave Texture",
    "TEX_MUSGRAVE": "Musgrave Texture",
    "TEX_GRADIENT": "Gradient Texture",
    "TEX_MAGIC": "Magic Texture",
    "TEX_BRICK": "Brick Texture",
    "TEX_CHECKER": "Checker Texture",
    "TEX_WHITE_NOISE": "White Noise Texture",
    "TEX_GABOR": "Gabor Texture",
}
# Node label from the Miris Blender guide's World setup, matched case-insensitively.
BLENDER_INTENSITY_LABEL = "blender light intensity"


def scope_objects(context, scope):
    if scope == "SELECTED":
        return [o for o in context.selected_objects if o.visible_get()]
    return [o for o in context.scene.objects if o.visible_get()]


def capture(context, scope):
    scene = context.scene
    units = scene.unit_settings
    depsgraph = context.evaluated_depsgraph_get()
    entries, material_users = [], {}
    lo, hi = Vector((float("inf"),) * 3), Vector((float("-inf"),) * 3)

    for obj in scope_objects(context, scope):
        entry = {"name": obj.name, "type": obj.type, "scale": list(obj.scale), "polygons": 0, "materials": []}
        if obj.type == "MESH":
            # Evaluate with modifiers: the export applies them, so measure what ships.
            evaluated = obj.evaluated_get(depsgraph)
            mesh = evaluated.to_mesh()
            try:
                entry["polygons"] = len(mesh.polygons)
            finally:
                evaluated.to_mesh_clear()
            if entry["polygons"]:
                # 8 transformed box corners: cheap even for million-vertex meshes.
                for corner in evaluated.bound_box:
                    co = evaluated.matrix_world @ Vector(corner)
                    lo = Vector(map(min, lo, co))
                    hi = Vector(map(max, hi, co))
            for slot in obj.material_slots:
                if slot.material:
                    entry["materials"].append(slot.material.name)
                    material_users.setdefault(slot.material, []).append(obj.name)
        entries.append(entry)

    bounds = None
    if lo.x != float("inf"):
        s = units.scale_length  # Blender units -> meters
        bounds = [[c * s for c in lo], [c * s for c in hi]]

    return {
        "unit_system": units.system,
        "unit_scale": units.scale_length,
        "length_unit": units.length_unit,
        "bounds": bounds,
        "objects": entries,
        "materials": [_material(mat, users) for mat, users in material_users.items()],
        "world": _world(scene.world),
    }


def _material(mat, users):
    info = {"name": mat.name, "users": users, "shader": "none", "unsupported_nodes": [], "textures": []}
    # Blender 5.0 made every material node-based and deprecated use_nodes.
    uses_nodes = mat.use_nodes if bpy.app.version < (5, 0, 0) else True
    if not uses_nodes or not mat.node_tree:
        return info
    nodes = reachable_nodes(mat.node_tree, "OUTPUT_MATERIAL")
    types = {n.type for n in nodes}
    info["shader"] = ("principled" if "BSDF_PRINCIPLED" in types
                      else "diffuse" if "BSDF_DIFFUSE" in types
                      else "other")
    for node in nodes:
        if node.type in PROCEDURAL_NODES:
            info["unsupported_nodes"].append(PROCEDURAL_NODES[node.type])
        elif node.type == "TEX_IMAGE" and node.image:
            info["textures"].append(_texture(node.image))
    return info


def _texture(image):
    packed = bool(image.packed_file) or bool(getattr(image, "packed_files", None))
    out = {"image": image.name, "packed": packed, "missing": False, "tiled": image.source == "TILED",
           "missing_tiles": []}
    if packed or image.source not in {"FILE", "SEQUENCE", "TILED"}:
        return out
    path = bpy.path.abspath(image.filepath, library=image.library)
    if image.source == "TILED":
        for tile in image.tiles:
            tile_path = path.replace("<UDIM>", str(tile.number)) if "<UDIM>" in path else path
            if not os.path.exists(tile_path):
                out["missing_tiles"].append(tile.number)
    else:
        out["missing"] = not os.path.exists(path)
    return out


def _world(world):
    info = {"env_texture": None, "env_missing": False, "blender_intensity": None, "background_strength": 1.0}
    if not world or not world.node_tree:
        return info
    tree = world.node_tree
    for node in reachable_nodes(tree, "OUTPUT_WORLD"):
        if node.type == "TEX_ENVIRONMENT" and node.image:
            info["env_texture"] = node.image.name
            info["env_missing"] = _texture(node.image)["missing"]
        elif node.type == "BACKGROUND" and not node.inputs["Strength"].is_linked:
            info["background_strength"] = float(node.inputs["Strength"].default_value)
    socket = intensity_socket(tree, BLENDER_INTENSITY_LABEL)
    if socket is not None:
        info["blender_intensity"] = socket_rgb(socket)
    return info


def reachable_nodes(tree, output_type):
    """Nodes that actually contribute to the active output.

    Walking upstream from the output (instead of scanning tree.nodes) keeps stray,
    disconnected nodes from producing false alarms.
    """
    outputs = [n for n in tree.nodes if n.type == output_type]
    output = next((n for n in outputs if getattr(n, "is_active_output", False)), outputs[0] if outputs else None)
    if output is None:
        return []
    seen, stack = {}, [output]
    while stack:
        node = stack.pop()
        if node.name in seen:
            continue
        seen[node.name] = node
        if node.type == "GROUP" and node.node_tree:
            stack.extend(n for n in node.node_tree.nodes if n.type not in {"GROUP_INPUT", "GROUP_OUTPUT"})
        for socket in node.inputs:
            stack.extend(link.from_node for link in socket.links if not link.is_muted)
    return list(seen.values())


def intensity_socket(tree, label):
    """The constant input of the node labeled per the Miris guide: the vector/color the
    artist types the intensity into. Works for Vector Math, Mix, and Math nodes."""
    node = next((n for n in tree.nodes if (n.label or "").strip().lower() == label), None)
    if node is None:
        return None
    for socket in node.inputs:
        if socket.is_linked or not socket.enabled or socket.name in {"Fac", "Factor"}:
            continue
        if socket.type in {"VECTOR", "RGBA", "VALUE"}:
            return socket
    return None


def socket_rgb(socket):
    value = socket.default_value
    try:
        return [float(value[0]), float(value[1]), float(value[2])]
    except TypeError:
        return [float(value)] * 3
