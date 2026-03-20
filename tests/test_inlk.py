"""Regression tests for INLK (internal link) format in packed image references.

These tests validate the binary format of the INLK TLV that packed image
renditions use to reference their parent atlas texture. The system actool
is used to produce reference CAR files for comparison.

Regression for: padding was 4 bytes instead of 2, causing CoreUI to
interpret the second zero as a terminator and never apply any parent
attributes. This made all packed images unresolvable at runtime.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from actool.compiler import compile_catalog
from tests.helpers import (
    REF_XCASSETS,
    has_system_actool,
    compile_with_system_actool,
    make_temp_catalog,
    parse_car_inlk_entries,
    parse_car_atlas_keys,
)


class TestInlkFormat(unittest.TestCase):
    """Test INLK binary format matches what CoreUI expects."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="actool_inlk_")
        self.outdir = os.path.join(self.tmpdir, "out")
        compile_catalog(REF_XCASSETS, self.outdir, "macosx", "11.0",
                        app_icon="AppIcon",
                        info_plist_path=os.path.join(self.outdir, "Info.plist"))
        self.car_path = os.path.join(self.outdir, "Assets.car")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_inlk_padding_is_single_uint16(self):
        """Padding before attr pairs must be exactly one uint16 zero.

        Regression: was struct.pack("<HH", 0, 0) = 4 bytes, causing CoreUI
        to see token_id=0 (terminator) as the first attr pair.
        """
        entries = parse_car_inlk_entries(self.car_path)
        self.assertGreater(len(entries), 0, "No INLK entries found")
        for e in entries:
            self.assertEqual(e['padding'], 0,
                             f"{e['name']}: padding should be 0")

    def test_inlk_attrs_contain_element_and_part(self):
        """Every INLK must have Element=9 and Part=181 in trailing attrs.

        Regression: with double padding, CoreUI saw terminator immediately
        and got no attributes at all.
        """
        entries = parse_car_inlk_entries(self.car_path)
        for e in entries:
            self.assertEqual(e['inlk_attrs'].get(1), 9,
                             f"{e['name']}: Element should be 9 (ELEMENT_PACKED)")
            self.assertEqual(e['inlk_attrs'].get(2), 181,
                             f"{e['name']}: Part should be 181 (PART_REGULAR)")

    def test_inlk_attrs_contain_scale(self):
        """Every INLK must have Scale matching the rendition's scale."""
        entries = parse_car_inlk_entries(self.car_path)
        for e in entries:
            self.assertIn(12, e['inlk_attrs'],
                          f"{e['name']}: Scale attr missing from INLK")
            self.assertEqual(e['inlk_attrs'][12], e['scale'],
                             f"{e['name']}: INLK Scale={e['inlk_attrs'][12]} "
                             f"but rendition scale={e['scale']}")

    def test_inlk_f2_accounts_for_terminator(self):
        """f2 (attr byte count) must include padding + pairs + terminator.

        Regression: terminator was outside f2, causing CoreUI to potentially
        read past the intended data boundary.
        """
        entries = parse_car_inlk_entries(self.car_path)
        for e in entries:
            n_attrs = len(e['inlk_attrs'])
            # f2 = padding(2) + n_attrs * pair(4) + terminator(2)
            expected = 2 + n_attrs * 4 + 2
            self.assertEqual(e['f2'], expected,
                             f"{e['name']}: f2={e['f2']} but expected {expected} "
                             f"for {n_attrs} attrs")

    def test_inlk_all_resolve_to_atlas(self):
        """Every INLK must point to an existing atlas rendition.

        Regression: missing Dim1 attr caused INLK to construct parent keys
        that didn't match any atlas entry.
        """
        entries = parse_car_inlk_entries(self.car_path)
        atlas_keys = parse_car_atlas_keys(self.car_path)
        self.assertGreater(len(atlas_keys), 0, "No atlas entries found")

        for e in entries:
            dim1 = e['inlk_attrs'].get(8, 0)
            scale = e['inlk_attrs'].get(12, 0)
            self.assertIn((dim1, scale), atlas_keys,
                          f"{e['name']}: INLK points to atlas "
                          f"(dim1={dim1}, scale={scale}) which doesn't exist. "
                          f"Available: {sorted(atlas_keys)}")

    def test_inlk_dim1_present_when_nonzero(self):
        """Dim1 attr (token 8) included when atlas has non-zero Dim1."""
        entries = parse_car_inlk_entries(self.car_path)
        atlas_keys = parse_car_atlas_keys(self.car_path)

        for e in entries:
            dim1 = e['inlk_attrs'].get(8, 0)
            scale = e['inlk_attrs'].get(12, 0)
            # If the target atlas has dim1>0, INLK must include it
            target = (dim1, scale)
            self.assertIn(target, atlas_keys,
                          f"{e['name']}: target atlas not found")


@unittest.skipUnless(has_system_actool(), "system actool not available")
class TestInlkMatchesSystemActool(unittest.TestCase):
    """Compare INLK format between our output and system actool."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="actool_inlk_ref_")
        self.our_dir = os.path.join(self.tmpdir, "ours")
        self.sys_dir = os.path.join(self.tmpdir, "system")

        compile_catalog(REF_XCASSETS, self.our_dir, "macosx", "11.0",
                        app_icon="AppIcon",
                        info_plist_path=os.path.join(self.our_dir, "Info.plist"))
        compile_with_system_actool(REF_XCASSETS, self.sys_dir,
                                   app_icon="AppIcon")

        self.our_car = os.path.join(self.our_dir, "Assets.car")
        self.sys_car = os.path.join(self.sys_dir, "Assets.car")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_same_inlk_padding_format(self):
        """Our padding value matches system actool (single uint16 = 0)."""
        our_entries = parse_car_inlk_entries(self.our_car)
        sys_entries = parse_car_inlk_entries(self.sys_car)

        for e in our_entries:
            self.assertEqual(e['padding'], 0)
        for e in sys_entries:
            self.assertEqual(e['padding'], 0)

    def test_same_inlk_f1_constant(self):
        """f1 header field matches system actool (always 12)."""
        our_entries = parse_car_inlk_entries(self.our_car)
        sys_entries = parse_car_inlk_entries(self.sys_car)

        for e in our_entries:
            self.assertEqual(e['f1'], 12, f"Our {e['name']}: f1={e['f1']}")
        for e in sys_entries:
            self.assertEqual(e['f1'], 12, f"Sys {e['name']}: f1={e['f1']}")

    def test_same_required_attrs(self):
        """Both produce Element=9, Part=181, Scale in every INLK."""
        our_entries = parse_car_inlk_entries(self.our_car)
        sys_entries = parse_car_inlk_entries(self.sys_car)

        for label, entries in [("ours", our_entries), ("system", sys_entries)]:
            for e in entries:
                self.assertEqual(e['inlk_attrs'].get(1), 9,
                                 f"{label} {e['name']}: Element != 9")
                self.assertEqual(e['inlk_attrs'].get(2), 181,
                                 f"{label} {e['name']}: Part != 181")
                self.assertIn(12, e['inlk_attrs'],
                              f"{label} {e['name']}: missing Scale")

    def test_all_system_inlk_resolve(self):
        """System actool INLK entries all resolve (sanity check)."""
        sys_entries = parse_car_inlk_entries(self.sys_car)
        sys_atlases = parse_car_atlas_keys(self.sys_car)
        for e in sys_entries:
            dim1 = e['inlk_attrs'].get(8, 0)
            scale = e['inlk_attrs'].get(12, 0)
            self.assertIn((dim1, scale), sys_atlases,
                          f"System {e['name']}: unresolved atlas "
                          f"(dim1={dim1}, scale={scale})")

    def test_all_our_inlk_resolve(self):
        """Our INLK entries all resolve to valid atlases."""
        our_entries = parse_car_inlk_entries(self.our_car)
        our_atlases = parse_car_atlas_keys(self.our_car)
        for e in our_entries:
            dim1 = e['inlk_attrs'].get(8, 0)
            scale = e['inlk_attrs'].get(12, 0)
            self.assertIn((dim1, scale), our_atlases,
                          f"Our {e['name']}: unresolved atlas "
                          f"(dim1={dim1}, scale={scale})")

    def test_f2_format_matches(self):
        """f2 byte count follows same pattern: 2 + 4*N + 2."""
        our_entries = parse_car_inlk_entries(self.our_car)
        sys_entries = parse_car_inlk_entries(self.sys_car)

        for label, entries in [("ours", our_entries), ("system", sys_entries)]:
            for e in entries:
                n = len(e['inlk_attrs'])
                expected = 2 + n * 4 + 2
                self.assertEqual(e['f2'], expected,
                                 f"{label} {e['name']}: f2={e['f2']} "
                                 f"expected {expected} for {n} attrs")


@unittest.skipUnless(has_system_actool(), "system actool not available")
class TestInlkMixedFormats(unittest.TestCase):
    """Test INLK resolution with mixed pixel formats (multiple atlas groups)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="actool_inlk_mixed_")
        catalog, _ = make_temp_catalog(
            [("RgbA", "RGBA"), ("RgbB", "RGBA"),
             ("GrayA", "LA"), ("GrayB", "LA")],
            self.tmpdir)
        self.our_dir = os.path.join(self.tmpdir, "ours")
        self.sys_dir = os.path.join(self.tmpdir, "system")

        compile_catalog(catalog, self.our_dir, "macosx", "11.0")
        compile_with_system_actool(catalog, self.sys_dir)

        self.our_car = os.path.join(self.our_dir, "Assets.car")
        self.sys_car = os.path.join(self.sys_dir, "Assets.car")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_mixed_format_inlk_all_resolve(self):
        """INLK links resolve when multiple pixel formats create separate atlases."""
        our_entries = parse_car_inlk_entries(self.our_car)
        our_atlases = parse_car_atlas_keys(self.our_car)
        self.assertGreater(len(our_entries), 0)

        for e in our_entries:
            dim1 = e['inlk_attrs'].get(8, 0)
            scale = e['inlk_attrs'].get(12, 0)
            self.assertIn((dim1, scale), our_atlases,
                          f"{e['name']}: unresolved (dim1={dim1}, scale={scale})")

    def test_system_mixed_format_all_resolve(self):
        """System actool mixed format INLK all resolve (sanity check)."""
        sys_entries = parse_car_inlk_entries(self.sys_car)
        sys_atlases = parse_car_atlas_keys(self.sys_car)
        for e in sys_entries:
            dim1 = e['inlk_attrs'].get(8, 0)
            scale = e['inlk_attrs'].get(12, 0)
            self.assertIn((dim1, scale), sys_atlases,
                          f"System {e['name']}: unresolved")


if __name__ == "__main__":
    unittest.main()
