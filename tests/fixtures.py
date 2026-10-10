"""Tiny hand-written USD export folders, one per thing that can go wrong."""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "stream_ready"), str(ROOT)]  # import core/ without the bpy-only package

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d4944415478da63f8cfc0f01f0005000201a5f1d2a20000000049454e44ae426082"
)


def usda(size=1.0, mpu=1.0, texture="./textures/wood.png", visibility=None):
    h = size / 2
    vis = f'\n        token visibility = "{visibility}"' if visibility else ""
    tex = f"""
    def Shader "Tex"
    {{
        uniform token info:id = "UsdUVTexture"
        asset inputs:file = @{texture}@
    }}""" if texture else ""
    return f"""#usda 1.0
(
    defaultPrim = "root"
    metersPerUnit = {mpu}
    upAxis = "Z"
)

def Xform "root"
{{
    def Mesh "Chair"
    {{{vis}
        int[] faceVertexCounts = [4]
        int[] faceVertexIndices = [0, 1, 2, 3]
        point3f[] points = [(-{h}, -{h}, -{h}), ({h}, -{h}, -{h}), ({h}, {h}, {h}), (-{h}, {h}, {h})]
    }}{tex}
}}
"""


def make_folder(files):
    """files: {relative path: str | bytes}. Returns the folder path; removed at exit by the caller."""
    folder = tempfile.mkdtemp(prefix="stream_ready_test_")
    for rel, content in files.items():
        path = os.path.join(folder, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content.encode("utf-8") if isinstance(content, str) else content)
    return folder


def good_folder(name="chair"):
    return make_folder({f"{name}.usda": usda(), "textures/wood.png": PNG_1PX})
