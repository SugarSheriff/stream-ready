# SPDX-License-Identifier: GPL-3.0-or-later
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty

from . import state
from .core import keystore, oauth
from .core.miris import DEFAULT_BASE_URL, DEFAULT_INPUT_TYPE

_cached = {}  # api_url -> (kind, value, label); panels redraw constantly, the vault shouldn't be hit each time


def credential(prefs):
    """("oauth", tokens, label), ("api_key", key, label), or ("", None, ""), for the
    configured API URL. Nothing here is ever stored in Blender preferences or a .blend."""
    if prefs.api_url not in _cached:
        _cached[prefs.api_url] = oauth.credential(prefs.api_url)
    return _cached[prefs.api_url]


def forget_cached_credential():
    _cached.clear()


def draw_account(box, prefs):
    """Sign-in status and buttons; shared by the preferences and the sidebar panel."""
    signin = state.signin
    if signin.status == "waiting":
        box.label(text=f"Approve code {signin.user_code} in your browser…", icon="SORTTIME")
        row = box.row(align=True)
        row.operator("stream_ready.open_signin_page", icon="URL")
        row.operator("stream_ready.cancel_sign_in", icon="X")
        return
    kind, _, label = credential(prefs)
    if kind:
        box.label(text=label + (f" · {signin.workspace}" if signin.workspace else ""), icon="LOCKED")
        row = box.row(align=True)
        row.operator("stream_ready.test_connection", icon="URL")
        if kind == "oauth":
            row.operator("stream_ready.sign_out", icon="X")
    else:
        if signin.message:
            box.label(text=signin.message, icon="ERROR")
        if keystore.backend_name():
            box.operator("stream_ready.sign_in", icon="USER")
        else:
            box.label(text="No credential vault on this system: set $MIRIS_API_KEY", icon="UNLOCKED")


class StreamReadyPreferences(bpy.types.AddonPreferences):
    # __package__ is "stream_ready" as a legacy add-on and "bl_ext.<repo>.stream_ready" as an extension.
    bl_idname = __package__

    api_url: StringProperty(
        name="API URL",
        description="Miris API base URL. Point it at mock/mock_miris.py to try the add-on without an account",
        default=DEFAULT_BASE_URL,
    )
    # No API key or token property on purpose: Blender saves every preference as plain
    # text in userpref.blend. Credentials live in the OS vault (core/keystore.py).
    workspace: StringProperty(
        name="Workspace",
        description="Optional workspace (customer UUID). Empty uses the one you signed in to",
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
        draw_account(col.box(), self)
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
