"""Tests for CAR file structural correctness."""

import os
import shutil
import struct
import tempfile
import unittest

from actool.compiler import compile_catalog
from tests.helpers import (
    REF_XCASSETS, REF_CAR, has_ref_car,
    parse_car_info, parse_car_layouts, make_temp_catalog,
)


class TestCarHeader(unittest.TestCase):
    """Test CARHEADER block structure."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="actool_test_")
        self.outdir = os.path.join(self.tmpdir, "out")
        compile_catalog(REF_XCASSETS, self.outdir, "macosx", "11.0",
                        app_icon="AppIcon",
                        info_plist_path=os.path.join(self.outdir, "Info.plist"))
        self.car_path = os.path.join(self.outdir, "Assets.car")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_magic(self):
        with open(self.car_path, 'rb') as f:
            self.assertEqual(f.read(8), b"BOMStore")

    def test_carheader_tag(self):
        with open(self.car_path, 'rb') as f:
            data = f.read()
        # Find CARHEADER via named blocks
        idx_off = struct.unpack('>I', data[16:20])[0]
        idx_data = data[idx_off:]
        n = struct.unpack('>I', idx_data[:4])[0]
        blocks = [(0, 0)]
        for i in range(1, n):
            off, ln = struct.unpack('>II', idx_data[4 + i * 8:12 + i * 8])
            blocks.append((off, ln))
        vars_off = struct.unpack('>I', data[24:28])[0]
        vars_ln = struct.unpack('>I', data[28:32])[0]
        vd = data[vars_off:vars_off + vars_ln]
        nv = struct.unpack('>I', vd[:4])[0]
        p = 4
        for _ in range(nv):
            vi = struct.unpack('>I', vd[p:p + 4])[0]
            nl = vd[p + 4]
            nm = vd[p + 5:p + 5 + nl].decode()
            p += 5 + nl
            if nm == "CARHEADER":
                off, ln = blocks[vi]
                tag = data[off:off + 4]
                self.assertEqual(tag, b"RATC")
                return
        self.fail("CARHEADER not found")

    def test_named_blocks_present(self):
        info = parse_car_info(self.car_path)
        required = ["CARHEADER", "KEYFORMAT", "EXTENDED_METADATA",
                     "FACETKEYS", "RENDITIONS", "BITMAPKEYS"]
        for name in required:
            self.assertIn(name, info["named_blocks"], f"Missing: {name}")

    def test_block_table_min_256(self):
        info = parse_car_info(self.car_path)
        self.assertGreaterEqual(info["table_count"], 256)


class TestKeyformat(unittest.TestCase):
    """Test dynamic KEYFORMAT generation."""

    def _compile(self, imagesets, app_icon=None):
        tmpdir = tempfile.mkdtemp(prefix="actool_test_")
        catalog, _ = make_temp_catalog(imagesets, tmpdir)
        outdir = os.path.join(tmpdir, "out")
        compile_catalog(catalog, outdir, "macosx", "11.0", app_icon=app_icon)
        info = parse_car_info(os.path.join(outdir, "Assets.car"))
        shutil.rmtree(tmpdir)
        return info

    def test_no_dim_tokens_when_unused(self):
        """Single imageset → no Dim1/Dim2 in keyformat."""
        info = self._compile([("Solo", "RGBA")])
        self.assertEqual(info["keyformat_count"], 8)

    def test_dim1_included_with_multiple_packs(self):
        """Multiple format groups → Dim1 included."""
        info = self._compile([("A", "RGBA"), ("B", "RGBA"),
                              ("C", "LA"), ("D", "LA")])
        self.assertGreaterEqual(info["keyformat_count"], 9)


class TestRenditionCounts(unittest.TestCase):
    """Test rendition counts match expectations."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="actool_test_")
        self.outdir = os.path.join(self.tmpdir, "out")
        compile_catalog(REF_XCASSETS, self.outdir, "macosx", "11.0",
                        app_icon="AppIcon",
                        info_plist_path=os.path.join(self.outdir, "Info.plist"))
        self.info = parse_car_info(os.path.join(self.outdir, "Assets.car"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_rendition_count(self):
        self.assertEqual(self.info["rendition_count"], 57)

    def test_layout_distribution(self):
        lc = self.info["layout_counts"]
        self.assertEqual(lc.get(12, 0), 5, "Inline icons")
        self.assertEqual(lc.get(1003, 0), 45, "Packed refs")
        self.assertEqual(lc.get(1004, 0), 6, "Packed assets")
        self.assertEqual(lc.get(1010, 0), 1, "Multisize image")

    @unittest.skipUnless(has_ref_car(), "No reference CAR available")
    def test_rendition_count_close_to_ref(self):
        """Rendition count within 2 of reference (minor atlas split diffs)."""
        ref_info = parse_car_info(REF_CAR)
        diff = abs(self.info["rendition_count"] - ref_info["rendition_count"])
        self.assertLessEqual(diff, 2,
                             f"Ours={self.info['rendition_count']} "
                             f"ref={ref_info['rendition_count']}")

    @unittest.skipUnless(has_ref_car(), "No reference CAR available")
    def test_layout_types_match_ref(self):
        """Same layout types present (counts may differ slightly)."""
        ref_info = parse_car_info(REF_CAR)
        self.assertEqual(set(self.info["layout_counts"].keys()),
                         set(ref_info["layout_counts"].keys()))


class TestInfoPlist(unittest.TestCase):
    """Test partial info plist output."""

    def test_plist_matches_ref(self):
        tmpdir = tempfile.mkdtemp(prefix="actool_test_")
        outdir = os.path.join(tmpdir, "out")
        plist_path = os.path.join(outdir, "Info.plist")
        compile_catalog(REF_XCASSETS, outdir, "macosx", "11.0",
                        app_icon="AppIcon", info_plist_path=plist_path)
        with open(plist_path) as f:
            content = f.read()
        self.assertIn("CFBundleIconFile", content)
        self.assertIn("AppIcon", content)
        self.assertIn("CFBundleIconName", content)
        shutil.rmtree(tmpdir)

    def test_plist_with_accent_color(self):
        tmpdir = tempfile.mkdtemp(prefix="actool_test_")
        outdir = os.path.join(tmpdir, "out")
        plist_path = os.path.join(outdir, "Info.plist")
        compile_catalog(REF_XCASSETS, outdir, "macosx", "11.0",
                        accent_color="AccentColor",
                        info_plist_path=plist_path)
        with open(plist_path) as f:
            content = f.read()
        self.assertIn("NSAccentColorName", content)
        self.assertIn("AccentColor", content)
        shutil.rmtree(tmpdir)


class TestNestedGroups(unittest.TestCase):
    """Test that imagesets inside group subdirectories are included.

    Regression: the catalog parser only iterated top-level entries,
    skipping group directories like 'devices/' and 'support/'.
    """

    def test_group_imagesets_included(self):
        """Imagesets in group subdirectories appear as facets."""
        catalog, tmpdir = make_temp_catalog(
            [("TopLevel", "RGBA")],
            groups={
                "mygroup": [("Nested1", "RGBA"), ("Nested2", "RGBA")],
            })
        try:
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", "11.0")
            info = parse_car_info(os.path.join(outdir, "Assets.car"))
            layouts = parse_car_layouts(os.path.join(outdir, "Assets.car"))
            # All three imagesets must be present
            self.assertIn("TopLevel.png", layouts)
            self.assertIn("Nested1.png", layouts)
            self.assertIn("Nested2.png", layouts)
        finally:
            shutil.rmtree(tmpdir)

    def test_deeply_nested_groups(self):
        """Groups nested multiple levels deep are all parsed."""
        catalog, tmpdir = make_temp_catalog(
            [("Root", "RGBA")],
            groups={
                "level1": [("L1", "RGBA")],
                "level1/level2": [("L2", "RGBA")],
            })
        try:
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", "11.0")
            layouts = parse_car_layouts(os.path.join(outdir, "Assets.car"))
            self.assertIn("Root.png", layouts)
            self.assertIn("L1.png", layouts)
            self.assertIn("L2.png", layouts)
        finally:
            shutil.rmtree(tmpdir)


if __name__ == "__main__":
    unittest.main()
