"""
xcassets catalog parser.

Reads .xcassets directories and produces Rendition objects for compilation.
"""

import json
import os
import struct
from pathlib import Path
from typing import Optional

from PIL import Image

from . import car
from .name_hash import hash_name as _hash_name


# Icon point size -> dim2 mapping for macOS app icons
# dim2 values match Apple's MSIS index (1-based by point size)
ICON_DIM2_MAP = {
    16: 1,
    32: 2,
    128: 3,
    256: 4,
    512: 5,
}

# Standard macOS icon sizes (point size, scale)
MACOS_ICON_SIZES = [
    (16, 1), (16, 2),
    (32, 1), (32, 2),
    (128, 1), (128, 2),
    (256, 1), (256, 2),
    (512, 1), (512, 2),
]


def _premultiply_bgra(data: bytes) -> bytes:
    """Premultiply alpha for BGRA pixel data."""
    buf = bytearray(data)
    for i in range(0, len(buf) - 3, 4):
        a = buf[i + 3]
        if a == 255:
            continue
        if a == 0:
            buf[i] = buf[i + 1] = buf[i + 2] = 0
        else:
            buf[i] = (buf[i] * a + 127) // 255
            buf[i + 1] = (buf[i + 1] * a + 127) // 255
            buf[i + 2] = (buf[i + 2] * a + 127) // 255
    return bytes(buf)


def _premultiply_ga8(data: bytes) -> bytes:
    """Premultiply alpha for GA8 (gray+alpha) pixel data."""
    buf = bytearray(data)
    for i in range(0, len(buf) - 1, 2):
        a = buf[i + 1]
        if a == 255:
            continue
        if a == 0:
            buf[i] = 0
        else:
            buf[i] = (buf[i] * a + 127) // 255
    return bytes(buf)


def load_image_as_bgra(path: str,
                       force_bgra: bool = False,
                       ) -> tuple[bytes, int, int, bytes]:
    """Load an image file and return (pixel_data, width, height, pixel_format).

    Pixel data is returned in premultiplied alpha form, matching CoreUI's
    expected format.

    When force_bgra is True, always return BGRA format even for grayscale
    images (used for older deployment targets that don't support GA8).
    """
    img = Image.open(path)

    if img.mode == "RGBA":
        r, g, b, a = img.split()
        # Detect grayscale-compatible RGBA (R==G==B) → store as GA8
        if not force_bgra and r.tobytes() == g.tobytes() == b.tobytes():
            ga = Image.merge("LA", (r, a))
            return _premultiply_ga8(ga.tobytes()), img.width, img.height, b" 8AG"
        # Convert RGBA to BGRA
        img = Image.merge("RGBA", (b, g, r, a))
        return _premultiply_bgra(img.tobytes()), img.width, img.height, b"BGRA"
    elif img.mode == "LA" or img.mode == "PA":
        if force_bgra:
            img = img.convert("RGBA")
            r, g, b, a = img.split()
            img = Image.merge("RGBA", (b, g, r, a))
            return _premultiply_bgra(img.tobytes()), img.width, img.height, b"BGRA"
        # Grayscale + Alpha -> GA8
        img = img.convert("LA")
        return _premultiply_ga8(img.tobytes()), img.width, img.height, b" 8AG"
    elif img.mode == "L":
        if force_bgra:
            img = img.convert("RGBA")
            r, g, b, a = img.split()
            img = Image.merge("RGBA", (b, g, r, a))
            return _premultiply_bgra(img.tobytes()), img.width, img.height, b"BGRA"
        # Grayscale -> add alpha channel (fully opaque, premultiply is no-op)
        img = img.convert("LA")
        return img.tobytes(), img.width, img.height, b" 8AG"
    elif img.mode == "RGB":
        # Add alpha channel, convert to BGRA
        img = img.convert("RGBA")
        r, g, b, a = img.split()
        img = Image.merge("RGBA", (b, g, r, a))
        pixel_data = img.tobytes()
        return pixel_data, img.width, img.height, b"BGRA"
    elif img.mode == "P":
        # Palette mode - convert to RGBA then BGRA
        img = img.convert("RGBA")
        r, g, b, a = img.split()
        img = Image.merge("RGBA", (b, g, r, a))
        pixel_data = img.tobytes()
        return pixel_data, img.width, img.height, b"BGRA"
    else:
        # Convert anything else to RGBA -> BGRA
        img = img.convert("RGBA")
        r, g, b, a = img.split()
        img = Image.merge("RGBA", (b, g, r, a))
        pixel_data = img.tobytes()
        return pixel_data, img.width, img.height, b"BGRA"


def _parse_color_component(value: str) -> float:
    """Parse a color component from xcassets Contents.json.

    Values can be:
    - Float 0-1: "0.500" (parsed through float32 to match Apple precision)
    - Integer 0-255: "128" (exact double, normalised to 0-1)
    - Hex: "0x80" (exact double, normalised to 0-1)

    Integer and hex values round-trip as exact doubles. Decimal string
    values are cast through float32 because Apple's actool parses them
    with single-precision intermediates (matching `strtof` behavior).
    """
    import struct as _struct
    if value.startswith("0x") or value.startswith("0X"):
        return int(value, 16) / 255.0
    # Integer form ("128") has no decimal point or exponent.
    if all(c.isdigit() or c in "+-" for c in value):
        i = int(value)
        if i > 1 or i < 0:
            return i / 255.0
        return float(i)
    f = float(value)
    if f > 1.0:
        return f / 255.0
    # Cast through float32 to match Apple's single-precision parsing.
    return _struct.unpack('<f', _struct.pack('<f', f))[0]


class AssetCatalog:
    """Parser for .xcassets directories."""

    def __init__(self, path: str, platform: str = "macosx",
                 min_deploy: str = "11.0", app_icon: Optional[str] = None,
                 include_languages: Optional[list[str]] = None,
                 development_region: Optional[str] = None):
        self.path = path
        self.platform = platform
        self.min_deploy = min_deploy
        self.app_icon = app_icon
        self.include_languages = include_languages
        self.development_region = development_region
        self._identifiers: dict[str, int] = {}
        self._next_id = 1
        self._locales_used: set[str] = set()
        # GA8 pixel format is only used with atlas packing (>= 10.11).
        # Older targets store everything as BGRA.
        deploy_ver = tuple(int(x) for x in min_deploy.split(".")[:2])
        min_ga8 = {"macosx": (10, 11), "iphoneos": (9, 0),
                   "appletvos": (9, 0), "watchos": (2, 0)}
        self._force_bgra = deploy_ver < min_ga8.get(platform, (10, 11))

    def _should_include_locale(self, locale: str) -> bool:
        """Check if a locale should be included based on filtering options.

        Per the manpage: if --include-language is not specified, all locales
        are included. When specified, only the listed languages plus the
        development region language are included. Non-localized assets
        (locale="") are always included.
        """
        if not self.include_languages:
            return True  # No filtering, include all

        if locale == self.development_region:
            return True  # Development region always included

        return locale in self.include_languages

    def get_locales_used(self) -> set[str]:
        """Return the set of locale codes that were included in the output."""
        return self._locales_used

    def _get_identifier(self, name: str) -> int:
        """Get or create an identifier for a facet name."""
        if name not in self._identifiers:
            self._identifiers[name] = _hash_name(name)
        return self._identifiers[name]

    def parse(self) -> tuple[list[car.Rendition], dict[str, tuple[int, int, int]]]:
        """Parse the asset catalog.

        Returns (renditions, facets) where facets maps name -> (element, part, identifier).
        """
        renditions = []
        facets = {}

        catalog_path = Path(self.path)
        if not catalog_path.exists():
            raise FileNotFoundError(f"Asset catalog not found: {self.path}")

        self._parse_directory(catalog_path, renditions, facets)

        return renditions, facets

    def _parse_directory(self, catalog_path: Path,
                         renditions: list, facets: dict,
                         namespace: str = ""):
        """Parse asset entries in a directory, recursing into groups."""
        for item in sorted(catalog_path.iterdir()):
            if item.suffix == ".imageset":
                self._parse_imageset(item, renditions, facets,
                                     namespace=namespace)

            elif item.suffix == ".appiconset":
                self._parse_appiconset(item, renditions, facets)

            elif item.suffix == ".iconset":
                self._parse_iconset(item, renditions, facets,
                                    namespace=namespace)

            elif item.suffix == ".colorset":
                self._parse_colorset(item, renditions, facets,
                                     namespace=namespace)

            elif item.suffix == ".dataset":
                self._parse_dataset(item, renditions, facets,
                                    namespace=namespace)

            elif item.suffix == ".spriteatlas":
                self._parse_spriteatlas(item, renditions, facets)

            elif item.suffix == ".imagestack":
                self._parse_imagestack(item, renditions, facets)

            elif item.is_dir() and not item.suffix:
                # Plain directory = xcassets group — recurse into it
                # Check if the group provides a namespace prefix
                child_ns = namespace
                group_json = item / "Contents.json"
                if group_json.exists():
                    with open(group_json) as f:
                        group_contents = json.load(f)
                    if group_contents.get("properties", {}).get(
                            "provides-namespace"):
                        child_ns = f"{namespace}{item.name}/" if namespace \
                            else f"{item.name}/"
                self._parse_directory(item, renditions, facets,
                                      namespace=child_ns)

    def _parse_imageset(self, item: Path, renditions: list, facets: dict,
                        namespace: str = ""):
        """Parse a .imageset directory."""
        name = item.stem
        facet_name = namespace + name
        ident = self._get_identifier(facet_name)

        contents_path = item / "Contents.json"
        if not contents_path.exists():
            return

        with open(contents_path) as f:
            contents = json.load(f)

        # Template rendering intent (bitmapEncoding values):
        # original=0, automatic=4, template=2
        props = contents.get("properties", {})
        intent_str = props.get("template-rendering-intent")
        intent_map = {"original": 0, "template": 2}
        template_intent = intent_map.get(intent_str, 4)  # default = automatic

        images = contents.get("images", [])
        for img_info in images:
            filename = img_info.get("filename")
            if not filename:
                continue

            img_path = item / filename
            if not img_path.exists():
                continue

            scale_str = img_info.get("scale", "1x")
            scale = int(scale_str.replace("x", ""))

            idiom = img_info.get("idiom", "universal")
            if self.platform == "macosx" and idiom not in ("mac", "universal"):
                continue

            # macOS only supports 1x and 2x scales
            if self.platform == "macosx" and scale > 2:
                continue

            locale = img_info.get("locale", "")

            # Language filtering
            if locale and not self._should_include_locale(locale):
                continue

            if locale:
                self._locales_used.add(locale)

            # Language direction (token 4 in keyformat)
            lang_dir_str = img_info.get("language-direction", "")
            direction_map = {
                "left-to-right": car.DIRECTION_LTR,
                "right-to-left": car.DIRECTION_RTL,
            }
            direction = direction_map.get(lang_dir_str, car.DIRECTION_DEFAULT)

            # Appearance variant (dark mode)
            appearance = 0
            for app in img_info.get("appearances", []):
                if (app.get("appearance") == "luminosity"
                        and app.get("value") == "dark"):
                    appearance = 1

            # PDF files are stored as raw data (layout 9), not rasterized
            if filename.lower().endswith(".pdf"):
                with open(img_path, "rb") as pdf_f:
                    pdf_data = pdf_f.read()
                csi = car.build_pdf_csi(filename, pdf_data)
                rend = car.Rendition(
                    name=filename,
                    identifier=ident,
                    element=car.ELEMENT_UNIVERSAL,
                    part=car.PART_REGULAR,
                    scale=1,
                    appearance=appearance,
                    direction=direction,
                    layout=car.LAYOUT_PDF,
                    pixel_format=car.PIXELFMT_PDF,
                )
                rend._csi_override = csi
                renditions.append(rend)
                continue

            pixel_data, width, height, pixel_format = load_image_as_bgra(
                str(img_path), force_bgra=self._force_bgra)

            rend = car.Rendition(
                name=filename,
                identifier=ident,
                element=car.ELEMENT_UNIVERSAL,
                part=car.PART_REGULAR,
                scale=scale,
                width=width,
                height=height,
                pixel_data=pixel_data,
                pixel_format=pixel_format,
                appearance=appearance,
                direction=direction,
                layout=car.LAYOUT_ONE_PART_SCALE,
                template_rendering_intent=template_intent,
                locale=locale,
                colorspace_id=2 if pixel_format == b" 8AG" else 1,
            )
            renditions.append(rend)

        facets[facet_name] = (car.ELEMENT_UNIVERSAL, car.PART_REGULAR, ident)

    def _parse_appiconset(self, item: Path, renditions: list, facets: dict):
        """Parse an .appiconset directory."""
        if not self.app_icon:
            return
        name = item.stem
        if name != self.app_icon:
            return

        ident = self._get_identifier(name)

        contents_path = item / "Contents.json"
        if not contents_path.exists():
            return

        with open(contents_path) as f:
            contents = json.load(f)

        icon_renditions = []
        images = contents.get("images", [])
        for img_info in images:
            filename = img_info.get("filename")
            if not filename:
                continue

            img_path = item / filename
            if not img_path.exists():
                continue

            # Skip images targeted at other platforms
            img_platform = img_info.get("platform", "")
            if img_platform and img_platform != self.platform:
                continue

            scale_str = img_info.get("scale", "1x")
            scale = int(scale_str.replace("x", ""))

            size_str = img_info.get("size", "")
            if "x" in size_str:
                point_w = int(size_str.split("x")[0])
            else:
                point_w = 0

            pixel_data, width, height, pixel_format = load_image_as_bgra(
                str(img_path), force_bgra=self._force_bgra)

            pixel_size = point_w * scale
            dim2 = ICON_DIM2_MAP.get(point_w, 0)

            rend = car.Rendition(
                name=filename,
                identifier=ident,
                element=car.ELEMENT_UNIVERSAL,
                part=car.PART_ICON,
                scale=scale,
                width=width,
                height=height,
                pixel_data=pixel_data,
                pixel_format=pixel_format,
                layout=car.LAYOUT_ONE_PART_SCALE,
                dim2=dim2,
                template_rendering_intent=0,  # Icons are always original
                colorspace_id=2 if pixel_format == b" 8AG" else 1,
            )
            renditions.append(rend)
            icon_renditions.append((rend, point_w, pixel_size))

        if not icon_renditions:
            return

        # Add multisize image rendition (one entry per point size)
        ms_entries = []
        seen_point_sizes = set()
        for rend, point_w, pixel_size in icon_renditions:
            if point_w not in seen_point_sizes:
                dim2 = ICON_DIM2_MAP.get(point_w, 0)
                ms_entries.append(car.MultisizeImageEntry(
                    width=point_w,
                    height=point_w,
                    index=dim2,
                ))
                seen_point_sizes.add(point_w)

        ms_rend = car.build_multisize_rendition(name, ident, ms_entries)
        renditions.append(ms_rend)

        facets[name] = (car.ELEMENT_UNIVERSAL, car.PART_ICON, ident)

    def _parse_iconset(self, item: Path, renditions: list, facets: dict,
                       namespace: str = ""):
        """Parse an .iconset directory (document type icons).

        Unlike .appiconset, .iconset has no Contents.json — icon images
        follow the naming convention icon_{W}x{H}[@{scale}x].png.
        All .iconset directories are processed (no --app-icon gate).
        """
        import re
        name = item.stem
        facet_name = namespace + name
        ident = self._get_identifier(facet_name)

        icon_renditions = []
        pattern = re.compile(
            r'^icon_(\d+)x(\d+)(?:@(\d+)x)?\.png$')

        for img_file in sorted(item.iterdir()):
            if not img_file.is_file():
                continue
            m = pattern.match(img_file.name)
            if not m:
                continue

            point_w = int(m.group(1))
            scale = int(m.group(3)) if m.group(3) else 1

            if self.platform == "macosx" and scale > 2:
                continue

            pixel_data, width, height, pixel_format = load_image_as_bgra(
                str(img_file), force_bgra=self._force_bgra)

            dim2 = ICON_DIM2_MAP.get(point_w, 0)

            rend = car.Rendition(
                name=img_file.name,
                identifier=ident,
                element=car.ELEMENT_UNIVERSAL,
                part=car.PART_ICON,
                scale=scale,
                width=width,
                height=height,
                pixel_data=pixel_data,
                pixel_format=pixel_format,
                layout=car.LAYOUT_ONE_PART_SCALE,
                dim2=dim2,
                template_rendering_intent=0,
                colorspace_id=2 if pixel_format == b" 8AG" else 1,
            )
            renditions.append(rend)
            icon_renditions.append((rend, point_w))

        if not icon_renditions:
            return

        # Add multisize image rendition
        ms_entries = []
        seen_point_sizes = set()
        for rend, point_w in icon_renditions:
            if point_w not in seen_point_sizes:
                dim2 = ICON_DIM2_MAP.get(point_w, 0)
                ms_entries.append(car.MultisizeImageEntry(
                    width=point_w,
                    height=point_w,
                    index=dim2,
                ))
                seen_point_sizes.add(point_w)

        ms_rend = car.build_multisize_rendition(name, ident, ms_entries)
        renditions.append(ms_rend)

        facets[facet_name] = (car.ELEMENT_UNIVERSAL, car.PART_ICON, ident)

    def _parse_colorset(self, item_path: Path,
                        renditions: list, facets: dict,
                        namespace: str = ""):
        """Parse a .colorset directory."""
        name = item_path.stem
        facet_name = namespace + name
        ident = self._get_identifier(facet_name)
        contents_path = item_path / "Contents.json"
        if not contents_path.exists():
            return

        with open(contents_path) as f:
            contents = json.load(f)

        added = False
        for color_entry in contents.get("colors", []):
            color_data = color_entry.get("color", {})
            components = color_data.get("components", {})
            if not components:
                continue

            r = _parse_color_component(components.get("red", "0"))
            g = _parse_color_component(components.get("green", "0"))
            b = _parse_color_component(components.get("blue", "0"))
            a = _parse_color_component(components.get("alpha", "1"))

            colorspace = color_data.get("color-space", "srgb")
            cs_map = {"srgb": 1, "display-p3": 3, "extended-srgb": 4,
                      "extended-linear-srgb": 7, "gray-gamma-22": 2}
            cs_id = cs_map.get(colorspace, 1)

            # Check for appearance variants (dark mode)
            appearance = 0
            appearances = color_entry.get("appearances", [])
            for app in appearances:
                if (app.get("appearance") == "luminosity" and
                        app.get("value") == "dark"):
                    appearance = 1

            csi = car.build_color_csi(name, r, g, b, a, cs_id)
            rend = car.Rendition(
                name=name,
                identifier=ident,
                element=car.ELEMENT_UNIVERSAL,
                part=car.PART_COLOR,
                scale=1,
                appearance=appearance,
                layout=car.LAYOUT_COLOR,
                pixel_format=b"\x00\x00\x00\x00",
                colorspace_id=0,  # CSI header colorspace is always 0 for colors
            )
            rend._csi_override = csi
            renditions.append(rend)
            added = True

        if added:
            facets[facet_name] = (car.ELEMENT_UNIVERSAL, car.PART_COLOR, ident)

    def _parse_dataset(self, item_path: Path,
                       renditions: list, facets: dict,
                       namespace: str = ""):
        """Parse a .dataset directory."""
        name = item_path.stem
        facet_name = namespace + name
        ident = self._get_identifier(facet_name)
        contents_path = item_path / "Contents.json"
        if not contents_path.exists():
            return

        with open(contents_path) as f:
            contents = json.load(f)

        for data_entry in contents.get("data", []):
            filename = data_entry.get("filename")
            if not filename:
                continue
            data_path = item_path / filename
            if not data_path.exists():
                continue

            with open(data_path, "rb") as f:
                raw_data = f.read()

            csi = car.build_data_csi(name, raw_data)
            rend = car.Rendition(
                name="CoreStructuredImage",
                identifier=ident,
                element=car.ELEMENT_UNIVERSAL,
                part=car.PART_REGULAR,
                scale=1,
                layout=car.LAYOUT_RAW_DATA,
                pixel_format=car.PIXELFMT_DATA,
            )
            rend._csi_override = csi
            renditions.append(rend)

        facets[facet_name] = (car.ELEMENT_UNIVERSAL, car.PART_REGULAR, ident)

    def _parse_spriteatlas(self, item_path: Path,
                           renditions: list, facets: dict):
        """Parse a .spriteatlas directory."""
        atlas_name = item_path.stem
        atlas_ident = self._get_identifier(atlas_name)

        # Register the atlas facet (element=9 for packed, no part)
        facets[atlas_name] = (car.ELEMENT_PACKED, None, atlas_ident)

        # Parse each imageset inside the spriteatlas
        for sprite_item in sorted(item_path.iterdir()):
            if sprite_item.suffix == ".imageset":
                sprite_name = sprite_item.stem
                # Namespaced facet: "AtlasName/SpriteName"
                full_name = f"{atlas_name}/{sprite_name}"
                sprite_ident = self._get_identifier(full_name)

                contents_path = sprite_item / "Contents.json"
                if not contents_path.exists():
                    continue

                with open(contents_path) as f:
                    contents = json.load(f)

                for img_info in contents.get("images", []):
                    filename = img_info.get("filename")
                    if not filename:
                        continue
                    img_path = sprite_item / filename
                    if not img_path.exists():
                        continue

                    scale_str = img_info.get("scale", "1x")
                    scale = int(scale_str.replace("x", ""))

                    pixel_data, width, height, pixel_format = \
                        load_image_as_bgra(str(img_path),
                                           force_bgra=self._force_bgra)

                    rend = car.Rendition(
                        name=filename,
                        identifier=sprite_ident,
                        element=car.ELEMENT_UNIVERSAL,
                        part=car.PART_REGULAR,
                        scale=scale,
                        width=width,
                        height=height,
                        pixel_data=pixel_data,
                        pixel_format=pixel_format,
                        layout=car.LAYOUT_ONE_PART_SCALE,
                        sprite_atlas_id=atlas_ident,
                        colorspace_id=2 if pixel_format == b" 8AG" else 1,
                    )
                    renditions.append(rend)

                facets[full_name] = (car.ELEMENT_UNIVERSAL,
                                     car.PART_REGULAR, sprite_ident)

    def _parse_imagestack(self, item_path: Path,
                          renditions: list, facets: dict):
        """Parse a .imagestack directory."""
        stack_name = item_path.stem
        contents_path = item_path / "Contents.json"
        if not contents_path.exists():
            return

        with open(contents_path) as f:
            contents = json.load(f)

        # Process each layer
        for layer_info in contents.get("layers", []):
            layer_filename = layer_info.get("filename")
            if not layer_filename:
                continue
            layer_path = item_path / layer_filename
            if not layer_path.exists():
                continue

            layer_name = Path(layer_filename).stem

            # Each layer has a Content.imageset inside
            content_imageset = layer_path / "Content.imageset"
            if not content_imageset.exists():
                continue

            # Namespaced facet: "StackName/LayerName/Content"
            full_name = f"{stack_name}/{layer_name}/Content"
            layer_ident = self._get_identifier(full_name)

            img_contents_path = content_imageset / "Contents.json"
            if not img_contents_path.exists():
                continue

            with open(img_contents_path) as f:
                img_contents = json.load(f)

            for img_info in img_contents.get("images", []):
                filename = img_info.get("filename")
                if not filename:
                    continue
                img_path = content_imageset / filename
                if not img_path.exists():
                    continue

                scale_str = img_info.get("scale", "1x")
                scale = int(scale_str.replace("x", ""))

                idiom = img_info.get("idiom", "universal")
                if self.platform == "macosx" and idiom not in ("mac", "universal"):
                    continue

                pixel_data, width, height, pixel_format = \
                    load_image_as_bgra(str(img_path))

                rend = car.Rendition(
                    name=filename,
                    identifier=layer_ident,
                    element=car.ELEMENT_UNIVERSAL,
                    part=car.PART_REGULAR,
                    scale=scale,
                    width=width,
                    height=height,
                    pixel_data=pixel_data,
                    pixel_format=pixel_format,
                    layout=car.LAYOUT_ONE_PART_SCALE,
                    colorspace_id=2 if pixel_format == b" 8AG" else 1,
                )
                renditions.append(rend)

            facets[full_name] = (car.ELEMENT_UNIVERSAL,
                                 car.PART_REGULAR, layer_ident)

    def get_icon_images(self) -> list[tuple[str, int, int]]:
        """Get paths and sizes for app icon images (for ICNS generation)."""
        if not self.app_icon:
            return []

        catalog_path = Path(self.path)
        icon_dir = catalog_path / f"{self.app_icon}.appiconset"
        if not icon_dir.exists():
            return []

        contents_path = icon_dir / "Contents.json"
        if not contents_path.exists():
            return []

        with open(contents_path) as f:
            contents = json.load(f)

        result = []
        for img_info in contents.get("images", []):
            filename = img_info.get("filename")
            if not filename:
                continue
            img_path = icon_dir / filename
            if not img_path.exists():
                continue

            scale_str = img_info.get("scale", "1x")
            scale = int(scale_str.replace("x", ""))
            size_str = img_info.get("size", "")
            if "x" in size_str:
                point_w = int(size_str.split("x")[0])
            else:
                continue

            pixel_size = point_w * scale
            result.append((str(img_path), pixel_size, scale))

        return result


def list_catalog_contents(xcassets_path: str) -> dict:
    """List the contents of an xcassets catalog as a tree structure.

    Returns a dict matching Apple's --print-contents plist format.
    """
    catalog_path = Path(xcassets_path)
    children = []

    for item in sorted(catalog_path.iterdir()):
        if item.suffix == ".imageset":
            children.append(_list_imageset(item))
        elif item.suffix == ".appiconset":
            children.append(_list_appiconset(item))
        elif item.suffix == ".colorset":
            children.append({"filename": item.name})
        elif item.suffix == ".dataset":
            children.append({"filename": item.name})
        elif item.suffix == ".spriteatlas":
            children.append(_list_spriteatlas(item))
        elif item.suffix == ".imagestack":
            children.append(_list_imagestack(item))

    result = {"filename": catalog_path.name}
    if children:
        result["children"] = children
    return result


def _list_imageset(item_path: Path) -> dict:
    """List contents of a single imageset."""
    contents_path = item_path / "Contents.json"
    if not contents_path.exists():
        return {"filename": item_path.name}

    with open(contents_path) as f:
        contents = json.load(f)

    props = contents.get("properties", {})
    rendering = props.get("template-rendering-intent")

    image_children = []
    for img_info in contents.get("images", []):
        filename = img_info.get("filename")
        if not filename:
            continue
        img_path = item_path / filename
        if not img_path.exists():
            continue

        entry = {"filename": filename}
        idiom = img_info.get("idiom")
        if idiom:
            entry["idiom"] = idiom

        scale = img_info.get("scale")
        if scale:
            entry["scale"] = scale

        try:
            img = Image.open(str(img_path))
            w_pts, h_pts = _image_point_size(img)
            entry["image"] = {"height": h_pts, "width": w_pts}
        except Exception:
            pass

        image_children.append(entry)

    result = {"filename": item_path.name}
    if rendering is not None:
        result["template-rendering-intent"] = rendering
    if image_children:
        result["children"] = image_children
    return result


def _list_appiconset(item_path: Path) -> dict:
    """List contents of an appiconset."""
    contents_path = item_path / "Contents.json"
    if not contents_path.exists():
        return {"filename": item_path.name}

    with open(contents_path) as f:
        contents = json.load(f)

    image_children = []
    for img_info in contents.get("images", []):
        filename = img_info.get("filename")
        if not filename:
            continue
        img_path = item_path / filename
        if not img_path.exists():
            continue

        entry = {"filename": filename}
        idiom = img_info.get("idiom")
        if idiom:
            entry["idiom"] = idiom
        size = img_info.get("size")
        if size:
            entry["size"] = size
        scale = img_info.get("scale")
        if scale:
            entry["scale"] = scale

        try:
            img = Image.open(str(img_path))
            w_pts, h_pts = _image_point_size(img)
            entry["image"] = {"height": h_pts, "width": w_pts}
        except Exception:
            pass

        image_children.append(entry)

    result = {"filename": item_path.name}
    if image_children:
        result["children"] = image_children
    return result


def _list_spriteatlas(item_path: Path) -> dict:
    """List contents of a .spriteatlas directory."""
    children = []
    for sprite_item in sorted(item_path.iterdir()):
        if sprite_item.suffix == ".imageset":
            children.append(_list_imageset(sprite_item))
    result = {"filename": item_path.name}
    if children:
        result["children"] = children
    return result


def _list_imagestack(item_path: Path) -> dict:
    """List contents of a .imagestack directory."""
    contents_path = item_path / "Contents.json"
    if not contents_path.exists():
        return {"filename": item_path.name}

    with open(contents_path) as f:
        contents = json.load(f)

    children = []
    for layer_info in contents.get("layers", []):
        layer_filename = layer_info.get("filename")
        if not layer_filename:
            continue
        layer_path = item_path / layer_filename
        if not layer_path.exists():
            continue

        layer_children = []
        content_imageset = layer_path / "Content.imageset"
        if content_imageset.exists():
            layer_children.append(_list_imageset(content_imageset))

        layer_entry = {"filename": layer_filename}
        if layer_children:
            layer_entry["children"] = layer_children
        children.append(layer_entry)

    result = {"filename": item_path.name}
    if children:
        result["children"] = children
    return result


def _image_point_size(img: Image.Image) -> tuple[float, float]:
    """Get image dimensions in points (72 DPI-based), matching Apple's actool."""
    dpi = img.info.get("dpi", (72, 72))
    w_pts = round(img.width * 72.0 / dpi[0])
    h_pts = round(img.height * 72.0 / dpi[1])
    # Always return float to match Apple's <real> plist type
    return float(w_pts), float(h_pts)
