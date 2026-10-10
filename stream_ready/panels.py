# SPDX-License-Identifier: GPL-3.0-or-later
import textwrap

import bpy

from . import state
from .core.findings import ERROR, WARN
from .properties import api_key

SEVERITY_ICON = {ERROR: "CANCEL", WARN: "ERROR", "info": "INFO"}
STATUS_ICON = {"ready": "CHECKMARK", "preview": "HIDE_OFF", "processing": "SORTTIME", "rejected": "CANCEL",
               "failed": "CANCEL", "cancelled": "X", "running": "SORTTIME"}
STATUS_TEXT = {"ready": "Streamable on Miris", "preview": "Preview ready: generate streamable in the portal",
               "processing": "Uploaded; still processing on Miris",
               "rejected": "Rejected by Miris", "failed": "Failed", "cancelled": "Cancelled", "running": "Publishing"}


def _wrapped(layout, context, text):
    # Panel labels do not wrap, so estimate a width from the sidebar size.
    width = max(24, int(context.region.width / 7.5))
    for line in textwrap.wrap(text, width):
        layout.label(text=line)


def _finding_box(layout, context, finding):
    box = layout.box()
    row = box.row(align=True)
    row.label(text=f"{finding.code}  ·  {finding.target}", icon=SEVERITY_ICON[finding.severity])
    if finding.object:
        row.operator("stream_ready.select_object", text="", icon="RESTRICT_SELECT_OFF").name = finding.object
    row.operator("stream_ready.open_docs", text="", icon="HELP").code = finding.code
    col = box.column(align=True)
    col.scale_y = 0.8
    _wrapped(col, context, finding.message)
    _wrapped(col, context, "Fix: " + finding.fix)
    if finding.autofix:
        box.operator(finding.autofix, icon="TOOL_SETTINGS")


class _MirisPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Miris"


class STREAM_READY_PT_main(_MirisPanel, bpy.types.Panel):
    bl_label = "Stream Ready"

    def draw(self, context):
        settings = context.scene.stream_ready
        prefs = context.preferences.addons[__package__].preferences
        layout = self.layout
        layout.prop(settings, "asset_name")
        layout.prop(settings, "tags")
        layout.row().prop(settings, "scope", expand=True)
        _, source = api_key(prefs)
        row = layout.row()
        row.enabled = False
        row.label(text=f"API key from {source}" if source else "No API key: Export Only",
                  icon="LOCKED" if source else "UNLOCKED")


class STREAM_READY_PT_preflight(_MirisPanel, bpy.types.Panel):
    bl_label = "Preflight"
    bl_parent_id = "STREAM_READY_PT_main"

    def draw(self, context):
        layout = self.layout
        layout.operator("stream_ready.preflight", icon="CHECKMARK")
        if not state.preflight_ran:
            layout.label(text="Not run yet")
            return
        if not state.findings:
            layout.label(text="Ready for Miris", icon="CHECKMARK")
            return
        for finding in state.findings:
            _finding_box(layout, context, finding)


class STREAM_READY_PT_publish(_MirisPanel, bpy.types.Panel):
    bl_label = "Publish"
    bl_parent_id = "STREAM_READY_PT_main"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.stream_ready
        publish = state.publish

        if publish.running:
            layout.operator("stream_ready.cancel", icon="X")
            if publish.total_bytes and hasattr(layout, "progress"):  # layout.progress is 4.0+
                layout.progress(factor=publish.done_bytes / publish.total_bytes, type="BAR",
                                text=f"{publish.done_bytes / 1e6:.1f} / {publish.total_bytes / 1e6:.1f} MB")
        else:
            layout.prop(settings, "create_viewer_key")
            layout.operator("stream_ready.publish", icon="EXPORT")
            row = layout.row(align=True)
            row.prop(settings, "export_dir", text="")
            row.operator("stream_ready.export", text="Export Only", icon="FILE_FOLDER")

        if publish.status != "idle":
            layout.label(text=STATUS_TEXT.get(publish.status, publish.status),
                         icon=STATUS_ICON.get(publish.status, "NONE"))
        result = publish.result or {}
        if result.get("asset_id"):
            row = layout.row(align=True)
            row.label(text=result["asset_id"], icon="PACKAGE")
            row.operator("stream_ready.copy", text="", icon="COPYDOWN").what = "asset_id"
            if result.get("viewer_key"):
                layout.operator("stream_ready.copy", text="Copy Embed Snippet", icon="COPYDOWN").what = "embed"
        for finding in result.get("findings") or []:
            _finding_box(layout, context, finding)

        lines = publish.lines()
        if lines:
            col = layout.box().column(align=True)
            col.scale_y = 0.75
            for line in lines[-10:]:
                _wrapped(col, context, line)
