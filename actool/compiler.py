"""
Asset catalog compiler.

Orchestrates the compilation of xcassets into Assets.car, .icns, and .plist files.
"""

import os
import struct
from pathlib import Path

from . import car
from .bom import BOMWriter
from .catalog import AssetCatalog
from .icns import create_icns
from .packer import PackedImage, Atlas, pack_images, group_for_packing


def compile_catalog(xcassets_path: str, output_dir: str, platform: str,
                    min_deploy: str, app_icon: str = None,
                    info_plist_path: str = None,
                    accent_color: str = None,
                    widget_background_color: str = None,
                    standalone_icon_behavior: str = "default",
                    include_languages: list[str] = None,
                    development_region: str = None,
                    plist_localizations: bool = True,
                    warnings_list: list = None,
                    errors_list: list = None,
                    notices_list: list = None):
    """Compile an xcassets catalog into output files."""
    os.makedirs(output_dir, exist_ok=True)
    has_icon = app_icon is not None
    if warnings_list is None:
        warnings_list = []
    if errors_list is None:
        errors_list = []
    if notices_list is None:
        notices_list = []

    # Select keyformat based on whether we have an app icon
    if has_icon:
        keyformat_attrs = car.KEYFORMAT_ATTRS_ICON
    else:
        keyformat_attrs = car.KEYFORMAT_ATTRS_NO_ICON

    # Parse the asset catalog
    catalog = AssetCatalog(xcassets_path, platform, min_deploy, app_icon,
                           include_languages=include_languages,
                           development_region=development_region)
    renditions, facets = catalog.parse()

    # Set has_icon on all renditions for correct key building
    for rend in renditions:
        rend.has_icon = has_icon

    # Group renditions for atlas packing
    pack_groups, inline_renditions = group_for_packing(renditions)

    # Build atlas textures and their references
    all_rendition_entries = []
    dim1_counter = 0

    for fmt, scale, rends in pack_groups:
        # Create packed images
        packed_imgs = []
        for rend in rends:
            packed_imgs.append(PackedImage(
                name=rend.name,
                identifier=rend.identifier,
                width=rend.width,
                height=rend.height,
                pixel_data=rend.pixel_data,
                pixel_format=rend.pixel_format,
                scale=rend.scale,
                is_template=rend.is_template,
            ))

        # Pack into atlas
        atlas = pack_images(packed_imgs)
        atlas.dim1 = dim1_counter
        atlas.render()

        # Create PackedAsset rendition (layout 1004)
        atlas_key = car.make_rendition_key(
            element=car.ELEMENT_PACKED,
            part=car.PART_REGULAR,
            dim1=dim1_counter,
            scale=scale,
            has_icon=has_icon,
        )
        atlas_csi = car.build_packed_asset_csi(
            name=atlas.name,
            width=atlas.width,
            height=atlas.height,
            scale=scale,
            pixel_format=fmt,
            pixel_data=atlas.pixel_data,
        )
        all_rendition_entries.append((atlas_key, atlas_csi))

        # Create PackedImage references (layout 1003) for each image
        for img in atlas.images:
            ref_key = car.make_rendition_key(
                element=car.ELEMENT_UNIVERSAL,
                part=car.PART_REGULAR,
                identifier=img.identifier,
                scale=scale,
                has_icon=has_icon,
            )
            ref_csi = car.build_packed_image_csi(
                name=img.name,
                width=img.width,
                height=img.height,
                scale=scale,
                pixel_format=fmt,
                x=img.x,
                y=img.y,
            )
            all_rendition_entries.append((ref_key, ref_csi))

        dim1_counter += 1

    # Add inline renditions
    for rend in inline_renditions:
        key = rend.build_rendition_key()
        if hasattr(rend, '_csi_override'):
            csi = rend._csi_override
        else:
            csi = rend.build_csi()
        all_rendition_entries.append((key, csi))

    # Sort by key for deterministic output
    all_rendition_entries.sort(key=lambda e: e[0])

    # Build the BOM file
    bom = BOMWriter()

    # Add CARHEADER
    bom.add_named_block("CARHEADER",
                        car.make_carheader(len(all_rendition_entries)))

    # Add KEYFORMAT
    bom.add_named_block("KEYFORMAT", car.make_keyformat(keyformat_attrs))

    # Add EXTENDED_METADATA
    bom.add_named_block("EXTENDED_METADATA",
                        car.make_extended_metadata(platform, min_deploy))

    # Build FACETKEYS tree
    facetkey_entries = []
    for name in sorted(facets.keys()):
        elem, part, ident = facets[name]
        key_data = name.encode("ascii")
        value_data = car.make_facetkey_value(elem, part, ident)
        facetkey_entries.append((key_data, value_data))
    bom.add_tree("FACETKEYS", facetkey_entries)

    # Build RENDITIONS tree
    bom.add_tree("RENDITIONS", all_rendition_entries)

    # Build BITMAPKEYS tree
    bitmapkey_entries = []
    for name in sorted(facets.keys(), key=lambda n: facets[n][2]):
        elem, part, ident = facets[name]
        key_data = struct.pack(">H", ident)
        value_data = _make_bitmap_info(ident, renditions)
        bitmapkey_entries.append((key_data, value_data))
    bom.add_tree("BITMAPKEYS", bitmapkey_entries)

    # Write the CAR file
    car_path = os.path.join(output_dir, "Assets.car")
    bom.write(car_path)

    # Generate ICNS file if app icon is specified
    if app_icon and standalone_icon_behavior != "none":
        icon_images = catalog.get_icon_images()
        if icon_images:
            icns_path = os.path.join(output_dir, f"{app_icon}.icns")
            create_icns(icon_images, icns_path)

    # Generate partial info plist
    if info_plist_path:
        locales = sorted(catalog.get_locales_used()) if plist_localizations else []
        _write_info_plist(info_plist_path, app_icon=app_icon,
                          accent_color=accent_color,
                          widget_background_color=widget_background_color,
                          localizations=locales)

    # Collect output files
    output_files = []
    if app_icon and standalone_icon_behavior != "none":
        icns_path = os.path.join(output_dir, f"{app_icon}.icns")
        if os.path.exists(icns_path):
            output_files.append(os.path.abspath(icns_path))
    car_path_abs = os.path.abspath(car_path)
    output_files.append(car_path_abs)
    if info_plist_path:
        output_files.append(os.path.abspath(info_plist_path))

    return output_files


def _make_bitmap_info(identifier: int,
                      renditions: list[car.Rendition]) -> bytes:
    """Build bitmap info for BITMAPKEYS."""
    count = sum(1 for r in renditions if r.identifier == identifier)
    buf = struct.pack("<I", 1)
    buf += struct.pack("<I", 0)
    buf += struct.pack("<I", count)
    buf += struct.pack("<I", len(car.KEYFORMAT_ATTRS))
    for attr in car.KEYFORMAT_ATTRS:
        buf += struct.pack("<I", 0xFFFFFFFF)
    return buf


def _write_info_plist(path: str, app_icon: str = None,
                      accent_color: str = None,
                      widget_background_color: str = None,
                      localizations: list[str] = None):
    """Write the partial info plist."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
             '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
             '<plist version="1.0">',
             '<dict>']

    if app_icon:
        lines.append('\t<key>CFBundleIconFile</key>')
        lines.append(f'\t<string>{app_icon}</string>')
        lines.append('\t<key>CFBundleIconName</key>')
        lines.append(f'\t<string>{app_icon}</string>')
    if accent_color:
        lines.append('\t<key>NSAccentColorName</key>')
        lines.append(f'\t<string>{accent_color}</string>')
    if widget_background_color:
        lines.append('\t<key>NSWidgetBackgroundColorName</key>')
        lines.append(f'\t<string>{widget_background_color}</string>')
    if localizations:
        lines.append('\t<key>CFBundleLocalizations</key>')
        lines.append('\t<array>')
        for loc in localizations:
            lines.append(f'\t\t<string>{loc}</string>')
        lines.append('\t</array>')

    lines.append('</dict>')
    lines.append('</plist>')
    lines.append('')

    with open(path, "w") as f:
        f.write("\n".join(lines))


