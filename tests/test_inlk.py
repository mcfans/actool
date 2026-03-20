"""Regression tests for INLK and CELM format in packed image renditions.

These tests validate:
1. INLK TLV binary format (internal links from packed images to atlases)
2. CELM block format (pixel data compression)
3. End-to-end rendering validation via CoreUI

The system actool is used to produce reference CAR files for comparison.

Regressions covered:
- INLK padding was 4 bytes instead of 2, causing CoreUI to see a terminator
  immediately and never apply parent atlas attributes.
- CELM with LZFSE compression (ver=1, comp=4) is not supported by CoreUI;
  only uncompressed (ver=1, comp=0) or DMP2 (ver=2, comp=11) work.
"""

import os
import shutil
import struct
import subprocess
import tempfile
import unittest

from actool.compiler import compile_catalog
from tests.helpers import (
    REF_XCASSETS,
    has_system_actool,
    has_validate_car,
    compile_with_system_actool,
    make_temp_catalog,
    parse_car_inlk_entries,
    parse_car_atlas_keys,
    parse_car_info,
    validate_car_rendering,
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


def _parse_celm_entries(car_path):
    """Extract CELM (compression) details from all renditions with pixel data."""
    with open(car_path, 'rb') as f:
        data = f.read()

    idx_off = struct.unpack('>I', data[16:20])[0]
    idx_data = data[idx_off:idx_off + struct.unpack('>I', data[20:24])[0]]
    n = struct.unpack('>I', idx_data[:4])[0]
    blocks = {}
    for i in range(n):
        a = struct.unpack('>I', idx_data[4 + i * 8:8 + i * 8])[0]
        l = struct.unpack('>I', idx_data[8 + i * 8:12 + i * 8])[0]
        if a or l:
            blocks[i] = (a, l)

    def rb(idx):
        if idx not in blocks:
            return b''
        return data[blocks[idx][0]:blocks[idx][0] + blocks[idx][1]]

    vars_off, vars_ln = struct.unpack('>II', data[24:32])
    vd = data[vars_off:vars_off + vars_ln]
    nv = struct.unpack('>I', vd[:4])[0]
    named = {}
    p = 4
    for _ in range(nv):
        bi = struct.unpack('>I', vd[p:p + 4])[0]
        nl = vd[p + 4]
        nm = vd[p + 5:p + 5 + nl].decode('ascii')
        p += 5 + nl
        named[nm] = bi

    rt = rb(named['RENDITIONS'])
    root = struct.unpack('>I', rt[8:12])[0]
    rends = []

    def coll(bi):
        nd = rb(bi)
        il = struct.unpack('>H', nd[:2])[0]
        cnt = struct.unpack('>H', nd[2:4])[0]
        if il:
            p2 = 12
            for _ in range(cnt):
                vi = struct.unpack('>I', nd[p2:p2 + 4])[0]
                ki = struct.unpack('>I', nd[p2 + 4:p2 + 8])[0]
                p2 += 8
                rends.append((rb(ki), rb(vi)))
        else:
            p2 = 12
            c = struct.unpack('>I', nd[p2:p2 + 4])[0]
            p2 += 4
            coll(c)
            for _ in range(cnt):
                p2 += 4
                c2 = struct.unpack('>I', nd[p2:p2 + 4])[0]
                p2 += 4
                coll(c2)
    coll(root)

    results = []
    for kd, vd_block in rends:
        if len(vd_block) < 184:
            continue
        layout = struct.unpack('<H', vd_block[36:38])[0]
        rend_len = struct.unpack('<I', vd_block[180:184])[0]
        if rend_len < 16:
            continue
        tvl_len = struct.unpack('<I', vd_block[168:172])[0]
        rs = 184 + tvl_len
        celm = vd_block[rs:rs + 16]
        name = vd_block[40:168].rstrip(b'\x00').decode('ascii', errors='replace')
        w = struct.unpack('<I', vd_block[12:16])[0]
        h = struct.unpack('<I', vd_block[16:20])[0]
        pf = vd_block[24:28]
        results.append({
            'name': name,
            'layout': layout,
            'width': w,
            'height': h,
            'pixel_format': pf,
            'celm_tag': celm[0:4],
            'celm_ver': struct.unpack('<I', celm[4:8])[0],
            'celm_comp': struct.unpack('<I', celm[8:12])[0],
            'celm_datalen': struct.unpack('<I', celm[12:16])[0],
            'rend_len': rend_len,
        })
    return results


class TestCelmFormat(unittest.TestCase):
    """Test CELM block format produces data CoreUI can decompress.

    Regression: CELM ver=1 comp=4 (plain LZFSE) causes CoreUI crash
    'Can't find the correct chunk'. Only comp=0 (uncompressed) is safe
    for CELM ver=1.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="actool_celm_")
        self.outdir = os.path.join(self.tmpdir, "out")
        compile_catalog(REF_XCASSETS, self.outdir, "macosx", "11.0",
                        app_icon="AppIcon",
                        info_plist_path=os.path.join(self.outdir, "Info.plist"))
        self.car_path = os.path.join(self.outdir, "Assets.car")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_celm_no_ver1_lzfse(self):
        """CELM ver=1 must NOT use comp=4 (LZFSE).

        Regression: plain LZFSE in CELM ver=1 crashes CoreUI with
        'Can't find the correct chunk'. LZFSE requires CELM ver=2.
        """
        entries = _parse_celm_entries(self.car_path)
        self.assertGreater(len(entries), 0)
        for e in entries:
            if e['celm_ver'] == 1:
                self.assertNotEqual(e['celm_comp'], 4,
                                    f"{e['name']}: CELM ver=1 comp=4 (LZFSE) "
                                    f"is not supported by CoreUI")

    def test_celm_lzfse_uses_ver2(self):
        """When LZFSE is used, it must be CELM ver=2."""
        entries = _parse_celm_entries(self.car_path)
        for e in entries:
            if e['celm_comp'] == 4:
                self.assertEqual(e['celm_ver'], 2,
                                 f"{e['name']}: LZFSE comp=4 requires ver=2, "
                                 f"got ver={e['celm_ver']}")

    def test_celm_no_lzfse_for_pre_10_11(self):
        """LZFSE must not be used when targeting macOS < 10.11.

        LZFSE was introduced in macOS 10.11. Earlier targets must use
        uncompressed data only.
        """
        tmpdir = tempfile.mkdtemp(prefix="actool_celm_deploy_")
        try:
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(REF_XCASSETS, outdir, "macosx", "10.10",
                            app_icon="AppIcon",
                            info_plist_path=os.path.join(outdir, "Info.plist"))
            entries = _parse_celm_entries(os.path.join(outdir, "Assets.car"))
            for e in entries:
                self.assertNotEqual(e['celm_comp'], 4,
                                    f"{e['name']}: LZFSE used with 10.10 target")
        finally:
            shutil.rmtree(tmpdir)

    def test_celm_uncompressed_data_matches_dimensions(self):
        """For uncompressed CELM, data length must match w * h * bpp."""
        entries = _parse_celm_entries(self.car_path)
        for e in entries:
            if e['celm_comp'] != 0:
                continue
            if e['width'] == 0 or e['height'] == 0:
                continue
            bpp = 4 if e['pixel_format'] == b'BGRA' else 2
            expected = e['width'] * e['height'] * bpp
            self.assertEqual(e['celm_datalen'], expected,
                             f"{e['name']}: CELM data {e['celm_datalen']} "
                             f"!= {e['width']}*{e['height']}*{bpp}={expected}")

    def test_celm_tag_is_mlec(self):
        """Renditions with pixel data must have 'MLEC' CELM tag."""
        entries = _parse_celm_entries(self.car_path)
        for e in entries:
            # Skip non-pixel renditions (multisize=SISM, color=RLOC, etc.)
            if e['layout'] in (1009, 1010, 1005):
                continue
            self.assertEqual(e['celm_tag'], b'MLEC',
                             f"{e['name']}: CELM tag={e['celm_tag']}")


@unittest.skipUnless(has_validate_car(), "validate_car tool not built")
class TestCoreUIRendering(unittest.TestCase):
    """End-to-end validation that CoreUI can render all images.

    Uses the validate_car tool which loads each image via CUICatalog
    and forces pixel decompression by drawing into a CGBitmapContext.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="actool_render_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_render_main_catalog(self):
        """All images from the main test catalog render without crash."""
        outdir = os.path.join(self.tmpdir, "main")
        compile_catalog(REF_XCASSETS, outdir, "macosx", "11.0",
                        app_icon="AppIcon",
                        info_plist_path=os.path.join(outdir, "Info.plist"))
        car = os.path.join(outdir, "Assets.car")
        ok, fail, details = validate_car_rendering(car)
        crashes = [d for d in details if d[0] == "CRASH"]
        self.assertEqual(len(crashes), 0,
                         f"CoreUI crashes: {crashes}")
        self.assertGreater(ok, 0, "No images validated")

    def test_render_mixed_formats(self):
        """Mixed BGRA + GA8 catalog renders without crash."""
        catalog, _ = make_temp_catalog(
            [("RgbA", "RGBA"), ("RgbB", "RGBA"),
             ("GrayA", "LA"), ("GrayB", "LA")],
            self.tmpdir)
        outdir = os.path.join(self.tmpdir, "mixed")
        compile_catalog(catalog, outdir, "macosx", "11.0")
        car = os.path.join(outdir, "Assets.car")
        ok, fail, details = validate_car_rendering(car)
        crashes = [d for d in details if d[0] == "CRASH"]
        self.assertEqual(len(crashes), 0,
                         f"CoreUI crashes: {crashes}")

    @unittest.skipUnless(has_system_actool(), "system actool not available")
    def test_render_matches_system_actool_success(self):
        """Our car validates the same set of images as system actool."""
        our_dir = os.path.join(self.tmpdir, "ours")
        sys_dir = os.path.join(self.tmpdir, "system")
        compile_catalog(REF_XCASSETS, our_dir, "macosx", "11.0",
                        app_icon="AppIcon",
                        info_plist_path=os.path.join(our_dir, "Info.plist"))
        compile_with_system_actool(REF_XCASSETS, sys_dir, app_icon="AppIcon")

        our_ok, our_fail, our_details = validate_car_rendering(
            os.path.join(our_dir, "Assets.car"))
        sys_ok, sys_fail, sys_details = validate_car_rendering(
            os.path.join(sys_dir, "Assets.car"))

        self.assertEqual(sys_fail, 0, f"System car failures: {sys_details}")
        self.assertEqual(our_fail, 0, f"Our car failures: {our_details}")
        # Same number of successful images
        self.assertEqual(our_ok, sys_ok,
                         f"Ours: {our_ok} OK, System: {sys_ok} OK")


@unittest.skipUnless(has_validate_car() and has_system_actool(),
                     "validate_car tool or system actool not available")
class TestCoreUIRenderingLegacyTarget(unittest.TestCase):
    """Regression tests using system actool with legacy deployment targets.

    Tests 10.10 (CELM ver=2/3 with RLE/LZVN) and 10.11 (CELM ver=3 comp=4
    LZFSE). Our car files must render identically to the system output.

    Regression: CELM ver=1 comp=4 (plain LZFSE) caused 'Can't find the
    correct chunk' crash. Only uncompressed (comp=0) or Apple's proprietary
    formats (ver=2/3) are supported by CoreUI.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="actool_legacy_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _assert_renders_match(self, min_deploy, catalog_path, app_icon=None,
                              label=""):
        """Compile with both actool implementations, assert both render OK."""
        our_dir = os.path.join(self.tmpdir, f"ours_{label}")
        sys_dir = os.path.join(self.tmpdir, f"system_{label}")
        plist = os.path.join(our_dir, "Info.plist") if app_icon else None
        compile_catalog(catalog_path, our_dir, "macosx", min_deploy,
                        app_icon=app_icon, info_plist_path=plist)
        compile_with_system_actool(catalog_path, sys_dir,
                                   app_icon=app_icon,
                                   min_deploy=min_deploy)

        sys_ok, sys_fail, sys_details = validate_car_rendering(
            os.path.join(sys_dir, "Assets.car"))
        self.assertEqual(sys_fail, 0,
                         f"System {min_deploy} car failures: {sys_details}")

        our_ok, our_fail, our_details = validate_car_rendering(
            os.path.join(our_dir, "Assets.car"))
        crashes = [d for d in our_details if d[0] == "CRASH"]
        self.assertEqual(len(crashes), 0,
                         f"Our {min_deploy} car CoreUI crashes: {crashes}")
        self.assertEqual(our_ok, sys_ok,
                         f"Ours: {our_ok} OK, System: {sys_ok} OK")

    def test_render_10_10_matches_system(self):
        """Our car renders same images as system actool with 10.10 target."""
        self._assert_renders_match("10.10", REF_XCASSETS,
                                   app_icon="AppIcon", label="10_10")

    def test_render_10_10_mixed_formats(self):
        """Mixed BGRA + GA8 catalog renders with 10.10 target."""
        catalog, _ = make_temp_catalog(
            [("RgbA", "RGBA"), ("RgbB", "RGBA"),
             ("GrayA", "LA"), ("GrayB", "LA")],
            self.tmpdir)
        our_dir = os.path.join(self.tmpdir, "mixed_ours")
        sys_dir = os.path.join(self.tmpdir, "mixed_sys")
        compile_catalog(catalog, our_dir, "macosx", "10.10")
        compile_with_system_actool(catalog, sys_dir, min_deploy="10.10")

        sys_ok, sys_fail, _ = validate_car_rendering(
            os.path.join(sys_dir, "Assets.car"))
        self.assertEqual(sys_fail, 0)

        our_ok, our_fail, our_details = validate_car_rendering(
            os.path.join(our_dir, "Assets.car"))
        crashes = [d for d in our_details if d[0] == "CRASH"]
        self.assertEqual(len(crashes), 0,
                         f"CoreUI crashes: {crashes}")

    def test_render_10_11_matches_system(self):
        """Our car renders same images as system actool with 10.11 target.

        The 10.11 target causes the system actool to produce CELM ver=3
        comp=4 (LZFSE) entries. Our uncompressed output must render the
        same set of images.
        """
        self._assert_renders_match("10.11", REF_XCASSETS,
                                   app_icon="AppIcon", label="10_11")

    def test_render_10_11_mixed_formats(self):
        """Mixed BGRA + GA8 catalog renders with 10.11 target."""
        catalog, _ = make_temp_catalog(
            [("RgbA", "RGBA"), ("RgbB", "RGBA"),
             ("GrayA", "LA"), ("GrayB", "LA")],
            self.tmpdir)
        self._assert_renders_match("10.11", catalog, label="10_11_mixed")

    def test_celm_no_unsupported_compression(self):
        """CELM blocks must not use compression types CoreUI can't handle.

        Regression: comp=4 (plain LZFSE) in CELM ver=1 crashes CoreUI.
        Supported: comp=0 (uncompressed), or Apple's ver=2/3 proprietary
        formats (comp=1 RLE, comp=3 LZVN, comp=4 LZFSE, comp=11 DMP2).
        """
        for min_deploy in ("10.10", "10.11", "11.0"):
            outdir = os.path.join(self.tmpdir, f"celm_{min_deploy}")
            compile_catalog(REF_XCASSETS, outdir, "macosx", min_deploy,
                            app_icon="AppIcon",
                            info_plist_path=os.path.join(outdir, "Info.plist"))
            entries = _parse_celm_entries(os.path.join(outdir, "Assets.car"))
            unsupported_ver1_comps = {4}  # LZFSE in ver=1 is broken
            for e in entries:
                if e['celm_ver'] == 1 and e['celm_comp'] in unsupported_ver1_comps:
                    self.fail(
                        f"{e['name']} (target {min_deploy}): "
                        f"CELM ver=1 comp={e['celm_comp']} crashes CoreUI")


if __name__ == "__main__":
    unittest.main()
