# SPDX-License-Identifier: GPL-3.0-or-later
"""Findings, and the one table that ties them to Miris's own error codes.

Upper-case codes are the error keys Miris's processing pipeline reports
(https://docs.miris.com/preparing-assets/error-reference). The add-on raises the same
codes before upload, so an artist sees the identical name whether a problem is caught
in Blender in a second or by the server after a round trip. Lower-case codes are this
add-on's own checks for things Miris accepts but renders badly.
"""

from dataclasses import asdict, dataclass

ERROR, WARN, INFO = "error", "warn", "info"

MIRIS_CODES = {
    "MISSING_REFERENCED_FILE": "The USD references a file (texture, sublayer, payload) that is not in the upload.",
    "MISSING_USD_FILE": "There is no USD file at the root of the upload folder.",
    "MISSING_VISIBLE_USD_PRIMS": "The USD has no renderable geometry.",
    "MORE_THAN_ONE_ROOT_USD_FILE": "More than one USD file sits at the root of the upload folder.",
    "UNSUPPORTED_SCENE_SIZE": "The geometry is outside the 1 cm³ to 100 km³ range Miris accepts.",
    "UNDEFINED": "Miris hit an unexpected processing error.",
}

# Default fixes when the server, not preflight, reports the code (preflight findings
# carry a fix specific to the object or file involved).
SERVER_FIXES = {
    "MISSING_REFERENCED_FILE": "Re-publish from Blender: the add-on exports with relative paths and copies textures next to the USD.",
    "MISSING_USD_FILE": "Publish the export folder itself, not its textures/ subfolder.",
    "MISSING_VISIBLE_USD_PRIMS": "Check that the exported objects are visible and enabled for render, then re-publish.",
    "MORE_THAN_ONE_ROOT_USD_FILE": "Keep one .usd at the folder root; move sublayers and payloads into a subfolder.",
    "UNSUPPORTED_SCENE_SIZE": "Model in meters at Unit Scale 1.0 and apply object scale, then re-publish.",
    "UNDEFINED": "Contact Miris support with the asset ID and the publish log.",
}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    target: str
    message: str
    fix: str
    object: str = ""  # an object the panel can select to show the artist where the problem is
    autofix: str = ""  # id of an operator that can fix it in one click, if any

    def to_dict(self):
        return asdict(self)

    @property
    def is_miris_code(self):
        return self.code in MIRIS_CODES


def sort(findings):
    order = {ERROR: 0, WARN: 1, INFO: 2}
    return sorted(findings, key=lambda f: (order[f.severity], f.code, f.target))


def has_errors(findings):
    return any(f.severity == ERROR for f in findings)


def summarize(findings):
    if not findings:
        return "Preflight clean"
    counts = {s: sum(1 for f in findings if f.severity == s) for s in (ERROR, WARN, INFO)}
    return f"Preflight: {counts[ERROR]} error(s), {counts[WARN]} warning(s), {counts[INFO]} note(s)"


def from_server(job_errors):
    """Turn the JobError list on an asset into Findings the panel already knows how to show."""
    out = []
    for err in job_errors or []:
        code = (err.get("error_key") or "UNDEFINED").upper()
        detail = err.get("error_description") or MIRIS_CODES.get(code, "Processing failed.")
        out.append(Finding(code, ERROR, "Miris", detail, SERVER_FIXES.get(code, SERVER_FIXES["UNDEFINED"])))
    return out
