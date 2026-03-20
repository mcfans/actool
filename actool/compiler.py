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

    # Parse the asset catalog
    catalog = AssetCatalog(xcassets_path, platform, min_deploy, app_icon,
                           include_languages=include_languages,
                           development_region=development_region)
    renditions, facets = catalog.parse()

    # Group renditions for atlas packing
    pack_groups, inline_renditions = group_for_packing(renditions)

    # Compute dynamic keyformat: include dim1 if packing produces multiple groups
    # (packed assets use dim1_counter as dim1 values)
    uses_dim1 = len(pack_groups) > 1
    keyformat_attrs = car.compute_keyformat(renditions, force_dim1=uses_dim1)

    # Set keyformat and deployment info on all renditions
    for rend in renditions:
        rend.has_icon = has_icon
        rend.keyformat = keyformat_attrs
        rend.min_deploy = min_deploy
        rend.platform = platform

    # Build atlas textures and their references
    all_rendition_entries = []
    dim1_counter = 0

    for fmt, scale, rends in pack_groups:
        # Check if this is a sprite atlas group
        sprite_atlas_id = rends[0].sprite_atlas_id if rends else 0

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
                part=rend.part,
                dim2=rend.dim2,
            ))

        # Pack into atlas
        atlas = pack_images(packed_imgs)
        atlas.dim1 = dim1_counter
        atlas.render()

        # Determine atlas name and key
        if sprite_atlas_id:
            # Explicit packed asset for sprite atlas
            atlas_name = atlas.name.replace("ZZZZPackedAsset",
                                            "ZZZZExplicitlyPackedAsset")
            atlas_key = car.make_rendition_key(
                element=car.ELEMENT_PACKED,
                part=car.PART_REGULAR,
                identifier=sprite_atlas_id,
                dim1=dim1_counter,
                scale=scale,
                keyformat=keyformat_attrs,
            )
        else:
            atlas_name = atlas.name
            atlas_key = car.make_rendition_key(
                element=car.ELEMENT_PACKED,
                part=car.PART_REGULAR,
                dim1=dim1_counter,
                scale=scale,
                keyformat=keyformat_attrs,
            )

        atlas_csi = car.build_packed_asset_csi(
            name=atlas_name,
            width=atlas.width,
            height=atlas.height,
            scale=scale,
            pixel_format=fmt,
            pixel_data=atlas.pixel_data,
            min_deploy=min_deploy,
            platform=platform,
        )
        all_rendition_entries.append((atlas_key, atlas_csi))

        # Create PackedImage references (layout 1003) for each image
        for img in atlas.images:
            ref_key = car.make_rendition_key(
                element=car.ELEMENT_UNIVERSAL,
                part=img.part,
                identifier=img.identifier,
                dim2=img.dim2,
                scale=scale,
                keyformat=keyformat_attrs,
            )
            ref_csi = car.build_packed_image_csi(
                name=img.name,
                width=img.width,
                height=img.height,
                scale=scale,
                pixel_format=fmt,
                x=img.x,
                y=img.y,
                atlas_identifier=sprite_atlas_id,
                atlas_dim1=dim1_counter,
            )
            all_rendition_entries.append((ref_key, ref_csi))

        dim1_counter += 1

    # Add sprite atlas metadata renditions
    # Collect sprite names per atlas
    atlas_sprites: dict[int, list[str]] = {}
    for name, (elem, part, ident) in facets.items():
        for rend in renditions:
            if rend.sprite_atlas_id and rend.identifier == ident:
                aid = rend.sprite_atlas_id
                if aid not in atlas_sprites:
                    atlas_sprites[aid] = []
                if name not in atlas_sprites[aid]:
                    atlas_sprites[aid].append(name)
                break

    for atlas_id, sprite_names in atlas_sprites.items():
        meta_key = car.make_rendition_key(
            element=car.ELEMENT_PACKED,
            part=car.PART_SPRITE_ATLAS,
            identifier=atlas_id, scale=1,
            keyformat=keyformat_attrs,
        )
        if not any(k == meta_key for k, _ in all_rendition_entries):
            meta_csi = car.build_sprite_atlas_metadata_csi(
                "CoreStructuredImage",
                sprite_names=sorted(sprite_names))
            all_rendition_entries.append((meta_key, meta_csi))

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

    # Build BITMAPKEYS tree (raw identifier keys, not block refs)
    bitmapkey_entries = _build_bitmapkeys(
        facets, all_rendition_entries, keyformat_attrs, has_icon)
    bom.add_raw_key_tree("BITMAPKEYS", bitmapkey_entries)

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


def _build_bitmapkeys(facets, rendition_entries, keyformat_attrs, has_icon):
    """Build BITMAPKEYS entries from facets and rendition entries.

    Returns list of (identifier_uint32, value_bytes) sorted by identifier.
    Each value is a 56-byte block with attribute bitmasks showing which
    attribute values are used across renditions of each facet.
    """
    # Wildcard attrs: element, part, identifier (constant per facet)
    wildcard_attrs = {1, 2, 17}  # Element, Part, Identifier

    # Group rendition keys by identifier
    id_keys: dict[int, list[list[int]]] = {}
    for key_data, _csi in rendition_entries:
        n_vals = len(key_data) // 2
        vals = struct.unpack(f"<{n_vals}H", key_data)
        # Find identifier (attr 17) position in keyformat
        id_pos = keyformat_attrs.index(17) if 17 in keyformat_attrs else -1
        if id_pos >= 0 and id_pos < len(vals):
            ident = vals[id_pos]
            if ident not in id_keys:
                id_keys[ident] = []
            id_keys[ident].append(list(vals))

    entries = []
    for name, (elem, part, ident) in sorted(facets.items(), key=lambda x: x[1][2]):
        if ident == 0:
            continue  # Skip packed asset entries (id=0)

        # Compute bitmasks for each attribute
        attr_masks = []
        keys_for_id = id_keys.get(ident, [])

        for i, attr_id in enumerate(keyformat_attrs):
            if attr_id in wildcard_attrs:
                attr_masks.append(0xFFFFFFFF)
            else:
                bitmask = 0
                for key_vals in keys_for_id:
                    if i < len(key_vals):
                        v = key_vals[i]
                        if v < 32:
                            bitmask |= (1 << v)
                attr_masks.append(bitmask if bitmask else 1)

        # Build value block: version(4) + unknown(4) + data_size(4) +
        # n_attrs(4) + attrs[n_attrs](4 each)
        n_attrs = len(keyformat_attrs)
        data_size = 4 + n_attrs * 4  # n_attrs field + attr values
        value = struct.pack("<III", 1, 0, data_size)
        value += struct.pack("<I", n_attrs)
        for mask in attr_masks:
            value += struct.pack("<I", mask)

        entries.append((ident, value))

    return entries


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


