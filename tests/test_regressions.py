"""Tests for specific regressions found via third-party repo validation.

Each test targets a concrete bug that was discovered and fixed, to prevent
re-introduction.
"""

import os
import shutil
import struct
import tempfile
import unittest

from PIL import Image

from actool.catalog import load_image_as_bgra
from actool.compiler import compile_catalog
from tests.helpers import (
    make_temp_catalog, parse_car_info, parse_car_csi_by_name,
    has_extract_pixels, extract_car_image,
)


def _make_catalog_with_image(tmpdir, name, img):
    """Create an xcassets catalog containing a single imageset.

    img: PIL.Image (used as 1x; 2x is created by doubling).
    """
    import json
    catalog = os.path.join(tmpdir, "Test.xcassets")
    os.makedirs(catalog, exist_ok=True)
    with open(os.path.join(catalog, "Contents.json"), "w") as f:
        json.dump({"info": {"author": "xcode", "version": 1}}, f)

    iset = os.path.join(catalog, f"{name}.imageset")
    os.makedirs(iset)
    img.save(os.path.join(iset, f"{name}.png"))
    img2x = img.resize((img.width * 2, img.height * 2), Image.NEAREST)
    img2x.save(os.path.join(iset, f"{name}@2x.png"))
    with open(os.path.join(iset, "Contents.json"), "w") as f:
        json.dump({
            "images": [
                {"filename": f"{name}.png", "idiom": "mac", "scale": "1x"},
                {"filename": f"{name}@2x.png", "idiom": "mac", "scale": "2x"},
            ],
            "info": {"author": "xcode", "version": 1},
        }, f)
    return catalog


class TestPremultipliedAlpha(unittest.TestCase):
    """Pixel data must be stored with premultiplied alpha.

    Regression: straight alpha was stored, causing semi-transparent pixels
    to render with incorrect colours (e.g. R=255,A=2 instead of R=2,A=2).
    """

    def test_load_image_premultiplies_bgra(self):
        """load_image_as_bgra returns premultiplied BGRA data."""
        img = Image.new("RGBA", (4, 4), (255, 0, 0, 2))
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            img.save(f.name)
            data, w, h, fmt = load_image_as_bgra(f.name)

        self.assertEqual(fmt, b"BGRA")
        # BGRA order: B, G, R, A
        b, g, r, a = data[0], data[1], data[2], data[3]
        self.assertEqual(a, 2)
        # Premultiplied: R_pm = 255 * 2 / 255 = 2 (with rounding)
        self.assertAlmostEqual(r, 2, delta=1)
        self.assertEqual(b, 0)
        self.assertEqual(g, 0)

    def test_load_image_premultiplies_ga8(self):
        """load_image_as_bgra returns premultiplied GA8 data."""
        img = Image.new("LA", (4, 4), (200, 10))
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            img.save(f.name)
            data, w, h, fmt = load_image_as_bgra(f.name)

        self.assertEqual(fmt, b" 8AG")
        # GA8 order: gray, alpha
        gray, alpha = data[0], data[1]
        self.assertEqual(alpha, 10)
        # Premultiplied: 200 * 10 / 255 ≈ 8
        self.assertAlmostEqual(gray, 8, delta=1)

    def test_fully_opaque_unchanged(self):
        """Fully opaque pixels are not modified by premultiplication."""
        img = Image.new("RGBA", (2, 2), (100, 150, 200, 255))
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            img.save(f.name)
            data, w, h, fmt = load_image_as_bgra(f.name)

        # BGRA: B=200, G=150, R=100, A=255
        self.assertEqual(data[0], 200)
        self.assertEqual(data[1], 150)
        self.assertEqual(data[2], 100)
        self.assertEqual(data[3], 255)

    def test_fully_transparent_zeroed(self):
        """Fully transparent pixels have all channels zeroed."""
        img = Image.new("RGBA", (2, 2), (255, 128, 64, 0))
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            img.save(f.name)
            data, w, h, fmt = load_image_as_bgra(f.name)

        self.assertEqual(data[0:4], b"\x00\x00\x00\x00")

    @unittest.skipUnless(has_extract_pixels(), "extract_pixels not built")
    def test_roundtrip_premultiplied(self):
        """Semi-transparent image roundtrips through CAR with correct alpha.

        The extracted pixel must have both colour and alpha premultiplied,
        not colour-only with alpha=255 (the old bug) or straight alpha.
        """
        tmpdir = tempfile.mkdtemp(prefix="actool_pma_")
        try:
            img = Image.new("RGBA", (16, 16), (255, 0, 0, 10))
            catalog = _make_catalog_with_image(tmpdir, "SemiRed", img)
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", "11.0")

            car = os.path.join(outdir, "Assets.car")
            ext = os.path.join(tmpdir, "ext")
            os.makedirs(ext)
            result = extract_car_image(car, "SemiRed", ext)
            self.assertIn(1, result)
            w, h, pixels = result[1]
            # RGBA from extract_pixels (premultiplied last)
            r, g, b, a = pixels[0], pixels[1], pixels[2], pixels[3]
            self.assertEqual(a, 10, f"alpha should be 10, got {a}")
            self.assertAlmostEqual(r, 10, delta=2,
                                   msg=f"premultiplied R should be ~10, got {r}")
        finally:
            shutil.rmtree(tmpdir)


class TestNoPackingPreLzfse(unittest.TestCase):
    """Pre-10.11 targets must not use atlas packing.

    Regression: our tool packed images into atlases for all deployment
    targets, but the system actool only packs for >= 10.11.
    """

    def test_pre_1011_all_inline(self):
        """macOS 10.9 target: all images stored inline, no packed atlases."""
        tmpdir = tempfile.mkdtemp(prefix="actool_nopack_")
        try:
            catalog, _ = make_temp_catalog(
                [("A", "RGBA"), ("B", "RGBA"), ("C", "RGBA")], tmpdir)
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", "10.9")
            info = parse_car_info(os.path.join(outdir, "Assets.car"))
            lc = info["layout_counts"]
            self.assertEqual(lc.get(1003, 0), 0,
                             "No packed refs for pre-10.11")
            self.assertEqual(lc.get(1004, 0), 0,
                             "No packed atlases for pre-10.11")
            self.assertGreater(lc.get(12, 0), 0,
                               "Images should be inline")
        finally:
            shutil.rmtree(tmpdir)

    def test_1011_uses_packing(self):
        """macOS 10.11 target: images are packed into atlases."""
        tmpdir = tempfile.mkdtemp(prefix="actool_pack_")
        try:
            catalog, _ = make_temp_catalog(
                [("A", "RGBA"), ("B", "RGBA"), ("C", "RGBA")], tmpdir)
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", "10.11")
            info = parse_car_info(os.path.join(outdir, "Assets.car"))
            lc = info["layout_counts"]
            self.assertGreater(lc.get(1003, 0), 0,
                               "Should have packed refs for 10.11")
            self.assertGreater(lc.get(1004, 0), 0,
                               "Should have packed atlases for 10.11")
        finally:
            shutil.rmtree(tmpdir)


class TestForceBgraPreLzfse(unittest.TestCase):
    """Pre-10.11 targets must store grayscale images as BGRA, not GA8.

    Regression: our tool stored grayscale-compatible RGBA images as GA8
    for all targets, but the system actool only uses GA8 with packing.
    """

    def test_pre_1011_grayscale_stored_as_bgra(self):
        """macOS 10.9: grayscale+alpha image stored as BGRA."""
        tmpdir = tempfile.mkdtemp(prefix="actool_bgra_")
        try:
            # Create a grayscale-compatible RGBA image (R==G==B)
            img = Image.new("LA", (16, 16), (128, 200))
            catalog = _make_catalog_with_image(tmpdir, "Gray", img)
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", "10.9")

            csi = parse_car_csi_by_name(os.path.join(outdir, "Assets.car"))
            entry = csi["Gray.png"][0]
            self.assertEqual(entry["pixel_format"], b"BGRA",
                             "Pre-10.11 grayscale should be BGRA")
        finally:
            shutil.rmtree(tmpdir)

    def test_1011_grayscale_stored_as_ga8(self):
        """macOS 11.0: grayscale+alpha image stored as GA8."""
        tmpdir = tempfile.mkdtemp(prefix="actool_ga8_")
        try:
            catalog, _ = make_temp_catalog(
                [("X", "LA"), ("Y", "LA"), ("Z", "LA")], tmpdir)
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", "11.0")

            csi = parse_car_csi_by_name(os.path.join(outdir, "Assets.car"))
            # Packed ref should be GA8
            entry = csi["X.png"][0]
            self.assertEqual(entry["pixel_format"], b" 8AG",
                             "11.0 grayscale should be GA8")
        finally:
            shutil.rmtree(tmpdir)


class TestInlineBytesPerRow(unittest.TestCase):
    """Inline images must use exact width*4 bpr, not 32-byte aligned.

    Regression: all images used 32-byte aligned bpr, but the system actool
    only aligns bpr for packed atlas data. Inline images use width*4.
    """

    def _get_bpr(self, car_path, name):
        """Extract BytesPerRow TLV value for a named rendition."""
        csi = parse_car_csi_by_name(car_path)
        for entry in csi.get(name, []):
            bpr_tlv = entry["tlvs"].get(0x03EF)
            if bpr_tlv and len(bpr_tlv) >= 4:
                return struct.unpack_from("<I", bpr_tlv, 0)[0]
        return None

    def test_inline_bpr_not_aligned(self):
        """Inline image with non-power-of-2 width uses exact bpr."""
        tmpdir = tempfile.mkdtemp(prefix="actool_bpr_")
        try:
            # Width=14 → exact bpr=56, aligned would be 64
            img = Image.new("RGBA", (14, 14), (100, 100, 100, 255))
            catalog = _make_catalog_with_image(tmpdir, "Odd", img)
            outdir = os.path.join(tmpdir, "out")
            # Use 10.9 to get all-inline (no packing)
            compile_catalog(catalog, outdir, "macosx", "10.9")

            bpr = self._get_bpr(os.path.join(outdir, "Assets.car"), "Odd.png")
            self.assertEqual(bpr, 14 * 4,
                             f"Inline bpr should be width*4=56, got {bpr}")
        finally:
            shutil.rmtree(tmpdir)

    def test_inline_bpr_various_widths(self):
        """Several non-aligned widths all use exact bpr."""
        tmpdir = tempfile.mkdtemp(prefix="actool_bpr_")
        try:
            import json
            catalog = os.path.join(tmpdir, "Test.xcassets")
            os.makedirs(catalog)
            with open(os.path.join(catalog, "Contents.json"), "w") as f:
                json.dump({"info": {"author": "xcode", "version": 1}}, f)

            test_widths = {"W6": 6, "W10": 10, "W18": 18}
            for name, w in test_widths.items():
                iset = os.path.join(catalog, f"{name}.imageset")
                os.makedirs(iset)
                Image.new("RGBA", (w, 8), (50, 50, 50, 255)).save(
                    os.path.join(iset, f"{name}.png"))
                Image.new("RGBA", (w * 2, 16), (50, 50, 50, 255)).save(
                    os.path.join(iset, f"{name}@2x.png"))
                with open(os.path.join(iset, "Contents.json"), "w") as f:
                    json.dump({
                        "images": [
                            {"filename": f"{name}.png", "idiom": "mac",
                             "scale": "1x"},
                            {"filename": f"{name}@2x.png", "idiom": "mac",
                             "scale": "2x"},
                        ],
                        "info": {"author": "xcode", "version": 1},
                    }, f)

            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", "10.9")
            car = os.path.join(outdir, "Assets.car")

            for name, w in test_widths.items():
                bpr = self._get_bpr(car, f"{name}.png")
                expected = w * 4
                self.assertEqual(bpr, expected,
                                 f"{name} (w={w}): bpr={bpr}, "
                                 f"expected {expected}")
        finally:
            shutil.rmtree(tmpdir)

    def test_atlas_bpr_is_aligned(self):
        """Packed atlas BytesPerRow uses 32-byte alignment."""
        tmpdir = tempfile.mkdtemp(prefix="actool_bpr_")
        try:
            # 3 images → packed atlas. Width 14 in atlas should align.
            catalog, _ = make_temp_catalog(
                [("A", "RGBA"), ("B", "RGBA"), ("C", "RGBA")], tmpdir)
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", "11.0")

            csi = parse_car_csi_by_name(os.path.join(outdir, "Assets.car"))
            for name, entries in csi.items():
                for entry in entries:
                    if entry["layout"] == 1004:  # packed atlas
                        bpr_tlv = entry["tlvs"].get(0x03EF)
                        if bpr_tlv:
                            bpr = struct.unpack_from("<I", bpr_tlv, 0)[0]
                            self.assertEqual(bpr % 32, 0,
                                             f"Atlas {name} bpr={bpr} "
                                             f"not 32-byte aligned")
        finally:
            shutil.rmtree(tmpdir)


def _make_catalog_with_pdf(tmpdir, imagesets):
    """Create an xcassets catalog with PDF and/or PNG imagesets.

    imagesets: list of (name, ext) where ext is 'pdf' or 'png'.
    For pdf: creates a minimal valid PDF file.
    For png: creates a small RGBA image.
    """
    import json
    catalog = os.path.join(tmpdir, "Test.xcassets")
    os.makedirs(catalog, exist_ok=True)
    with open(os.path.join(catalog, "Contents.json"), "w") as f:
        json.dump({"info": {"author": "xcode", "version": 1}}, f)

    # Minimal valid PDF
    pdf_bytes = (
        b"%PDF-1.0\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 16 16]/Parent 2 0 R>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
        b"0000000058 00000 n \n0000000115 00000 n \n"
        b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n190\n%%EOF\n"
    )

    for name, ext in imagesets:
        iset = os.path.join(catalog, f"{name}.imageset")
        os.makedirs(iset)
        filename = f"{name}.{ext}"
        if ext == "pdf":
            with open(os.path.join(iset, filename), "wb") as f:
                f.write(pdf_bytes)
            imgs = [{"filename": filename, "idiom": "universal"}]
        else:
            Image.new("RGBA", (16, 16), (100, 50, 25, 255)).save(
                os.path.join(iset, filename))
            Image.new("RGBA", (32, 32), (100, 50, 25, 255)).save(
                os.path.join(iset, f"{name}@2x.{ext}"))
            imgs = [
                {"filename": filename, "idiom": "mac", "scale": "1x"},
                {"filename": f"{name}@2x.{ext}", "idiom": "mac",
                 "scale": "2x"},
            ]
        with open(os.path.join(iset, "Contents.json"), "w") as f:
            json.dump({
                "images": imgs,
                "info": {"author": "xcode", "version": 1},
            }, f)

    return catalog


class TestPdfImagesets(unittest.TestCase):
    """PDF images in imagesets must not crash the compiler.

    Regression: PIL.Image.open() on a PDF file raised
    UnidentifiedImageError, causing the entire catalog compilation to
    fail with no output.
    """

    def test_pdf_imageset_compiles(self):
        """Catalog with a PDF imageset produces an Assets.car."""
        tmpdir = tempfile.mkdtemp(prefix="actool_pdf_")
        try:
            catalog = _make_catalog_with_pdf(tmpdir, [("Icon", "pdf")])
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", "11.0")
            self.assertTrue(
                os.path.isfile(os.path.join(outdir, "Assets.car")),
                "Assets.car should be produced for PDF imagesets")
        finally:
            shutil.rmtree(tmpdir)

    def test_mixed_pdf_and_png_compiles(self):
        """Catalog with both PDF and PNG imagesets compiles fully."""
        tmpdir = tempfile.mkdtemp(prefix="actool_mix_")
        try:
            catalog = _make_catalog_with_pdf(
                tmpdir, [("Vec", "pdf"), ("Raster", "png")])
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", "11.0")
            car = os.path.join(outdir, "Assets.car")
            self.assertTrue(os.path.isfile(car))
            csi = parse_car_csi_by_name(car)
            # PNG images should still be present and valid
            self.assertIn("Raster.png", csi)
        finally:
            shutil.rmtree(tmpdir)

    def test_pdf_stored_as_layout_9(self):
        """PDF imageset creates a layout-9 rendition with PDF pixel format."""
        tmpdir = tempfile.mkdtemp(prefix="actool_pdf9_")
        try:
            catalog = _make_catalog_with_pdf(tmpdir, [("Icon", "pdf")])
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", "11.0")
            csi = parse_car_csi_by_name(os.path.join(outdir, "Assets.car"))
            entries = csi.get("Icon.pdf", [])
            self.assertTrue(len(entries) > 0, "PDF rendition should exist")
            pdf_entry = entries[0]
            self.assertEqual(pdf_entry["layout"], 9,
                             "PDF should use layout 9")
            self.assertEqual(pdf_entry["pixel_format"], b" FDP",
                             "PDF should use ' FDP' pixel format")
        finally:
            shutil.rmtree(tmpdir)

    def test_pdf_colorspace_zero(self):
        """PDF renditions must have colorspace_id=0."""
        tmpdir = tempfile.mkdtemp(prefix="actool_pdfcs_")
        try:
            catalog = _make_catalog_with_pdf(tmpdir, [("Icon", "pdf")])
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", "11.0")
            csi = parse_car_csi_by_name(os.path.join(outdir, "Assets.car"))
            pdf_entry = csi["Icon.pdf"][0]
            self.assertEqual(pdf_entry["cs"], 0,
                             "PDF rendition colorspace should be 0")
        finally:
            shutil.rmtree(tmpdir)

    def test_pdf_data_preserved(self):
        """The raw PDF data is preserved inside the RAWD wrapper."""
        tmpdir = tempfile.mkdtemp(prefix="actool_pdfraw_")
        try:
            catalog = _make_catalog_with_pdf(tmpdir, [("Icon", "pdf")])
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", "11.0")
            csi = parse_car_csi_by_name(os.path.join(outdir, "Assets.car"))
            rend_data = csi["Icon.pdf"][0]["rend"]
            # RAWD header: "DWAR" + ver(4) + len(4) + data
            self.assertEqual(rend_data[:4], b"DWAR")
            data_len = struct.unpack_from("<I", rend_data, 8)[0]
            pdf_data = rend_data[12:12 + data_len]
            self.assertTrue(pdf_data.startswith(b"%PDF"),
                            "RAWD payload should contain the PDF")
        finally:
            shutil.rmtree(tmpdir)
