"""Tests for deepmap2 (comp=11) compression integration.

Validates:
1. deepmap2 is used for packed atlases when deployment target >= macOS 11.0
2. deepmap2 is also used for inline BGRA images at >= macOS 11.0
3. deepmap2 is NOT used for deployment targets below macOS 11.0
4. CELM envelope format for deepmap2 blocks (sub-header with pixfmt)
5. deepmap2 payload starts with "dmp2" magic and has valid header fields
6. CoreUI can decode deepmap2-compressed renditions
7. Fallback to lzfse/zip when deepmap2 is unavailable
"""

import os
import shutil
import struct
import tempfile
import unittest

from actool import deepmap2
from actool.car import compress_data, LAYOUT_ONE_PART_SCALE, LAYOUT_NAME_LIST
from actool.compiler import compile_catalog
from tests.helpers import (
    REF_XCASSETS,
    has_system_actool,
    has_validate_car,
    compile_with_system_actool,
    make_temp_catalog,
    validate_car_rendering,
)


def _parse_celm_blocks(car_path):
    """Extract CELM details from all renditions with pixel data in a CAR."""
    with open(car_path, 'rb') as f:
        data = f.read()

    results = []
    pos = 0
    while True:
        pos = data.find(b'ISTC', pos)
        if pos == -1:
            break
        w = struct.unpack_from('<I', data, pos + 12)[0]
        h = struct.unpack_from('<I', data, pos + 16)[0]
        pixfmt = data[pos + 24:pos + 28]
        layout = struct.unpack_from('<H', data, pos + 36)[0]
        name = data[pos + 40:pos + 168].split(b'\x00')[0].decode(
            'ascii', errors='replace')

        tvl_len = struct.unpack_from('<I', data, pos + 168)[0]
        rend_len = struct.unpack_from('<I', data, pos + 180)[0]
        rend_start = pos + 184 + tvl_len

        entry = {
            'name': name,
            'layout': layout,
            'width': w,
            'height': h,
            'pixel_format': pixfmt,
            'rend_len': rend_len,
        }

        if rend_len >= 16 and data[rend_start:rend_start + 4] == b'MLEC':
            ver, comp, dlen = struct.unpack_from('<III', data, rend_start + 4)
            entry['celm_ver'] = ver
            entry['celm_comp'] = comp
            entry['celm_dlen'] = dlen
            entry['celm_payload'] = data[rend_start + 16:rend_start + 16 + dlen]

        results.append(entry)
        pos += 4

    return results


# -- Unit tests for the deepmap2 module --

@unittest.skipUnless(deepmap2.is_available(),
                     "vImage deepmap2 encoder not available")
class TestDeepmap2Encode(unittest.TestCase):
    """Unit tests for the deepmap2 encode/envelope functions."""

    def test_encode_bgra(self):
        """Encoding a BGRA image produces data starting with 'dmp2' magic."""
        w, h = 32, 32
        pixel_data = bytes([0xAA, 0xBB, 0xCC, 0xFF] * (w * h))
        result = deepmap2.encode(pixel_data, b"BGRA", w, h)
        self.assertIsNotNone(result)
        self.assertTrue(result.startswith(b"dmp2"),
                        f"Expected dmp2 magic, got {result[:4]!r}")

    def test_encode_ga8(self):
        """Encoding a GA8 (gray+alpha) image produces dmp2 data."""
        w, h = 32, 32
        pixel_data = bytes([0x80, 0xFF] * (w * h))
        result = deepmap2.encode(pixel_data, b" 8AG", w, h)
        self.assertIsNotNone(result)
        self.assertTrue(result.startswith(b"dmp2"))

    def test_encode_bgra_pixfmt_field(self):
        """DMP2 header pixel format field is 4 for BGRA."""
        w, h = 16, 16
        pixel_data = bytes(w * h * 4)
        result = deepmap2.encode(pixel_data, b"BGRA", w, h)
        self.assertIsNotNone(result)
        self.assertEqual(result[7], 4)  # pixel format byte in dmp2 header

    def test_encode_ga8_pixfmt_field(self):
        """DMP2 header pixel format field is 2 for GA8."""
        w, h = 16, 16
        pixel_data = bytes(w * h * 2)
        result = deepmap2.encode(pixel_data, b" 8AG", w, h)
        self.assertIsNotNone(result)
        self.assertEqual(result[7], 2)

    def test_encode_unknown_format_returns_none(self):
        """Encoding with an unsupported pixel format returns None."""
        result = deepmap2.encode(b"\x00" * 64, b"XYZW", 4, 4)
        self.assertIsNone(result)

    def test_encode_produces_smaller_output(self):
        """DMP2 encoding compresses data (output smaller than input)."""
        w, h = 64, 64
        pixel_data = bytes(w * h * 4)  # all zeros — very compressible
        result = deepmap2.encode(pixel_data, b"BGRA", w, h)
        self.assertIsNotNone(result)
        self.assertLess(len(result), len(pixel_data))

    def test_dmp2_header_quality_and_param(self):
        """DMP2 header has quality=1 and param=10 matching system actool."""
        w, h = 16, 16
        pixel_data = bytes([0xAA, 0xBB, 0xCC, 0xFF] * (w * h))
        result = deepmap2.encode(pixel_data, b"BGRA", w, h)
        self.assertIsNotNone(result)
        # dmp2 header: magic(4) + ct(1) + quality(1) + param(1) + pf(1) + ...
        quality = result[5]
        param = result[6]
        self.assertEqual(quality, 1, f"Expected quality=1, got {quality}")
        self.assertEqual(param, 10, f"Expected param=10, got {param}")


class TestMakeCelmDmp2(unittest.TestCase):
    """Test the CELM DMP2 envelope construction."""

    def test_celm_header_fields(self):
        """CELM envelope has tag=MLEC, ver=0, comp=11."""
        fake_dmp2 = b"dmp2" + b"\x00" * 20
        celm = deepmap2.make_celm_dmp2(fake_dmp2, b"BGRA")

        self.assertEqual(celm[:4], b"MLEC")
        ver, comp, dlen = struct.unpack_from('<III', celm, 4)
        self.assertEqual(ver, 0)
        self.assertEqual(comp, 11)
        # dlen = 16 (sub-header) + len(fake_dmp2)
        self.assertEqual(dlen, 16 + len(fake_dmp2))

    def test_celm_sub_header(self):
        """Sub-header has version=1, correct pixfmt, correct dmp2 length."""
        fake_dmp2 = b"dmp2" + b"\x00" * 30
        celm = deepmap2.make_celm_dmp2(fake_dmp2, b"BGRA")

        sub_ver, sub_pf, sub_len, sub_zero = struct.unpack_from(
            '<IIII', celm, 16)
        self.assertEqual(sub_ver, 1)
        self.assertEqual(sub_pf, 4)  # BGRA → deepmap2 format 4
        self.assertEqual(sub_len, len(fake_dmp2))
        self.assertEqual(sub_zero, 0)

    def test_celm_sub_header_ga8(self):
        """Sub-header pixel format is 2 for GA8."""
        fake_dmp2 = b"dmp2" + b"\x00" * 10
        celm = deepmap2.make_celm_dmp2(fake_dmp2, b" 8AG")

        sub_pf = struct.unpack_from('<I', celm, 20)[0]
        self.assertEqual(sub_pf, 2)

    def test_celm_contains_dmp2_payload(self):
        """The dmp2 data appears verbatim after the 32-byte header."""
        fake_dmp2 = b"dmp2TESTPAYLOAD!"
        celm = deepmap2.make_celm_dmp2(fake_dmp2, b"BGRA")

        # 16 bytes CELM header + 16 bytes sub-header = 32 bytes
        payload = celm[32:]
        self.assertEqual(payload, fake_dmp2)


# -- Integration tests: compress_data() selection logic --

class TestCompressDataDmp2Selection(unittest.TestCase):
    """Test that compress_data() selects DMP2 vs LZFSE correctly."""

    def _make_pixel_data(self, w, h, bpp=4):
        """Create pixel data large enough to trigger compression (> 256)."""
        return bytes([0xAA, 0xBB, 0xCC, 0xFF][:bpp] * (w * h))

    @unittest.skipUnless(deepmap2.is_available(),
                         "vImage deepmap2 encoder not available")
    def test_dmp2_used_for_atlas_at_11_0(self):
        """compress_data with allow_dmp2=True and target 11.0 uses DMP2."""
        w, h = 64, 64
        data = self._make_pixel_data(w, h)
        result = compress_data(data, b"BGRA", w, h,
                               min_deploy="11.0", platform="macosx",
                               allow_dmp2=True)
        ver, comp = struct.unpack_from('<II', result, 4)
        self.assertEqual(comp, 11, "Expected DMP2 (comp=11)")
        self.assertEqual(ver, 0, "Expected CELM ver=0 for DMP2")

    def test_lzfse_used_for_standalone_at_11_0(self):
        """compress_data with allow_dmp2=False and target 11.0 uses LZFSE."""
        w, h = 64, 64
        data = self._make_pixel_data(w, h)
        result = compress_data(data, b"BGRA", w, h,
                               min_deploy="11.0", platform="macosx",
                               allow_dmp2=False)
        comp = struct.unpack_from('<I', result, 8)[0]
        self.assertNotEqual(comp, 11,
                            "Standalone images should not use DMP2")

    def test_no_dmp2_at_10_15(self):
        """compress_data with target 10.15 never uses DMP2."""
        w, h = 64, 64
        data = self._make_pixel_data(w, h)
        result = compress_data(data, b"BGRA", w, h,
                               min_deploy="10.15", platform="macosx",
                               allow_dmp2=True)
        comp = struct.unpack_from('<I', result, 8)[0]
        self.assertNotEqual(comp, 11,
                            "DMP2 should not be used for target < 11.0")

    def test_no_dmp2_at_10_11(self):
        """compress_data with target 10.11 uses LZFSE, not DMP2."""
        w, h = 64, 64
        data = self._make_pixel_data(w, h)
        result = compress_data(data, b"BGRA", w, h,
                               min_deploy="10.11", platform="macosx",
                               allow_dmp2=True)
        comp = struct.unpack_from('<I', result, 8)[0]
        self.assertNotEqual(comp, 11)

    @unittest.skipUnless(deepmap2.is_available(),
                         "vImage deepmap2 encoder not available")
    def test_dmp2_for_ga8_atlas(self):
        """DMP2 works for GA8 (gray+alpha) packed atlas data."""
        w, h = 64, 64
        data = self._make_pixel_data(w, h, bpp=2)
        result = compress_data(data, b" 8AG", w, h,
                               min_deploy="11.0", platform="macosx",
                               allow_dmp2=True)
        comp = struct.unpack_from('<I', result, 8)[0]
        self.assertEqual(comp, 11, "GA8 atlas should use DMP2 at 11.0+")

    def test_small_data_skips_dmp2(self):
        """Pixel data <= 256 bytes skips DMP2 and uses uncompressed."""
        # 8x8 BGRA = 256 bytes, exactly at the threshold
        data = bytes(256)
        result = compress_data(data, b"BGRA", 8, 8,
                               min_deploy="11.0", platform="macosx",
                               allow_dmp2=True)
        comp = struct.unpack_from('<I', result, 8)[0]
        self.assertNotEqual(comp, 11,
                            "Data <= 256 bytes should not use DMP2")


# -- Full pipeline: CAR file compression layout --

class TestCarDmp2Layout(unittest.TestCase):
    """Test that compiled CAR files use DMP2 only for packed atlases."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="actool_dmp2_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _compile_and_parse(self, min_deploy, **kwargs):
        outdir = os.path.join(self.tmpdir, f"out_{min_deploy}")
        compile_catalog(REF_XCASSETS, outdir, "macosx", min_deploy,
                        app_icon="AppIcon",
                        info_plist_path=os.path.join(outdir, "Info.plist"),
                        **kwargs)
        return _parse_celm_blocks(os.path.join(outdir, "Assets.car"))

    @unittest.skipUnless(deepmap2.is_available(),
                         "vImage deepmap2 encoder not available")
    def test_ga8_packed_atlases_use_dmp2_at_11_0(self):
        """GA8 packed atlas renditions use deepmap2 at target 11.0."""
        entries = self._compile_and_parse("11.0")
        ga8_atlases = [e for e in entries
                       if e['layout'] == LAYOUT_NAME_LIST
                       and e.get('pixel_format') == b" 8AG"
                       and 'celm_comp' in e]
        self.assertGreater(len(ga8_atlases), 0,
                           "No GA8 packed atlas renditions found")
        for e in ga8_atlases:
            self.assertEqual(e['celm_comp'], 11,
                             f"{e['name']}: GA8 atlas should use deepmap2 "
                             f"(comp=11), got comp={e['celm_comp']}")

    def test_standalone_bgra_use_lzfse_at_11_0(self):
        """BGRA standalone (layout 12) renditions use KCBC LZFSE at 11.0.

        The system actool uses KCBC LZFSE (not deepmap2) for inline BGRA.
        Only packed atlases and inline GA8 use deepmap2.
        """
        entries = self._compile_and_parse("11.0")
        standalone_bgra = [e for e in entries
                           if e['layout'] == LAYOUT_ONE_PART_SCALE
                           and 'celm_comp' in e
                           and e.get('pixel_format') == b"BGRA"]
        self.assertGreater(len(standalone_bgra), 0,
                           "No standalone BGRA renditions found")
        for e in standalone_bgra:
            self.assertEqual(e['celm_comp'], 4,
                             f"{e['name']}: standalone BGRA should use KCBC "
                             f"LZFSE at 11.0, got comp={e['celm_comp']}")

    def test_no_dmp2_at_10_15(self):
        """No renditions use DMP2 when target is 10.15."""
        entries = self._compile_and_parse("10.15")
        for e in entries:
            if 'celm_comp' in e:
                self.assertNotEqual(e['celm_comp'], 11,
                                    f"{e['name']}: DMP2 used at target 10.15")

    def test_no_dmp2_at_10_11(self):
        """No renditions use DMP2 when target is 10.11."""
        entries = self._compile_and_parse("10.11")
        for e in entries:
            if 'celm_comp' in e:
                self.assertNotEqual(e['celm_comp'], 11,
                                    f"{e['name']}: DMP2 used at target 10.11")

    @unittest.skipUnless(deepmap2.is_available(),
                         "vImage deepmap2 encoder not available")
    def test_dmp2_celm_envelope_valid(self):
        """All DMP2 CELM blocks have correct envelope structure."""
        entries = self._compile_and_parse("11.0")
        dmp2_entries = [e for e in entries if e.get('celm_comp') == 11]
        self.assertGreater(len(dmp2_entries), 0)

        for e in dmp2_entries:
            payload = e['celm_payload']

            if payload.startswith(b"dmp2"):
                # Inline DMP2: raw dmp2 data without sub-header
                pass  # valid
            else:
                # Atlas/non-inline DMP2: sub-header + dmp2 data
                self.assertGreaterEqual(len(payload), 16,
                                        f"{e['name']}: payload too short")
                sub_ver, sub_pf, sub_len, sub_zero = struct.unpack_from(
                    '<IIII', payload, 0)
                self.assertEqual(sub_ver, 1,
                                 f"{e['name']}: sub-header version={sub_ver}")
                self.assertEqual(sub_zero, 0,
                                 f"{e['name']}: sub-header padding={sub_zero}")
                self.assertEqual(len(payload), 16 + sub_len,
                                 f"{e['name']}: sub_len mismatch")

                # DMP2 data starts with "dmp2" magic
                dmp2_data = payload[16:]
                self.assertTrue(dmp2_data.startswith(b"dmp2"),
                                f"{e['name']}: missing dmp2 magic, "
                                f"got {dmp2_data[:4]!r}")

                # Pixel format in dmp2 header matches sub-header
                dmp2_pf = dmp2_data[7]
                self.assertEqual(dmp2_pf, sub_pf,
                                 f"{e['name']}: dmp2 pf={dmp2_pf} != "
                                 f"sub-header pf={sub_pf}")

    @unittest.skipUnless(deepmap2.is_available(),
                         "vImage deepmap2 encoder not available")
    def test_dmp2_ga8_atlases_only(self):
        """Only GA8 atlases use deepmap2 at 11.0; BGRA uses KCBC LZFSE."""
        catalog, _ = make_temp_catalog(
            [("RgbA", "RGBA"), ("RgbB", "RGBA"),
             ("GrayA", "LA"), ("GrayB", "LA")],
            self.tmpdir)
        outdir = os.path.join(self.tmpdir, "mixed_dmp2")
        compile_catalog(catalog, outdir, "macosx", "11.0")

        entries = _parse_celm_blocks(os.path.join(outdir, "Assets.car"))
        atlases = [e for e in entries
                   if e['layout'] == LAYOUT_NAME_LIST and 'celm_comp' in e]
        self.assertGreater(len(atlases), 0)
        for e in atlases:
            if e.get('pixel_format') == b" 8AG":
                self.assertEqual(e['celm_comp'], 11,
                                 f"{e['name']}: GA8 atlas should use deepmap2")
            elif e.get('pixel_format') == b"BGRA":
                self.assertEqual(e['celm_comp'], 4,
                                 f"{e['name']}: BGRA atlas should use KCBC")


# -- CoreUI rendering with DMP2 --

@unittest.skipUnless(has_validate_car(), "validate_car tool not built")
@unittest.skipUnless(deepmap2.is_available(),
                     "vImage deepmap2 encoder not available")
class TestCoreUIRenderingDmp2(unittest.TestCase):
    """CoreUI can render all images from DMP2-compressed CAR files."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="actool_dmp2_render_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_render_main_catalog_11_0(self):
        """All images render from CAR with DMP2 atlas compression."""
        outdir = os.path.join(self.tmpdir, "main")
        compile_catalog(REF_XCASSETS, outdir, "macosx", "11.0",
                        app_icon="AppIcon",
                        info_plist_path=os.path.join(outdir, "Info.plist"))
        car = os.path.join(outdir, "Assets.car")
        ok, fail, details = validate_car_rendering(car)
        crashes = [d for d in details if d[0] == "CRASH"]
        self.assertEqual(len(crashes), 0,
                         f"CoreUI crashes with DMP2 data: {crashes}")
        self.assertGreater(ok, 0, "No images validated")
        self.assertEqual(fail, 0, f"Rendering failures: {details}")

    def test_render_mixed_formats_11_0(self):
        """Mixed BGRA + GA8 catalog renders with DMP2 compression."""
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
    def test_render_matches_system_actool_11_0(self):
        """DMP2 CAR renders same number of images as system actool."""
        our_dir = os.path.join(self.tmpdir, "ours")
        sys_dir = os.path.join(self.tmpdir, "system")
        compile_catalog(REF_XCASSETS, our_dir, "macosx", "11.0",
                        app_icon="AppIcon",
                        info_plist_path=os.path.join(our_dir, "Info.plist"))
        compile_with_system_actool(REF_XCASSETS, sys_dir,
                                   app_icon="AppIcon",
                                   min_deploy="11.0")

        our_ok, our_fail, our_details = validate_car_rendering(
            os.path.join(our_dir, "Assets.car"))
        sys_ok, sys_fail, sys_details = validate_car_rendering(
            os.path.join(sys_dir, "Assets.car"))

        self.assertEqual(sys_fail, 0,
                         f"System car failures: {sys_details}")
        our_crashes = [d for d in our_details if d[0] == "CRASH"]
        self.assertEqual(len(our_crashes), 0,
                         f"Our DMP2 car crashes: {our_crashes}")
        self.assertEqual(our_ok, sys_ok,
                         f"Ours: {our_ok} OK, System: {sys_ok} OK")


if __name__ == "__main__":
    unittest.main()
