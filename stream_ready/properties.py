# SPDX-License-Identifier: GPL-3.0-or-later
import os

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty

from .core.miris import DEFAULT_BASE_URL, DEFAULT_INPUT_TYPE

API_KEY_ENV = "MIRIS_API_KEY"


def api_key(prefs):
    """(key, where it came from). The environment wins, so a studio can inject the key
    from a secrets manager and never type it into Blender at all."""
    env = os.environ.get(API_KEY_ENV, "").strip()
    if env:
        return env, f"${API_KEY_ENV}"
    if prefs.api_key.strip():
        return prefs.api_key.strip(), "preferences"
    return "", ""


class StreamReadyPreferences(bpy.types.AddonPreferences):
    # __package__ is "stream_ready" as a legacy add-on and "bl_ext.<repo>.stream_ready" as an extension.
    bl_idname = __package__

    api_url: StringProperty(
        name="API URL",
        description="Miris API base URL. Point it at mock/mock_miris.py to try the add-on without an account",
        default=DEFAULT_BASE_URL,
    )
    # User preferences, not scene data: the key never travels inside a .blend someone
    # sends to a client. Setting MIRIS_API_KEY in the environment is safer still.
    api_key: StringProperty(
        name="API key",
        description=f"Miris API key (Portal > API keys). ${API_KEY_ENV} overrides this. "
                    "Stored in Blender preferences, never in the .blend file",
        subtype="PASSWORD",
    )
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
        _, source = api_key(self)
        row = col.row()
        row.enabled = source != f"${API_KEY_ENV}"
        row.prop(self, "api_key")
        if source == f"${API_KEY_ENV}":
            col.label(text=f"Using the key from ${API_KEY_ENV}", icon="LOCKED")
        col.prop(self, "workspace")
        col.operator("stream_ready.test_connection", icon="URL")
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
