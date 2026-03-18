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


def _hash_name(name: str) -> int:
    """Generate a deterministic identifier from a name.

    Apple uses some hash function to assign identifiers to facets.
    We replicate the behavior by using a simple hash.
    """
    h = 0
    for c in name:
        h = (h * 31 + ord(c)) & 0xFFFF
    return h


def load_image_as_bgra(path: str) -> tuple[bytes, int, int, bytes]:
    """Load an image file and return (pixel_data, width, height, pixel_format)."""
    img = Image.open(path)

    if img.mode == "RGBA":
        # Convert RGBA to BGRA
        r, g, b, a = img.split()
        img = Image.merge("RGBA", (b, g, r, a))
        pixel_data = img.tobytes()
        return pixel_data, img.width, img.height, b"BGRA"
    elif img.mode == "LA" or img.mode == "PA":
        # Grayscale + Alpha -> GA8
        img = img.convert("LA")
        pixel_data = img.tobytes()
        return pixel_data, img.width, img.height, b" 8AG"
    elif img.mode == "L":
        # Grayscale -> add alpha channel
        img = img.convert("LA")
        pixel_data = img.tobytes()
        return pixel_data, img.width, img.height, b" 8AG"
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

        # Process each asset directory
        for item in sorted(catalog_path.iterdir()):
            if item.suffix == ".imageset":
                name = item.stem
                ident = self._get_identifier(name)
                is_template = False

                contents_path = item / "Contents.json"
                if not contents_path.exists():
                    continue

                with open(contents_path) as f:
                    contents = json.load(f)

                # Check template rendering intent
                props = contents.get("properties", {})
                if props.get("template-rendering-intent") == "template":
                    is_template = True

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

                    locale = img_info.get("locale", "")

                    # Language filtering
                    if locale and not self._should_include_locale(locale):
                        continue

                    if locale:
                        self._locales_used.add(locale)

                    pixel_data, width, height, pixel_format = load_image_as_bgra(
                        str(img_path))

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
                        layout=car.LAYOUT_ONE_PART_SCALE,
                        is_template=is_template,
                        locale=locale,
                    )
                    renditions.append(rend)

                facets[name] = (car.ELEMENT_UNIVERSAL, car.PART_REGULAR, ident)

            elif item.suffix == ".appiconset":
                if not self.app_icon:
                    continue
                name = item.stem
                if name != self.app_icon:
                    continue

                ident = self._get_identifier(name)

                contents_path = item / "Contents.json"
                if not contents_path.exists():
                    continue

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

                    scale_str = img_info.get("scale", "1x")
                    scale = int(scale_str.replace("x", ""))

                    size_str = img_info.get("size", "")
                    if "x" in size_str:
                        point_w = int(size_str.split("x")[0])
                    else:
                        point_w = 0

                    pixel_data, width, height, pixel_format = load_image_as_bgra(
                        str(img_path))

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
                    )
                    renditions.append(rend)
                    icon_renditions.append((rend, point_w, pixel_size))

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

            elif item.suffix == ".colorset":
                self._parse_colorset(item, renditions, facets)

            elif item.suffix == ".dataset":
                self._parse_dataset(item, renditions, facets)

            elif item.suffix == ".spriteatlas":
                self._parse_spriteatlas(item, renditions, facets)

            elif item.suffix == ".imagestack":
                self._parse_imagestack(item, renditions, facets)

        return renditions, facets

    def _parse_colorset(self, item_path: Path,
                        renditions: list, facets: dict):
        """Parse a .colorset directory."""
        name = item_path.stem
        ident = self._get_identifier(name)
        contents_path = item_path / "Contents.json"
        if not contents_path.exists():
            return

        with open(contents_path) as f:
            contents = json.load(f)

        for color_entry in contents.get("colors", []):
            color_data = color_entry.get("color", {})
            components = color_data.get("components", {})
            if not components:
                continue

            r = float(components.get("red", 0))
            g = float(components.get("green", 0))
            b = float(components.get("blue", 0))
            a = float(components.get("alpha", 1))

            colorspace = color_data.get("color-space", "srgb")
            cs_id = 0  # sRGB

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
                colorspace_id=cs_id,
            )
            rend._csi_override = csi
            renditions.append(rend)

        facets[name] = (car.ELEMENT_UNIVERSAL, car.PART_COLOR, ident)

    def _parse_dataset(self, item_path: Path,
                       renditions: list, facets: dict):
        """Parse a .dataset directory."""
        name = item_path.stem
        ident = self._get_identifier(name)
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

        facets[name] = (car.ELEMENT_UNIVERSAL, car.PART_REGULAR, ident)

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
                        load_image_as_bgra(str(img_path))

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
