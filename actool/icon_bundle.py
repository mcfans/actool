"""
.icon bundle support.

Handles the modern macOS icon bundle format (icon.json + source image).
The .icon bundle contains a single high-resolution source image that gets
auto-resized to all standard macOS icon sizes.
"""

import json
import os
from pathlib import Path

from PIL import Image

from .icns import create_icns, _make_exif, _make_argb, _reencode_png
from . import car
from .bom import BOMWriter
from .packer import PackedImage, pack_images


# Standard macOS icon point sizes and their scales
ICON_SIZES = [
    (16, 1), (16, 2),
    (32, 1), (32, 2),
    (128, 1), (128, 2),
    (256, 1), (256, 2),
    (512, 1), (512, 2),
]


def is_icon_bundle(path: str) -> bool:
    """Check if the given path is a .icon bundle."""
    p = Path(path)
    return p.suffix == ".icon" and (p / "icon.json").exists()


def compile_icon_bundle(icon_path: str, output_dir: str, platform: str,
                        min_deploy: str, app_icon: str = None,
                        info_plist_path: str = None,
                        accent_color: str = None,
                        standalone_icon_behavior: str = "default",
                        warnings_list: list = None,
                        notices_list: list = None) -> list[str]:
    """Compile a .icon bundle into output files.

    Returns list of output file paths.
    """
    os.makedirs(output_dir, exist_ok=True)
    if warnings_list is None:
        warnings_list = []
    if notices_list is None:
        notices_list = []

    bundle_path = Path(icon_path)
    icon_name = app_icon or bundle_path.stem

    # Parse icon.json
    with open(bundle_path / "icon.json") as f:
        icon_json = json.load(f)

    # Find the source image
    source_image = _find_source_image(bundle_path, icon_json)
    if not source_image:
        warnings_list.append({
            "description": f"No source image found in {icon_path}"
        })
        return []

    # Load source image
    src_img = Image.open(source_image).convert("RGBA")

    # Generate resized icons as temporary files
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="actool_icon_")
    icon_images = []

    try:
        for point_size, scale in ICON_SIZES:
            pixel_size = point_size * scale
            resized = src_img.resize((pixel_size, pixel_size), Image.LANCZOS)
            filename = f"Icon{pixel_size}x{pixel_size}.png"
            filepath = os.path.join(tmpdir, filename)
            resized.save(filepath, format="PNG")
            icon_images.append((filepath, pixel_size, scale))

        output_files = []

        # Generate ICNS
        if standalone_icon_behavior != "none":
            icns_path = os.path.join(output_dir, f"{icon_name}.icns")
            create_icns(icon_images, icns_path)
            if os.path.exists(icns_path):
                output_files.append(os.path.abspath(icns_path))

        # Generate CAR file
        car_path = os.path.join(output_dir, "Assets.car")
        _build_icon_car(car_path, icon_name, icon_images, src_img,
                        platform, min_deploy)
        output_files.append(os.path.abspath(car_path))

        # Generate partial info plist
        if info_plist_path:
            _write_icon_plist(info_plist_path, icon_name, accent_color,
                              notices_list)
            output_files.append(os.path.abspath(info_plist_path))

        return output_files

    finally:
        # Clean up temp files
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def _find_source_image(bundle_path: Path, icon_json: dict) -> str:
    """Find the source image in the .icon bundle."""
    # Check layers in groups for image references
    for group in icon_json.get("groups", []):
        for layer in group.get("layers", []):
            image_name = layer.get("image-name")
            if image_name:
                # Look in Assets directory
                assets_path = bundle_path / "Assets" / image_name
                if assets_path.exists():
                    return str(assets_path)
                # Look in bundle root
                root_path = bundle_path / image_name
                if root_path.exists():
                    return str(root_path)
    return None


def _build_icon_car(car_path: str, icon_name: str, icon_images: list,
                    src_img, platform: str, min_deploy: str):
    """Build a CAR file from icon images."""
    from .catalog import load_image_as_bgra, _hash_name

    ident = _hash_name(icon_name)
    renditions = []

    # Create renditions for each icon size
    for img_path, pixel_size, scale in icon_images:
        pixel_data, width, height, pixel_format = load_image_as_bgra(img_path)

        dim2 = {16: 1, 32: 2, 128: 3, 256: 4, 512: 5}.get(
            pixel_size // scale, 0)

        rend = car.Rendition(
            name=os.path.basename(img_path),
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

    # Add multisize image rendition
    ms_entries = []
    seen = set()
    for img_path, pixel_size, scale in icon_images:
        point_size = pixel_size // scale
        if point_size not in seen:
            dim2 = {16: 1, 32: 2, 128: 3, 256: 4, 512: 5}.get(point_size, 0)
            ms_entries.append(car.MultisizeImageEntry(
                width=point_size, height=point_size, index=dim2))
            seen.add(point_size)

    ms_rend = car.build_multisize_rendition(icon_name, ident, ms_entries)
    renditions.append(ms_rend)

    # Build rendition entries
    all_entries = []
    for rend in renditions:
        key = rend.build_rendition_key()
        if hasattr(rend, '_csi_override'):
            csi = rend._csi_override
        else:
            csi = rend.build_csi()
        all_entries.append((key, csi))

    all_entries.sort(key=lambda e: e[0])

    # Build BOM file
    bom = BOMWriter()
    bom.add_named_block("CARHEADER", car.make_carheader(len(all_entries)))
    bom.add_named_block("KEYFORMAT", car.make_keyformat(
        car.KEYFORMAT_ATTRS_ICON))
    bom.add_named_block("EXTENDED_METADATA",
                        car.make_extended_metadata(platform, min_deploy))

    facetkey_entries = [(icon_name.encode("ascii"),
                         car.make_facetkey_value(car.ELEMENT_UNIVERSAL,
                                                car.PART_ICON, ident))]
    bom.add_tree("FACETKEYS", facetkey_entries)
    bom.add_tree("RENDITIONS", all_entries)
    bom.add_tree("BITMAPKEYS", [])
    bom.write(car_path)


def _write_icon_plist(path: str, icon_name: str, accent_color: str = None,
                      notices_list: list = None):
    """Write the partial info plist for an icon bundle."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
             '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
             '<plist version="1.0">',
             '<dict>']
    lines.append(f'\t<key>CFBundleIconFile</key>')
    lines.append(f'\t<string>{icon_name}</string>')
    lines.append(f'\t<key>CFBundleIconName</key>')
    lines.append(f'\t<string>{icon_name}</string>')
    if accent_color:
        # Check if accent color is a real color (we don't have color sets)
        if notices_list is not None:
            notices_list.append({
                "description": f"Accent color '{accent_color}' is not "
                               f"present in any asset catalogs."
            })
    lines.append('</dict>')
    lines.append('</plist>')
    lines.append('')

    with open(path, "w") as f:
        f.write("\n".join(lines))
