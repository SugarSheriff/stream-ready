"""Scene rules against hand-built snapshots (the Blender side is covered by blender_smoke.py)."""

import unittest

import fixtures  # noqa: F401  (sets up the import path)
from core import preflight
from core.findings import has_errors


def scene(**overrides):
    snap = {
        "unit_system": "METRIC", "unit_scale": 1.0, "length_unit": "METERS",
        "bounds": [[-0.5, -0.5, 0.0], [0.5, 0.5, 1.0]],
        "objects": [{"name": "Chair", "type": "MESH", "polygons": 120, "scale": [1, 1, 1], "materials": ["Wood"]}],
        "materials": [{"name": "Wood", "users": ["Chair"], "shader": "principled", "unsupported_nodes": [],
                       "textures": [{"image": "wood.png", "missing": False, "packed": False, "tiled": False}]}],
        "world": {"env_texture": "studio.hdr", "env_missing": False,
                  "blender_intensity": [1, 1, 1], "background_strength": 1.0},
    }
    snap.update(overrides)
    return snap


def codes(snap):
    return {f.code for f in preflight.run(snap)}


class Preflight(unittest.TestCase):
    def test_clean_scene(self):
        self.assertEqual(preflight.run(scene()), [])

    def test_wrong_units_offer_a_one_click_fix(self):
        findings = preflight.run(scene(unit_scale=0.01, length_unit="CENTIMETERS"))
        self.assertEqual([f.code for f in findings], ["units.not_meters"])
        self.assertEqual(findings[0].autofix, "stream_ready.fix_units")

    def test_imperial_units(self):
        self.assertIn("units.not_meters", codes(scene(unit_system="IMPERIAL", length_unit="FEET")))

    def test_no_geometry_matches_the_miris_error_key(self):
        findings = preflight.run(scene(objects=[], bounds=None))
        self.assertEqual(findings[0].code, "MISSING_VISIBLE_USD_PRIMS")
        self.assertTrue(has_errors(findings))

    def test_tiny_scene_matches_the_miris_error_key(self):
        self.assertIn("UNSUPPORTED_SCENE_SIZE", codes(scene(bounds=[[0, 0, 0], [0.004, 0.004, 0.004]])))

    def test_huge_but_legal_scene_is_a_warning(self):
        found = preflight.run(scene(bounds=[[0, 0, 0], [800, 800, 10]]))
        self.assertEqual([(f.code, f.severity) for f in found], [("units.suspicious_size", "warn")])

    def test_missing_texture_blocks_publish(self):
        mat = scene()["materials"][0]
        mat["textures"][0]["missing"] = True
        findings = preflight.run(scene(materials=[mat]))
        self.assertEqual(findings[0].code, "MISSING_REFERENCED_FILE")
        self.assertEqual(findings[0].object, "Chair")

    def test_packed_texture_is_fine_even_if_the_original_file_is_gone(self):
        mat = scene()["materials"][0]
        mat["textures"][0].update(missing=True, packed=True)
        self.assertEqual(codes(scene(materials=[mat])), set())

    def test_missing_udim_tiles(self):
        mat = scene()["materials"][0]
        mat["textures"][0].update(tiled=True, missing_tiles=[1003])
        self.assertIn("MISSING_REFERENCED_FILE", codes(scene(materials=[mat])))

    def test_unsupported_shader_and_procedural_nodes(self):
        mat = {"name": "Toon", "users": ["Chair"], "shader": "other", "unsupported_nodes": ["Noise Texture"], "textures": []}
        self.assertEqual(codes(scene(materials=[mat])), {"material.unsupported_shader", "material.procedural"})

    def test_negative_scale(self):
        objs = [{"name": "Mirror", "type": "MESH", "polygons": 6, "scale": [-1, 1, 1], "materials": []}]
        self.assertIn("transform.negative_scale", codes(scene(objects=objs)))


class Lighting(unittest.TestCase):
    """The Miris Blender guide's HDRI calibration: DomeLight color = Blender intensity x 1562."""

    def test_no_hdri_is_only_a_note(self):
        findings = preflight.run(scene(world={"env_texture": None}))
        self.assertEqual([(f.code, f.severity) for f in findings], [("lighting.no_hdri", "info")])
        self.assertIsNone(preflight.dome_light_color({"env_texture": None}))

    def test_dome_color_from_the_guides_labeled_node(self):
        world = dict(scene()["world"], blender_intensity=[2, 2, 2])
        self.assertEqual(preflight.dome_light_color(world), [3124, 3124, 3124])

    def test_dome_color_falls_back_to_background_strength(self):
        world = dict(scene()["world"], blender_intensity=None, background_strength=0.5)
        self.assertEqual(codes(scene(world=world)), set())
        self.assertEqual(preflight.dome_light_color(world), [781, 781, 781])

    def test_unequal_rgb_is_flagged_and_averaged(self):
        world = dict(scene()["world"], blender_intensity=[1, 1, 0.4])
        self.assertEqual(codes(scene(world=world)), {"lighting.unequal_rgb"})
        self.assertAlmostEqual(preflight.dome_light_color(world)[0], 0.8 * 1562)

    def test_missing_hdri_file(self):
        world = dict(scene()["world"], env_missing=True)
        self.assertEqual(codes(scene(world=world)), {"MISSING_REFERENCED_FILE"})


if __name__ == "__main__":
    unittest.main()
