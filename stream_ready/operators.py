# SPDX-License-Identifier: GPL-3.0-or-later
import os
import shutil
import tempfile
import threading

import bpy
from bpy.props import StringProperty

from . import export, snapshot, state
from .core import bundle, keystore, preflight
from .core.findings import ERROR, has_errors, summarize
from .core.http import Cancelled, HttpError, Transport
from .core.miris import MirisClient, MirisError
from .core.publish import publish
from .properties import api_key, forget_cached_key

ERROR_REFERENCE = "https://docs.miris.com/preparing-assets/error-reference"
DOCS_URL = "https://github.com/SugarSheriff/stream-ready/blob/main/TROUBLESHOOTING.md"
EMBED = """<!-- npm i @miris-inc/components -->
<miris-scene viewer-key="{key}">
  <miris-stream uuid="{asset}"></miris-stream>
</miris-scene>"""


def _prefs(context):
    return context.preferences.addons[__package__].preferences


def _run_preflight(context):
    snap = snapshot.capture(context, context.scene.stream_ready.scope)
    findings = preflight.run(snap)
    state.findings[:] = findings
    state.preflight_ran = True
    return snap, findings


def _asset_name(context):
    name = context.scene.stream_ready.asset_name.strip()
    return name or bpy.path.display_name_from_filepath(bpy.data.filepath) or context.scene.name


def _export_checked(op, context, folder, snap):
    """Export, then check the folder as Miris will. Returns the bundle report or None."""
    try:
        _, notes = export.export_usd(context, folder, _asset_name(context), context.scene.stream_ready.scope,
                                     dome_color=preflight.dome_light_color(snap["world"]))
    except RuntimeError as exc:
        op.report({"ERROR"}, str(exc))
        return None
    report = bundle.check(folder)
    report["notes"] = notes
    # Show output problems next to the scene's: an artist should not have to care which
    # side of the export a problem was found on.
    state.findings[:] = [f for f in state.findings if f.severity != ERROR] + report["findings"]
    if has_errors(report["findings"]):
        op.report({"ERROR"}, f"Exported USD would be rejected: {summarize(report['findings'])}. "
                             f"Export kept at {folder}")
        return None
    return report


class STREAM_READY_OT_preflight(bpy.types.Operator):
    bl_idname = "stream_ready.preflight"
    bl_label = "Run Preflight"
    bl_description = "Check the scene against the Miris Blender guide and Miris's ingest errors"

    def execute(self, context):
        _, findings = _run_preflight(context)
        self.report({"WARNING"} if findings else {"INFO"}, summarize(findings))
        return {"FINISHED"}


class STREAM_READY_OT_export(bpy.types.Operator):
    bl_idname = "stream_ready.export"
    bl_label = "Export Only"
    bl_description = "Write a Miris-ready USD folder to upload yourself (no API key needed)"

    def execute(self, context):
        if context.mode != "OBJECT":
            self.report({"ERROR"}, "Switch to Object Mode before exporting")
            return {"CANCELLED"}
        snap, findings = _run_preflight(context)
        if has_errors(findings):
            self.report({"ERROR"}, "Fix the preflight errors first")
            return {"CANCELLED"}
        folder = os.path.join(bpy.path.abspath(context.scene.stream_ready.export_dir),
                              export.safe_name(_asset_name(context)))
        shutil.rmtree(folder, ignore_errors=True)  # stale textures from a previous export would ride along
        report = _export_checked(self, context, folder, snap)
        if report is None:
            return {"CANCELLED"}
        self.report({"INFO"}, f"Miris-ready: {len(report['files'])} file(s) in {folder}")
        return {"FINISHED"}


class STREAM_READY_OT_publish(bpy.types.Operator):
    bl_idname = "stream_ready.publish"
    bl_label = "Publish to Miris"
    bl_description = "Preflight, export USD, check it, upload it, and watch it process"

    @classmethod
    def poll(cls, context):
        return not state.publish.running

    def execute(self, context):
        prefs = _prefs(context)
        # Blender 4.2+ lets users disable network access globally; extensions must respect it.
        if not getattr(bpy.app, "online_access", True):
            self.report({"ERROR"}, "Online access is off (Preferences > System > Network)")
            return {"CANCELLED"}
        key, _ = api_key()
        if not key:
            self.report({"ERROR"}, "No API key: Set API Key in the add-on preferences (or use Export Only)")
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            # Edit-mode changes are not flushed to mesh data until you leave edit mode.
            self.report({"ERROR"}, "Switch to Object Mode before publishing")
            return {"CANCELLED"}
        snap, findings = _run_preflight(context)
        if has_errors(findings):
            self.report({"ERROR"}, f"{summarize(findings)}. Fix errors before publishing")
            return {"CANCELLED"}

        name = _asset_name(context)
        folder = os.path.join(tempfile.mkdtemp(prefix="stream_ready_"), export.safe_name(name))
        report = _export_checked(self, context, folder, snap)
        if report is None:
            return {"CANCELLED"}

        settings = context.scene.stream_ready
        tags = [t.strip() for t in settings.tags.split(",") if t.strip()]
        state.publish.start()
        for note in report["notes"]:
            state.publish.log(note)
        transport = Transport(log=state.publish.log, cancel=state.publish.cancel)
        client = MirisClient(transport, key, prefs.api_url, prefs.workspace.strip())
        kwargs = dict(name=name, root=report["root"], tags=tags, input_type=prefs.input_type,
                      streamable=prefs.streamable,
                      create_viewer_key=settings.create_viewer_key, log=state.publish.log,
                      progress=state.publish.progress, poll_timeout=prefs.poll_timeout)
        threading.Thread(target=_worker, args=(client, folder, report["files"], kwargs),
                         name="stream-ready-publish", daemon=True).start()
        bpy.app.timers.register(_redraw_while_running, first_interval=0.2)
        return {"FINISHED"}


class STREAM_READY_OT_cancel(bpy.types.Operator):
    bl_idname = "stream_ready.cancel"
    bl_label = "Cancel"
    bl_description = "Stop after the current request and mark the upload canceled on Miris"

    def execute(self, context):
        state.publish.cancel.set()
        return {"FINISHED"}


class STREAM_READY_OT_test_connection(bpy.types.Operator):
    bl_idname = "stream_ready.test_connection"
    bl_label = "Test Connection"
    bl_description = "Check the API URL and key with one authenticated request"

    def execute(self, context):
        prefs = _prefs(context)
        key, source = api_key()
        if not key:
            self.report({"ERROR"}, "No API key set")
            return {"CANCELLED"}
        client = MirisClient(Transport(timeout=10, max_attempts=1), key, prefs.api_url, prefs.workspace.strip())
        try:
            info = client.customer_info()
        except (MirisError, HttpError) as exc:
            self.report({"ERROR"}, f"Connection failed: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Connected to {info.get('name') or 'Miris'} (key from {source})")
        return {"FINISHED"}


class STREAM_READY_OT_set_api_key(bpy.types.Operator):
    bl_idname = "stream_ready.set_api_key"
    bl_label = "Set API Key"
    bl_description = "Save a Miris API key in your operating system's credential vault (never in Blender files)"
    # No REGISTER: registered operators are echoed, arguments and all, into Blender's Info
    # log and Python console history. INTERNAL also keeps it out of the F3 search.
    bl_options = {"INTERNAL"}

    # SKIP_SAVE: Blender would otherwise remember the last value typed into this dialog.
    key: StringProperty(name="API key", subtype="PASSWORD", options={"SKIP_SAVE"})

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "key")
        layout.label(text=f"Saved to {keystore.backend_name() or 'nowhere: no vault found'}.", icon="LOCKED")
        layout.label(text="Create keys in the Miris portal. Revoke there if one ever leaks.")

    def execute(self, context):
        try:
            keystore.save(self.key)
        except keystore.KeystoreError as exc:
            self.report({"ERROR"}, f"Could not save the key: {exc}")
            return {"CANCELLED"}
        finally:
            self.key = ""
        forget_cached_key()
        self.report({"INFO"}, f"API key saved to {keystore.backend_name()}")
        return {"FINISHED"}


class STREAM_READY_OT_clear_api_key(bpy.types.Operator):
    bl_idname = "stream_ready.clear_api_key"
    bl_label = "Remove"
    bl_description = "Delete the saved API key from the credential vault"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        keystore.delete()
        forget_cached_key()
        self.report({"INFO"}, "API key removed")
        return {"FINISHED"}


class STREAM_READY_OT_fix_units(bpy.types.Operator):
    bl_idname = "stream_ready.fix_units"
    bl_label = "Set Metric / 1.0 / Meters"
    bl_description = "Set scene units the way the Miris Blender guide asks for"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        units = context.scene.unit_settings
        old = units.scale_length
        units.system, units.scale_length, units.length_unit = "METRIC", 1.0, "METERS"
        _run_preflight(context)
        if abs(old - 1.0) > 1e-6:
            self.report({"WARNING"}, f"Unit scale was {old:g}: check that objects are still real-world size")
        return {"FINISHED"}


class STREAM_READY_OT_select_object(bpy.types.Operator):
    bl_idname = "stream_ready.select_object"
    bl_label = "Select"
    bl_description = "Select the object this finding is about"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty()

    def execute(self, context):
        obj = context.scene.objects.get(self.name)
        if obj is None:
            self.report({"WARNING"}, f"{self.name} is no longer in the scene")
            return {"CANCELLED"}
        for other in context.selected_objects:
            other.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        return {"FINISHED"}


class STREAM_READY_OT_open_docs(bpy.types.Operator):
    bl_idname = "stream_ready.open_docs"
    bl_label = "Explain"
    bl_description = "Open the documentation for this finding"

    code: StringProperty()

    def execute(self, context):
        if self.code.isupper():  # Miris's own error key: send people to Miris's own docs
            url = ERROR_REFERENCE
        else:  # GitHub heading anchors drop the dots: "lighting.no_hdri" -> "#lightingno_hdri"
            url = f"{DOCS_URL}#{self.code.replace('.', '')}"
        bpy.ops.wm.url_open(url=url)
        return {"FINISHED"}


class STREAM_READY_OT_copy(bpy.types.Operator):
    bl_idname = "stream_ready.copy"
    bl_label = "Copy"
    bl_description = "Copy to the clipboard"

    what: StringProperty()  # "asset_id" | "embed"

    def execute(self, context):
        result = state.publish.result or {}
        if self.what == "embed":
            key = (result.get("viewer_key") or {}).get("key") or "VIEWER_KEY"
            text = EMBED.format(key=key, asset=result.get("asset_id", ""))
        else:
            text = result.get("asset_id", "")
        context.window_manager.clipboard = text
        self.report({"INFO"}, "Copied" + (" (the viewer key is not shown again; keep it somewhere safe)" if self.what == "embed" else ""))
        return {"FINISHED"}


def _worker(client, folder, files, kwargs):
    # Runs off the main thread: no bpy calls allowed in here.
    try:
        result = publish(client, folder, files, **kwargs)
    except Cancelled:
        state.publish.log("cancelled")
        state.publish.finish("cancelled")
    except (MirisError, HttpError) as exc:
        state.publish.log(f"failed: {exc}")
        state.publish.log(f"export kept for debugging: {folder}")
        state.publish.finish("failed")
        return
    except Exception as exc:  # a worker that dies silently is worse than a noisy one
        state.publish.log(f"unexpected error: {exc!r}")
        state.publish.log(f"export kept for debugging: {folder}")
        state.publish.finish("failed")
        return
    else:
        if result["status"] == "rejected":
            state.publish.log(f"export kept for debugging: {folder}")
            state.publish.finish("rejected", result)
            return
        state.publish.finish(result["status"], result)
    shutil.rmtree(os.path.dirname(folder), ignore_errors=True)


def _redraw_while_running():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    return 0.25 if state.publish.running else None


def shutdown():
    state.publish.cancel.set()
    if bpy.app.timers.is_registered(_redraw_while_running):
        bpy.app.timers.unregister(_redraw_while_running)
