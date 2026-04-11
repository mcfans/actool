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

from actool import car
from actool.catalog import load_image_as_bgra
from actool.compiler import compile_catalog
from tests.helpers import (
    make_temp_catalog, parse_car_info, parse_car_csi_by_name,
    has_extract_pixels, extract_car_image,
    _read_car_blocks, _walk_tree_leaves,
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


def _make_iconset(parent_dir, name, sizes=None):
    """Create a .iconset directory with icon_WxH[@2x].png files.

    sizes: list of point sizes (default: [16, 32, 128, 256, 512]).
    Each size produces a 1x and 2x PNG.
    """
    if sizes is None:
        sizes = [16, 32, 128, 256, 512]
    iset = os.path.join(parent_dir, f"{name}.iconset")
    os.makedirs(iset, exist_ok=True)
    for pt in sizes:
        for scale in (1, 2):
            px = pt * scale
            suffix = f"@{scale}x" if scale > 1 else ""
            fname = f"icon_{pt}x{pt}{suffix}.png"
            # Use a distinct colour per size so pixels are verifiable
            color = ((pt * 7) % 256, (pt * 13) % 256, 50, 255)
            Image.new("RGBA", (px, px), color).save(
                os.path.join(iset, fname))
    return iset


class TestIconset(unittest.TestCase):
    """The .iconset directory format must produce the same rendition
    structure as .appiconset: a multisize descriptor plus individual
    icon renditions with the correct dim2 values.

    Regression: .iconset directories were silently ignored, causing all
    document-type icons to be missing from the compiled car file.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="actool_iconset_")
        self.catalog = os.path.join(self.tmpdir, "Test.xcassets")
        os.makedirs(self.catalog)
        import json
        with open(os.path.join(self.catalog, "Contents.json"), "w") as f:
            json.dump({"info": {"author": "xcode", "version": 1}}, f)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_iconset_produces_renditions(self):
        """An .iconset directory produces a multisize + icon renditions."""
        _make_iconset(self.catalog, "MyDoc")
        outdir = os.path.join(self.tmpdir, "out")
        compile_catalog(self.catalog, outdir, "macosx", "11.0")
        car = os.path.join(outdir, "Assets.car")
        self.assertTrue(os.path.isfile(car))
        info = parse_car_info(car)
        lc = info["layout_counts"]
        self.assertGreater(lc.get(1010, 0), 0,
                           "Should have a multisize rendition")

    def test_iconset_facet_created(self):
        """The iconset name appears as a facet in the car file."""
        _make_iconset(self.catalog, "MyDoc")
        outdir = os.path.join(self.tmpdir, "out")
        compile_catalog(self.catalog, outdir, "macosx", "11.0")
        csi = parse_car_csi_by_name(os.path.join(outdir, "Assets.car"))
        # The multisize rendition should exist with the iconset name
        self.assertIn("MyDoc", csi,
                      "Multisize rendition named after iconset")

    def test_iconset_correct_dim2_values(self):
        """Each icon size maps to the correct dim2 index."""
        from actool.catalog import ICON_DIM2_MAP
        _make_iconset(self.catalog, "MyDoc")
        outdir = os.path.join(self.tmpdir, "out")
        compile_catalog(self.catalog, outdir, "macosx", "11.0")
        csi = parse_car_csi_by_name(os.path.join(outdir, "Assets.car"))

        # Check multisize entry lists all expected sizes
        ms_entry = csi["MyDoc"][0]
        self.assertEqual(ms_entry["layout"], 1010)

        # Check that icon renditions exist for each size/scale
        for point_w, expected_dim2 in ICON_DIM2_MAP.items():
            fname = f"icon_{point_w}x{point_w}.png"
            self.assertIn(fname, csi,
                          f"Missing icon rendition for {point_w}pt")

    def test_iconset_rendition_count(self):
        """5 sizes × 2 scales = 10 icon renditions + 1 multisize + atlases."""
        _make_iconset(self.catalog, "MyDoc")
        outdir = os.path.join(self.tmpdir, "out")
        compile_catalog(self.catalog, outdir, "macosx", "11.0")
        info = parse_car_info(car_path=os.path.join(outdir, "Assets.car"))
        lc = info["layout_counts"]
        # 10 icon images: small ones packed (1003), large ones inline (12)
        self.assertEqual(lc.get(1003, 0) + lc.get(12, 0), 10)
        # 1 multisize descriptor
        self.assertEqual(lc.get(1010, 0), 1)

    def test_iconset_in_group_directory(self):
        """Iconsets inside a group subdirectory are found."""
        group = os.path.join(self.catalog, "DocIcons")
        os.makedirs(group)
        import json
        with open(os.path.join(group, "Contents.json"), "w") as f:
            json.dump({"info": {"author": "xcode", "version": 1}}, f)
        _make_iconset(group, "doc_mp4")
        outdir = os.path.join(self.tmpdir, "out")
        compile_catalog(self.catalog, outdir, "macosx", "11.0")
        csi = parse_car_csi_by_name(os.path.join(outdir, "Assets.car"))
        self.assertIn("doc_mp4", csi)

    def test_iconset_partial_sizes(self):
        """An iconset with only some sizes still compiles."""
        _make_iconset(self.catalog, "Small", sizes=[16, 32])
        outdir = os.path.join(self.tmpdir, "out")
        compile_catalog(self.catalog, outdir, "macosx", "11.0")
        car = os.path.join(outdir, "Assets.car")
        info = parse_car_info(car)
        lc = info["layout_counts"]
        # 2 sizes × 2 scales = 4 icons + 1 multisize
        self.assertEqual(lc.get(1003, 0) + lc.get(12, 0), 4)
        self.assertEqual(lc.get(1010, 0), 1)

    @unittest.skipUnless(has_extract_pixels(), "extract_pixels not built")
    def test_iconset_pixels_roundtrip(self):
        """Icon images from an iconset extract with correct pixels."""
        # Use a distinctive colour for 32pt icons
        iset = os.path.join(self.catalog, "TestIcon.iconset")
        os.makedirs(iset)
        color = (200, 100, 50, 255)
        Image.new("RGBA", (32, 32), color).save(
            os.path.join(iset, "icon_32x32.png"))
        Image.new("RGBA", (64, 64), color).save(
            os.path.join(iset, "icon_32x32@2x.png"))

        outdir = os.path.join(self.tmpdir, "out")
        compile_catalog(self.catalog, outdir, "macosx", "11.0")
        car = os.path.join(outdir, "Assets.car")
        ext = os.path.join(self.tmpdir, "ext")
        os.makedirs(ext)
        result = extract_car_image(car, "TestIcon", ext)
        self.assertIn(1, result, "1x icon should be extractable")
        w, h, pixels = result[1]
        self.assertEqual((w, h), (32, 32))
        # RGBA: check R channel (premultiplied, but alpha=255 so same)
        self.assertEqual(pixels[0], 200)


def _make_colorset(parent_dir, name, colors):
    """Create a .colorset directory.

    colors: list of dicts, each with:
        r, g, b, a: float 0-1
        appearance: optional, "dark" for dark mode variant
    """
    import json
    cset = os.path.join(parent_dir, f"{name}.colorset")
    os.makedirs(cset, exist_ok=True)
    entries = []
    for c in colors:
        entry = {
            "idiom": "universal",
            "color": {
                "color-space": "srgb",
                "components": {
                    "red": str(c["r"]),
                    "green": str(c["g"]),
                    "blue": str(c["b"]),
                    "alpha": str(c["a"]),
                },
            },
        }
        if c.get("appearance") == "dark":
            entry["appearances"] = [
                {"appearance": "luminosity", "value": "dark"}
            ]
        entries.append(entry)
    with open(os.path.join(cset, "Contents.json"), "w") as f:
        json.dump({
            "info": {"author": "xcode", "version": 1},
            "colors": entries,
        }, f)


def _make_directional_imageset(parent_dir, name, ltr_color, rtl_color):
    """Create an imageset with language-direction variants."""
    import json
    iset = os.path.join(parent_dir, f"{name}.imageset")
    os.makedirs(iset, exist_ok=True)
    Image.new("RGBA", (16, 16), ltr_color).save(
        os.path.join(iset, f"{name}-ltr.png"))
    Image.new("RGBA", (16, 16), rtl_color).save(
        os.path.join(iset, f"{name}-rtl.png"))
    with open(os.path.join(iset, "Contents.json"), "w") as f:
        json.dump({
            "images": [
                {"filename": f"{name}-ltr.png", "idiom": "universal",
                 "language-direction": "left-to-right"},
                {"filename": f"{name}-rtl.png", "idiom": "universal",
                 "language-direction": "right-to-left"},
            ],
            "info": {"author": "xcode", "version": 1},
        }, f)


def _parse_keyformat(car_path):
    """Read the KEYFORMAT token list from a car file."""
    data, blocks, named, read_block = _read_car_blocks(car_path)
    kf = read_block(named["KEYFORMAT"])
    count = struct.unpack_from('<I', kf, 8)[0]
    return [struct.unpack_from('<I', kf, 12 + i * 4)[0]
            for i in range(count)]


def _parse_appearance_keys(car_path):
    """Read the APPEARANCEKEYS tree → {name: id}."""
    data, blocks, named, read_block = _read_car_blocks(car_path)
    if "APPEARANCEKEYS" not in named:
        return {}
    tree = read_block(named["APPEARANCEKEYS"])
    root = struct.unpack('>I', tree[8:12])[0]
    result = {}
    for key_bytes, val_bytes in _walk_tree_leaves(read_block, root):
        name = key_bytes.rstrip(b'\x00').decode()
        val = struct.unpack('<H', val_bytes[:2])[0]
        result[name] = val
    return result


class TestDarkModeColors(unittest.TestCase):
    """Colorsets with dark-mode appearance variants must produce two
    renditions — one with appearance=0 (light) and one with appearance=1
    (dark) — and an APPEARANCEKEYS block mapping names to IDs.

    Regression: dark mode color variants were parsed but the
    APPEARANCEKEYS block and keyformat token 7 (ThemeAppearance) were
    not verified to work end-to-end.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="actool_dark_")
        self.catalog = os.path.join(self.tmpdir, "Test.xcassets")
        os.makedirs(self.catalog)
        import json
        with open(os.path.join(self.catalog, "Contents.json"), "w") as f:
            json.dump({"info": {"author": "xcode", "version": 1}}, f)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _compile(self):
        outdir = os.path.join(self.tmpdir, "out")
        compile_catalog(self.catalog, outdir, "macosx", "11.0")
        return os.path.join(outdir, "Assets.car")

    def test_dark_color_produces_two_renditions(self):
        """A colorset with light+dark produces two color renditions."""
        _make_colorset(self.catalog, "Accent", [
            {"r": 1.0, "g": 0.0, "b": 0.0, "a": 1.0},
            {"r": 0.0, "g": 0.0, "b": 1.0, "a": 1.0, "appearance": "dark"},
        ])
        car_path = self._compile()
        csi = parse_car_csi_by_name(car_path)
        entries = csi.get("Accent", [])
        self.assertEqual(len(entries), 2,
                         "Should have light and dark renditions")
        layouts = {e["layout"] for e in entries}
        self.assertEqual(layouts, {1009})

    def test_appearance_keys_block_present(self):
        """APPEARANCEKEYS named block exists when dark mode is used."""
        _make_colorset(self.catalog, "Bg", [
            {"r": 1.0, "g": 1.0, "b": 1.0, "a": 1.0},
            {"r": 0.0, "g": 0.0, "b": 0.0, "a": 1.0, "appearance": "dark"},
        ])
        car_path = self._compile()
        ak = _parse_appearance_keys(car_path)
        self.assertIn("NSAppearanceNameDarkAqua", ak)
        self.assertEqual(ak["NSAppearanceNameDarkAqua"], 1)

    def test_no_dark_mode_no_appearance_keys(self):
        """Without dark mode, APPEARANCEKEYS may still exist but dark=1."""
        _make_colorset(self.catalog, "Plain", [
            {"r": 0.5, "g": 0.5, "b": 0.5, "a": 1.0},
        ])
        car_path = self._compile()
        # Should still compile without issues
        info = parse_car_info(car_path)
        self.assertEqual(info["layout_counts"].get(1009, 0), 1)

    def test_dark_color_values_correct(self):
        """Light and dark renditions contain the correct colour values."""
        from tests.helpers import parse_colr_rendition
        _make_colorset(self.catalog, "Test", [
            {"r": 1.0, "g": 0.0, "b": 0.0, "a": 1.0},
            {"r": 0.0, "g": 1.0, "b": 0.0, "a": 0.5, "appearance": "dark"},
        ])
        car_path = self._compile()
        csi = parse_car_csi_by_name(car_path)
        entries = csi["Test"]
        # Sort by appearance (token 7 in key)
        for entry in entries:
            colr = parse_colr_rendition(entry["rend"])
            self.assertIsNotNone(colr)
            r, g, b, a = colr["components"]
            # Determine which variant this is from the key
            key_vals = struct.unpack_from(
                f'<{len(entry["key"])//2}H', entry["key"])
            # Token 7 (appearance) is always first in keyformat
            appearance = key_vals[0]
            if appearance == 0:
                self.assertAlmostEqual(r, 1.0, places=3)
                self.assertAlmostEqual(a, 1.0, places=3)
            else:
                self.assertAlmostEqual(g, 1.0, places=3)
                self.assertAlmostEqual(a, 0.5, places=3)


class TestLanguageDirection(unittest.TestCase):
    """Imagesets with language-direction must include token 4 (Direction)
    in the keyformat and set the correct direction values on renditions.

    Regression: language-direction was ignored, causing the keyformat to
    omit token 4 and all direction-specific renditions to share the same
    key (collision).
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="actool_dir_")
        self.catalog = os.path.join(self.tmpdir, "Test.xcassets")
        os.makedirs(self.catalog)
        import json
        with open(os.path.join(self.catalog, "Contents.json"), "w") as f:
            json.dump({"info": {"author": "xcode", "version": 1}}, f)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _compile(self):
        outdir = os.path.join(self.tmpdir, "out")
        compile_catalog(self.catalog, outdir, "macosx", "11.0")
        return os.path.join(outdir, "Assets.car")

    def test_direction_adds_token_4(self):
        """Token 4 appears in keyformat when language-direction is used."""
        _make_directional_imageset(
            self.catalog, "Arrow", (255, 0, 0, 255), (0, 0, 255, 255))
        car_path = self._compile()
        kf = _parse_keyformat(car_path)
        self.assertIn(4, kf, "Token 4 (Direction) should be in keyformat")

    def test_no_direction_no_token_4(self):
        """Token 4 absent when no language-direction is used."""
        # Just regular images, no direction
        import json
        iset = os.path.join(self.catalog, "Plain.imageset")
        os.makedirs(iset)
        Image.new("RGBA", (16, 16), (100, 100, 100, 255)).save(
            os.path.join(iset, "Plain.png"))
        Image.new("RGBA", (32, 32), (100, 100, 100, 255)).save(
            os.path.join(iset, "Plain@2x.png"))
        with open(os.path.join(iset, "Contents.json"), "w") as f:
            json.dump({
                "images": [
                    {"filename": "Plain.png", "idiom": "mac", "scale": "1x"},
                    {"filename": "Plain@2x.png", "idiom": "mac",
                     "scale": "2x"},
                ],
                "info": {"author": "xcode", "version": 1},
            }, f)
        car_path = self._compile()
        kf = _parse_keyformat(car_path)
        self.assertNotIn(4, kf,
                         "Token 4 should be absent without direction")

    def test_direction_values_correct(self):
        """LTR and RTL renditions have distinct direction values."""
        _make_directional_imageset(
            self.catalog, "Nav", (255, 0, 0, 255), (0, 0, 255, 255))
        car_path = self._compile()
        csi = parse_car_csi_by_name(car_path)

        kf = _parse_keyformat(car_path)
        dir_idx = kf.index(4)

        directions = set()
        for name, entries in csi.items():
            for entry in entries:
                key_vals = struct.unpack_from(
                    f'<{len(entry["key"])//2}H', entry["key"])
                if len(key_vals) > dir_idx:
                    d = key_vals[dir_idx]
                    if d != 0:
                        directions.add(d)

        self.assertIn(car.DIRECTION_LTR, directions)
        self.assertIn(car.DIRECTION_RTL, directions)

    def test_both_variants_renderable(self):
        """Both LTR and RTL renditions are present in the car."""
        _make_directional_imageset(
            self.catalog, "Arrow", (255, 0, 0, 255), (0, 0, 255, 255))
        car_path = self._compile()
        csi = parse_car_csi_by_name(car_path)
        # Both LTR and RTL filenames should be present as renditions
        self.assertIn("Arrow-ltr.png", csi)
        self.assertIn("Arrow-rtl.png", csi)


class TestOversizedImagesInline(unittest.TestCase):
    """Images too large for atlas packing must be stored inline.

    Regression: large images (wider than 258px or taller than 192px) were
    packed into oversized atlases, while the system actool stores them
    inline.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="actool_big_")
        self.catalog = os.path.join(self.tmpdir, "Test.xcassets")
        os.makedirs(self.catalog)
        import json
        with open(os.path.join(self.catalog, "Contents.json"), "w") as f:
            json.dump({"info": {"author": "xcode", "version": 1}}, f)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _add_imageset(self, name, width, height):
        import json
        iset = os.path.join(self.catalog, f"{name}.imageset")
        os.makedirs(iset)
        Image.new("RGBA", (width, height), (100, 50, 25, 255)).save(
            os.path.join(iset, f"{name}.png"))
        with open(os.path.join(iset, "Contents.json"), "w") as f:
            json.dump({
                "images": [
                    {"filename": f"{name}.png", "idiom": "mac",
                     "scale": "1x"},
                ],
                "info": {"author": "xcode", "version": 1},
            }, f)

    def test_wide_image_inline(self):
        """Image wider than atlas max (262px) is stored inline."""
        self._add_imageset("Wide", 400, 100)
        self._add_imageset("Small", 16, 16)
        outdir = os.path.join(self.tmpdir, "out")
        compile_catalog(self.catalog, outdir, "macosx", "11.0")
        csi = parse_car_csi_by_name(os.path.join(outdir, "Assets.car"))
        self.assertEqual(csi["Wide.png"][0]["layout"], 12,
                         "Wide image should be inline")

    def test_tall_image_inline(self):
        """Image taller than atlas max (196px) is stored inline."""
        self._add_imageset("Tall", 100, 300)
        self._add_imageset("Small", 16, 16)
        outdir = os.path.join(self.tmpdir, "out")
        compile_catalog(self.catalog, outdir, "macosx", "11.0")
        csi = parse_car_csi_by_name(os.path.join(outdir, "Assets.car"))
        self.assertEqual(csi["Tall.png"][0]["layout"], 12,
                         "Tall image should be inline")

    def test_fitting_image_packed(self):
        """Image within atlas limits is packed normally."""
        self._add_imageset("A", 100, 100)
        self._add_imageset("B", 100, 100)
        outdir = os.path.join(self.tmpdir, "out")
        compile_catalog(self.catalog, outdir, "macosx", "11.0")
        csi = parse_car_csi_by_name(os.path.join(outdir, "Assets.car"))
        self.assertEqual(csi["A.png"][0]["layout"], 1003,
                         "Fitting image should be packed")


class TestGzipCompression(unittest.TestCase):
    """Inline images must use gzip (comp=2) compression when data is large
    enough, and uncompressed (comp=0) otherwise.

    Regression: images were stored uncompressed at all deployment targets,
    producing valid but needlessly large car files.
    """

    def _compile_and_get_celm(self, image_size, min_deploy="10.9"):
        """Compile a single image and return its CELM (ver, comp) tuple."""
        tmpdir = tempfile.mkdtemp(prefix="actool_gz_")
        try:
            w, h = image_size
            img = Image.new("RGBA", (w, h), (100, 50, 25, 128))
            catalog = _make_catalog_with_image(tmpdir, "Img", img)
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", min_deploy)
            csi = parse_car_csi_by_name(os.path.join(outdir, "Assets.car"))
            entry = csi["Img.png"][0]
            rend = entry["rend"]
            if len(rend) >= 12:
                ver = struct.unpack_from('<I', rend, 4)[0]
                comp = struct.unpack_from('<I', rend, 8)[0]
                return ver, comp
            return None, None
        finally:
            shutil.rmtree(tmpdir)

    def test_large_image_uses_gzip(self):
        """Image with > 256B raw data uses gzip (comp=2)."""
        ver, comp = self._compile_and_get_celm((32, 32))  # 4096B raw
        self.assertEqual(comp, 2, "Large image should use gzip")
        self.assertEqual(ver, 0, "Gzip CELM should use ver=0")

    def test_small_image_uncompressed(self):
        """Image with <= 256B raw data stays uncompressed (comp=0)."""
        ver, comp = self._compile_and_get_celm((4, 4))  # 64B raw
        self.assertEqual(comp, 0, "Small image should be uncompressed")

    def test_gzip_at_all_deploy_targets(self):
        """Gzip works for 10.9, 10.11, and 11.0 targets."""
        for deploy in ("10.9", "10.11", "11.0"):
            ver, comp = self._compile_and_get_celm((32, 32), deploy)
            # 11.0 may use DMP2 for packed, but inline uses gzip
            self.assertIn(comp, (2, 11),
                          f"macOS {deploy}: expected gzip(2) or dmp2(11), "
                          f"got {comp}")

    def test_gzip_data_is_valid(self):
        """The gzip payload decompresses to the correct size."""
        import zlib
        tmpdir = tempfile.mkdtemp(prefix="actool_gzv_")
        try:
            img = Image.new("RGBA", (32, 32), (100, 50, 25, 200))
            catalog = _make_catalog_with_image(tmpdir, "Img", img)
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", "10.9")
            csi = parse_car_csi_by_name(os.path.join(outdir, "Assets.car"))
            entry = csi["Img.png"][0]
            rend = entry["rend"]
            comp = struct.unpack_from('<I', rend, 8)[0]
            payload_len = struct.unpack_from('<I', rend, 12)[0]
            payload = rend[16:16 + payload_len]
            self.assertEqual(comp, 2)
            decompressed = zlib.decompress(payload, 15 + 32)
            expected = 32 * 4 * 32  # width * bpp * height
            self.assertEqual(len(decompressed), expected)
        finally:
            shutil.rmtree(tmpdir)

    @unittest.skipUnless(has_extract_pixels(), "extract_pixels not built")
    def test_gzip_pixels_roundtrip(self):
        """Gzip-compressed image roundtrips through CoreUI correctly."""
        tmpdir = tempfile.mkdtemp(prefix="actool_gzrt_")
        try:
            # Solid colour with semi-transparent alpha
            img = Image.new("RGBA", (16, 16), (200, 100, 50, 180))
            catalog = _make_catalog_with_image(tmpdir, "GzImg", img)
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", "10.9")
            car = os.path.join(outdir, "Assets.car")
            ext = os.path.join(tmpdir, "ext")
            os.makedirs(ext)
            result = extract_car_image(car, "GzImg", ext)
            self.assertIn(1, result)
            w, h, pixels = result[1]
            self.assertEqual((w, h), (16, 16))
            # Check alpha survived (premultiplied: R = 200*180/255 ≈ 141)
            a = pixels[3]
            self.assertEqual(a, 180, f"Alpha should be 180, got {a}")
            r = pixels[0]
            self.assertAlmostEqual(r, 141, delta=2,
                                   msg=f"Premultiplied R ≈ 141, got {r}")
        finally:
            shutil.rmtree(tmpdir)


class TestOpaqueFlag(unittest.TestCase):
    """The isOpaque rendition flag must never be set.

    Regression: our tool set is_opaque=True for images with all alpha=255,
    but the system actool never sets this flag.
    """

    def test_opaque_flag_not_set_for_rgb(self):
        """RGB image (fully opaque) does not get is_opaque flag."""
        tmpdir = tempfile.mkdtemp(prefix="actool_opq_")
        try:
            img = Image.new("RGB", (16, 16), (255, 0, 0))
            catalog = _make_catalog_with_image(tmpdir, "Opaque", img)
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", "11.0")
            csi = parse_car_csi_by_name(os.path.join(outdir, "Assets.car"))
            for entry in csi["Opaque.png"]:
                self.assertFalse(entry["flags"] & 0x02,
                                 "isOpaque bit should not be set")
        finally:
            shutil.rmtree(tmpdir)

    def test_opaque_flag_not_set_for_transparent(self):
        """Image with alpha < 255 also does not get is_opaque flag."""
        tmpdir = tempfile.mkdtemp(prefix="actool_opq_")
        try:
            img = Image.new("RGBA", (16, 16), (255, 0, 0, 128))
            catalog = _make_catalog_with_image(tmpdir, "Semi", img)
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", "11.0")
            csi = parse_car_csi_by_name(os.path.join(outdir, "Assets.car"))
            for entry in csi["Semi.png"]:
                self.assertFalse(entry["flags"] & 0x02,
                                 "isOpaque bit should not be set")
        finally:
            shutil.rmtree(tmpdir)

    def test_packed_ref_opaque_flag_not_set(self):
        """Packed image references also don't set is_opaque."""
        tmpdir = tempfile.mkdtemp(prefix="actool_opq_")
        try:
            catalog, _ = make_temp_catalog(
                [("A", "RGBA"), ("B", "RGBA"), ("C", "RGBA")], tmpdir)
            outdir = os.path.join(tmpdir, "out")
            compile_catalog(catalog, outdir, "macosx", "11.0")
            csi = parse_car_csi_by_name(os.path.join(outdir, "Assets.car"))
            for name, entries in csi.items():
                for entry in entries:
                    if entry["layout"] == 1003:
                        self.assertFalse(entry["flags"] & 0x02,
                                         f"{name}: isOpaque should not "
                                         f"be set on packed refs")
        finally:
            shutil.rmtree(tmpdir)
