"""
Atlas packing for CAR files.

Implements a column-first bin-packing algorithm that matches Apple's actool
output. Images are grouped by pixel format and scale, then packed into
atlas textures.

Packing rules:
- 2px margin on all edges, 2px gap between images
- Sorted by height descending, then width descending
- Column-based: stack images vertically in columns
- When a column is full, start a new column to the right
- Minimum 2 images per format/scale group to trigger packing
"""

import struct
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image


MARGIN = 2
GAP = 2


@dataclass
class PackedImage:
    """An image placed in an atlas."""
    name: str
    identifier: int
    width: int
    height: int
    x: int = 0
    y: int = 0
    pixel_data: bytes = b""
    pixel_format: bytes = b"BGRA"
    scale: int = 1
    is_template: bool = False
    part: int = 181  # PART_REGULAR by default, PART_ICON for icons
    dim2: int = 0  # For icon images, the multisize index


@dataclass
class Atlas:
    """A packed atlas texture containing multiple images."""
    width: int = 0
    height: int = 0
    pixel_format: bytes = b"BGRA"
    scale: int = 1
    dim1: int = 0
    images: list[PackedImage] = field(default_factory=list)
    pixel_data: bytes = b""

    @property
    def name(self) -> str:
        fmt_idx = 0 if self.pixel_format == b"BGRA" else 1
        return f"ZZZZPackedAsset-{self.scale}.0.{fmt_idx}-gamut0"

    @property
    def bytes_per_row(self) -> int:
        """Row stride in bytes, aligned to 32 bytes."""
        bpp = 4 if self.pixel_format == b"BGRA" else 2
        exact = self.width * bpp
        return ((exact + 31) // 32) * 32

    def render(self):
        """Render all packed images into a single atlas pixel buffer.

        Rows are padded to 32-byte alignment to match CoreUI's expected
        stride. The buffer uses top-down row order; the INLK y coordinates
        are flipped to bottom-left origin separately by the compiler.
        """
        bpp = 4 if self.pixel_format == b"BGRA" else 2
        bpr = self.bytes_per_row
        buf = bytearray(bpr * self.height)

        for img in self.images:
            src_stride = img.width * bpp

            for row in range(img.height):
                src_off = row * src_stride
                dst_off = (img.y + row) * bpr + img.x * bpp
                buf[dst_off:dst_off + src_stride] = \
                    img.pixel_data[src_off:src_off + src_stride]

        self.pixel_data = bytes(buf)


def pack_images(images: list[PackedImage], max_width: int = 2048) -> Atlas:
    """Pack images into an atlas using column-based packing.

    Uses a column-first approach: sorts by height desc, packs into columns,
    then fills remaining space with shorter images.
    """
    if not images:
        return Atlas()

    atlas = Atlas(
        pixel_format=images[0].pixel_format,
        scale=images[0].scale,
    )

    # Sort by height descending, then width descending
    sorted_imgs = sorted(images, key=lambda i: (-i.height, -i.width))

    # Column-based packing
    columns: list[list[PackedImage]] = []
    col_x_positions: list[int] = []
    col_widths: list[int] = []
    col_heights: list[int] = []

    for img in sorted_imgs:
        placed = False

        # Try to place in an existing column (if image fits in width)
        for ci in range(len(columns)):
            col_w = col_widths[ci]
            col_h = col_heights[ci]

            if img.width <= col_w:
                # Check if there's vertical space
                # No explicit height limit, just pack vertically
                img.x = col_x_positions[ci]
                img.y = col_h + GAP
                columns[ci].append(img)
                col_heights[ci] = img.y + img.height
                placed = True
                break

        if not placed:
            # Start a new column
            if columns:
                new_x = col_x_positions[-1] + col_widths[-1] + GAP
            else:
                new_x = MARGIN

            if new_x + img.width + MARGIN <= max_width or not columns:
                img.x = new_x
                img.y = MARGIN
                columns.append([img])
                col_x_positions.append(new_x)
                col_widths.append(img.width)
                col_heights.append(img.y + img.height)
            else:
                # Shouldn't happen with reasonable max_width, but handle it
                img.x = MARGIN
                img.y = max(col_heights) + GAP
                if columns:
                    columns[0].append(img)
                    col_heights[0] = img.y + img.height
                else:
                    columns.append([img])
                    col_x_positions.append(MARGIN)
                    col_widths.append(img.width)
                    col_heights.append(img.y + img.height)

    # Calculate atlas dimensions
    if columns:
        atlas.width = max(cx + cw for cx, cw in zip(col_x_positions, col_widths)) + MARGIN
        atlas.height = max(col_heights) + MARGIN
    else:
        atlas.width = 0
        atlas.height = 0

    # Collect all placed images
    for col in columns:
        atlas.images.extend(col)

    return atlas


def group_for_packing(renditions) -> tuple[list, list]:
    """Group renditions into packable groups and inline renditions.

    Returns (pack_groups, inline_renditions) where pack_groups is a list of
    (format, scale, renditions_list) tuples.
    """
    from . import car

    # Group by (pixel_format, scale)
    groups: dict[tuple[bytes, int], list] = {}
    icon_renditions = []

    # Threshold: icon images >= 256px are stored inline
    ICON_INLINE_THRESHOLD = 256

    for rend in renditions:
        # Multisize, color, and data renditions are always inline
        if rend.layout in (car.LAYOUT_MULTISIZE_IMAGE, car.LAYOUT_COLOR,
                           car.LAYOUT_RAW_DATA, car.LAYOUT_METADATA):
            icon_renditions.append(rend)
            continue
        # Large app icon renditions are stored inline
        if rend.part == car.PART_ICON and rend.width >= ICON_INLINE_THRESHOLD:
            icon_renditions.append(rend)
            continue
        # Renditions with CSI overrides (pre-built) are always inline
        if hasattr(rend, '_csi_override'):
            icon_renditions.append(rend)
            continue

        # Group by format, scale, icon status, and sprite atlas
        is_icon = rend.part == car.PART_ICON
        atlas_id = rend.sprite_atlas_id  # 0 for regular, non-zero for sprites
        key = (rend.pixel_format, rend.scale, is_icon, atlas_id)
        if key not in groups:
            groups[key] = []
        groups[key].append(rend)

    # Only pack groups with 2+ images
    pack_groups = []
    inline = list(icon_renditions)

    for (fmt, scale, _is_icon, _atlas_id), rends in sorted(groups.items()):
        if len(rends) >= 2:
            pack_groups.append((fmt, scale, rends))
        else:
            inline.extend(rends)

    return pack_groups, inline
