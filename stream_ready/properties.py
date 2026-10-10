# SPDX-License-Identifier: GPL-3.0-or-later
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty

from .core import keystore
from .core.miris import DEFAULT_BASE_URL, DEFAULT_INPUT_TYPE

_cached = None  # (key, source); panels redraw constantly, the vault shouldn't be hit each time


def api_key():
    """(key, where it came from): $MIRIS_API_KEY first, then the OS credential vault.
    The key is never stored in Blender preferences or the .blend file."""
    global _cached
    if _cached is None:
        _cached = keystore.resolve()
    return _cached


def forget_cached_key():
    global _cached
    _cached = None


class StreamReadyPreferences(bpy.types.AddonPreferences):
    # __package__ is "stream_ready" as a legacy add-on and "bl_ext.<repo>.stream_ready" as an extension.
    bl_idname = __package__

    api_url: StringProperty(
        name="API URL",
        description="Miris API base URL. Point it at mock/mock_miris.py to try the add-on without an account",
        default=DEFAULT_BASE_URL,
    )
    # No API key property on purpose: see core/keystore.py and STREAM_READY_OT_set_api_key.
    workspace: StringProperty(
        name="Workspace",
        description="Optional workspace (customer UUID). Empty uses the key's default workspace",
    )
    input_type: StringProperty(
        name="Input type",
        description="input_type sent with each upload (advanced)",
        default=DEFAULT_INPUT_TYPE,
    )
    streamable: BoolProperty(
        name="Skip preview",
        description="Process straight to streamable instead of a preview first. "
                    "Only works if Miris has enabled it on your account",
        default=False,
    )
    poll_timeout: FloatProperty(
        name="Wait for processing (s)",
        description="How long to watch processing before handing off to the Miris portal",
        default=600.0, min=0.0, max=3600.0,
    )

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "api_url")
        _, source = api_key()
        box = col.box()
        if source:
            box.label(text=f"API key stored in {source}", icon="LOCKED")
        else:
            vault = keystore.backend_name()
            box.label(text=f"No API key. Saved keys go to {vault}" if vault
                      else "No API key, and no credential vault found: set $MIRIS_API_KEY", icon="UNLOCKED")
        row = box.row(align=True)
        row.operator("stream_ready.set_api_key", icon="KEY_HLT")
        sub = row.row(align=True)
        sub.enabled = source not in ("", "$MIRIS_API_KEY")
        sub.operator("stream_ready.clear_api_key", icon="TRASH")
        row.operator("stream_ready.test_connection", icon="URL")
        col.prop(self, "workspace")
        col.separator()
        col.prop(self, "streamable")
        col.prop(self, "poll_timeout")
        col.prop(self, "input_type")


class StreamReadySceneSettings(bpy.types.PropertyGroup):
    asset_name: StringProperty(
        name="Asset name",
        description="Name shown in the Miris portal. Defaults to the .blend file name",
    )
    tags: StringProperty(
        name="Tags",
        description="Comma-separated tags, e.g. chair, oak, v2",
    )
    scope: EnumProperty(
        name="Export",
        items=[
            ("SELECTED", "Selected", "Export only the selected objects"),
            ("VISIBLE", "Visible", "Export every visible object in the scene"),
        ],
        default="VISIBLE",
    )
    create_viewer_key: BoolProperty(
        name="Create share key",
        description="Create a viewer key scoped to just this asset and copy an embed snippet",
        default=True,
    )
    export_dir: StringProperty(
        name="Export folder",
        description="Where Export Only writes the Miris-ready USD folder",
        subtype="DIR_PATH",
        default="//miris_export/",
    )
