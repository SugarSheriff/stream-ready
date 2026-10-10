"""Folder checks: each Miris error code, caught locally before upload."""

import shutil
import unittest

from fixtures import PNG_1PX, good_folder, make_folder, usda

from core import bundle


class BundleChecks(unittest.TestCase):
    def check(self, folder):
        self.addCleanup(shutil.rmtree, folder, True)
        report = bundle.check(folder, use_pxr=False)
        return report, {f.code for f in report["findings"] if f.severity == "error"}

    def test_clean_export(self):
        report, errors = self.check(good_folder())
        self.assertEqual(errors, set())
        self.assertEqual(report["root"], "chair.usda")
        self.assertEqual([rel for rel, _ in report["files"]], ["chair.usda", "textures/wood.png"])
        self.assertEqual(report["size_m"], [1.0, 1.0, 1.0])

    def test_missing_usd_file(self):
        _, errors = self.check(make_folder({"textures/wood.png": PNG_1PX}))
        self.assertEqual(errors, {"MISSING_USD_FILE"})

    def test_usd_only_in_a_subfolder_is_still_missing(self):
        _, errors = self.check(make_folder({"export/chair.usda": usda(texture=None)}))
        self.assertEqual(errors, {"MISSING_USD_FILE"})

    def test_more_than_one_root(self):
        _, errors = self.check(make_folder({"chair.usda": usda(texture=None), "chair_payload.usdc": b"PXR-USDC"}))
        self.assertEqual(errors, {"MORE_THAN_ONE_ROOT_USD_FILE"})

    def test_missing_texture(self):
        report, errors = self.check(make_folder({"chair.usda": usda()}))
        self.assertEqual(errors, {"MISSING_REFERENCED_FILE"})
        self.assertIn("does not exist", report["findings"][0].message)

    def test_absolute_texture_path(self):
        report, errors = self.check(make_folder({"chair.usda": usda(texture="C:/Users/artist/wood.png")}))
        self.assertEqual(errors, {"MISSING_REFERENCED_FILE"})
        self.assertIn("absolute path", report["findings"][0].message)

    def test_texture_outside_the_folder(self):
        _, errors = self.check(make_folder({"chair.usda": usda(texture="../shared/wood.png")}))
        self.assertEqual(errors, {"MISSING_REFERENCED_FILE"})

    def test_udim_set_with_tiles(self):
        _, errors = self.check(make_folder({
            "chair.usda": usda(texture="./textures/wood.<UDIM>.png"),
            "textures/wood.1001.png": PNG_1PX,
        }))
        self.assertEqual(errors, set())

    def test_udim_set_without_tiles(self):
        _, errors = self.check(make_folder({"chair.usda": usda(texture="./textures/wood.<UDIM>.png")}))
        self.assertEqual(errors, {"MISSING_REFERENCED_FILE"})

    def test_invisible_geometry(self):
        _, errors = self.check(make_folder({"chair.usda": usda(texture=None, visibility="invisible")}))
        self.assertEqual(errors, {"MISSING_VISIBLE_USD_PRIMS"})

    def test_millimeter_asset_exported_as_meters_is_too_small(self):
        # A 5 mm "chair": what a cm-authored model looks like after a bad 0.01 scale.
        report, errors = self.check(make_folder({"chair.usda": usda(size=0.005, texture=None)}))
        self.assertEqual(errors, {"UNSUPPORTED_SCENE_SIZE"})
        self.assertIn("5.00 mm", report["findings"][0].message)

    def test_meters_per_unit_scales_the_measurement(self):
        # 100 units at 0.01 m/unit is a 1 m chair, not a 100 m one.
        report, errors = self.check(make_folder({"chair.usda": usda(size=100, mpu=0.01, texture=None)}))
        self.assertEqual(errors, set())
        self.assertEqual(report["size_m"], [1.0, 1.0, 1.0])

    def test_continent_sized_asset_is_too_large(self):
        _, errors = self.check(make_folder({"chair.usda": usda(size=10_000, texture=None)}))
        self.assertEqual(errors, {"UNSUPPORTED_SCENE_SIZE"})

    def test_flat_geometry_is_not_rejected_for_zero_volume(self):
        flat = usda(texture=None).replace("(-0.5, -0.5, -0.5), (0.5, -0.5, -0.5), (0.5, 0.5, 0.5), (-0.5, 0.5, 0.5)",
                                          "(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)")
        report, errors = self.check(make_folder({"ground.usda": flat}))
        self.assertEqual(errors, set())
        self.assertEqual(report["size_m"][2], 0.0)

    def test_dotfiles_are_not_uploaded(self):
        folder = good_folder()
        with open(f"{folder}/.DS_Store", "wb") as f:
            f.write(b"junk")
        report, _ = self.check(folder)
        self.assertNotIn(".DS_Store", [rel for rel, _ in report["files"]])


if __name__ == "__main__":
    unittest.main()
