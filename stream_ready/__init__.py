# SPDX-License-Identifier: GPL-3.0-or-later
"""Stream Ready: preflight a Blender scene for Miris, export OpenUSD, and publish it.

Unofficial; not affiliated with or endorsed by Miris. Blender 4.2+ loads this as an
extension (blender_manifest.toml). bl_info below is only read when it is installed as a
legacy add-on.
"""

bl_info = {
    "name": "Stream Ready (for Miris)",
    "author": "Liam Hulsey",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > Miris",
    "description": "Preflight a scene, export OpenUSD, and publish it to Miris for 3D streaming",
    "category": "Import-Export",
}

import bpy

from . import operators, panels, properties

_classes = (
    properties.StreamReadyPreferences,
    properties.StreamReadySceneSettings,
    operators.STREAM_READY_OT_preflight,
    operators.STREAM_READY_OT_export,
    operators.STREAM_READY_OT_publish,
    operators.STREAM_READY_OT_cancel,
    operators.STREAM_READY_OT_test_connection,
    operators.STREAM_READY_OT_sign_in,
    operators.STREAM_READY_OT_open_signin_page,
    operators.STREAM_READY_OT_cancel_sign_in,
    operators.STREAM_READY_OT_sign_out,
    operators.STREAM_READY_OT_fix_units,
    operators.STREAM_READY_OT_select_object,
    operators.STREAM_READY_OT_open_docs,
    operators.STREAM_READY_OT_copy,
    panels.STREAM_READY_PT_main,
    panels.STREAM_READY_PT_preflight,
    panels.STREAM_READY_PT_publish,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.stream_ready = bpy.props.PointerProperty(type=properties.StreamReadySceneSettings)


def unregister():
    operators.shutdown()
    del bpy.types.Scene.stream_ready
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
