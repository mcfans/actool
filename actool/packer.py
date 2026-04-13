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

from dataclasses import dataclass, field

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
    template_rendering_intent: int = 4  # bitmapEncoding: 0=original, 4=automatic, 2=template
    part: int = 181  # PART_REGULAR by default, PART_ICON for icons
    dim2: int = 0  # For icon images, the multisize index
    appearance: int = 0
    direction: int = 0


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
        return f"ZZZZPackedAsset-{self.scale}.{self.dim1}.{fmt_idx}-gamut0"

    @property
    def bytes_per_row(self) -> int:
        """Row stride in bytes for the encoded buffer, aligned to 32 bytes.

        Uses the actual bytes-per-pixel of the atlas format (4 for BGRA,
        2 for GA8). vImage's deepmap2 encoder reads `width * pixel_size`
        bytes per row at this stride.
        """
        bpp = 4 if self.pixel_format == b"BGRA" else 2
        exact = self.width * bpp
        return ((exact + 31) // 32) * 32

    def render(self):
        """Render all packed images into a single atlas pixel buffer.

        Rows are padded to 32-byte alignment to match the encoder's
        expected stride. The buffer uses top-down row order; the INLK
        y coordinates are flipped to bottom-left origin separately by
        the compiler.

        Sub-images are placed at byte offsets matching the atlas's
        actual bytes-per-pixel (2 for GA8, 4 for BGRA), not at a fixed
        4-bpp positioning. This keeps the GA8 atlas consistent so that
        vImage encodes the full pixel content.
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
    """Pack images into atlas(es) using shelf-based packing.

    Uses shelf packing to match Apple's actool layout: images are arranged
    in horizontal shelves, with column stacking within each shelf. When a
    single atlas would exceed the height limit, splits into multiple atlases.

    Returns a single Atlas when all images fit, or the first atlas from
    a multi-atlas split.
    """
    if not images:
        return Atlas()

    atlases = pack_images_split(images, max_width=max_width)
    return atlases[0] if atlases else Atlas()


def pack_images_split(images: list[PackedImage], max_width: int = 2048,
                      max_height: int = 196) -> list[Atlas]:
    """Pack images into multiple atlases using shelf-based bin packing.

    Matches Apple's actool packing strategy:
    - First shelf determines atlas width
    - Subsequent shelves fill within that width
    - Images can be stacked vertically within a shelf (column-within-shelf)
    - When height exceeds max_height, overflow images go to a new atlas
    - When starting a new atlas, widest remaining image goes first
    """
    if not images:
        return []

    remaining = sorted(images, key=lambda i: (-i.height, -i.width))
    atlases = []

    while remaining:
        atlas = Atlas(
            pixel_format=remaining[0].pixel_format,
            scale=remaining[0].scale,
        )

        placed, overflow = _pack_shelf_atlas(
            atlas, remaining, max_width, max_height)
        atlases.append(atlas)
        remaining = overflow

    return atlases


def _pack_shelf_atlas(atlas: Atlas, sorted_imgs: list[PackedImage],
                      max_width: int, max_height: int
                      ) -> tuple[list[PackedImage], list[PackedImage]]:
    """Pack images into a single atlas using shelf-based packing.

    The first shelf can grow the atlas width freely. Subsequent shelves
    are constrained to fit within the width established by the first shelf.

    Returns (placed, overflow) lists.
    """
    # Shelves: each shelf has columns of images
    shelves: list[dict] = []  # {y, height, columns: [{x, width, images, bottom}]}
    atlas_width = 0  # Set by first shelf, constrains subsequent shelves

    placed = []
    overflow = []

    for img in sorted_imgs:
        fit = False

        # Try to fit in an existing shelf's column or as a new column
        for si, shelf in enumerate(shelves):
            is_first_shelf = (si == 0)

            # Try existing columns within this shelf
            for col in shelf['columns']:
                if img.width <= col['width']:
                    new_bottom = col['bottom'] + GAP + img.height
                    if new_bottom <= shelf['y'] + shelf['height']:
                        # Fits within shelf height
                        img.x = col['x']
                        img.y = col['bottom'] + GAP
                        col['images'].append(img)
                        col['bottom'] = img.y + img.height
                        placed.append(img)
                        fit = True
                        break

            if fit:
                break

            # Try new column on this shelf
            if shelf['columns']:
                last_col = shelf['columns'][-1]
                new_x = last_col['x'] + last_col['width'] + GAP
            else:
                new_x = MARGIN

            if img.height <= shelf['height']:
                # First shelf can grow atlas width; others are constrained
                width_ok = (is_first_shelf and
                            new_x + img.width + MARGIN <= max_width)
                if not width_ok and atlas_width > 0:
                    width_ok = new_x + img.width + MARGIN <= atlas_width
                if not width_ok and not shelf['columns']:
                    width_ok = True  # First column always fits

                if width_ok:
                    img.x = new_x
                    img.y = shelf['y']
                    shelf['columns'].append({
                        'x': new_x,
                        'width': img.width,
                        'images': [img],
                        'bottom': img.y + img.height,
                    })
                    # First shelf determines atlas width
                    new_right = new_x + img.width + MARGIN
                    if new_right > atlas_width:
                        atlas_width = new_right
                    placed.append(img)
                    fit = True
                    break

        if fit:
            continue

        # Try new shelf
        if shelves:
            last_shelf = shelves[-1]
            new_y = last_shelf['y'] + last_shelf['height'] + GAP
        else:
            new_y = MARGIN

        if new_y + img.height + MARGIN <= max_height or not shelves:
            shelf_height = img.height
            img.x = MARGIN
            img.y = new_y

            shelf = {
                'y': new_y,
                'height': shelf_height,
                'columns': [{
                    'x': MARGIN,
                    'width': img.width,
                    'images': [img],
                    'bottom': img.y + img.height,
                }],
            }
            shelves.append(shelf)

            # Update atlas width
            new_right = MARGIN + img.width + MARGIN
            if new_right > atlas_width:
                atlas_width = new_right
            placed.append(img)
        else:
            overflow.append(img)

    # Calculate atlas dimensions
    if shelves:
        atlas.width = atlas_width
        max_bottom = 0
        for shelf in shelves:
            for col in shelf['columns']:
                if col['bottom'] > max_bottom:
                    max_bottom = col['bottom']
        atlas.height = max_bottom + MARGIN
    else:
        atlas.width = 0
        atlas.height = 0

    atlas.images = placed
    return placed, overflow


def group_for_packing(renditions) -> tuple[list, list]:
    """Group renditions into packable groups and inline renditions.

    Returns (pack_groups, inline_renditions) where pack_groups is a list of
    (format, scale, renditions_list) tuples.
    """
    from . import car

    # Group by (pixel_format, scale)
    groups: dict[tuple[bytes, int], list] = {}
    force_inline = []

    # Threshold: icon images >= 256px are stored inline
    ICON_INLINE_THRESHOLD = 256
    # Atlas packing limits (images exceeding these go inline)
    PACK_MAX_WIDTH = 262
    PACK_MAX_HEIGHT = 196
    PACK_MARGIN = 4  # 2px margin on each side

    for rend in renditions:
        # Multisize, color, and data renditions are always inline
        if rend.layout in (car.LAYOUT_MULTISIZE_IMAGE, car.LAYOUT_COLOR,
                           car.LAYOUT_RAW_DATA, car.LAYOUT_METADATA):
            force_inline.append(rend)
            continue
        # Large app icon renditions are stored inline
        if rend.part == car.PART_ICON and rend.width >= ICON_INLINE_THRESHOLD:
            force_inline.append(rend)
            continue
        # Renditions with CSI overrides (pre-built) are always inline
        if hasattr(rend, '_csi_override'):
            force_inline.append(rend)
            continue
        # Images too large for atlas packing are stored inline
        if (rend.width >= PACK_MAX_WIDTH - PACK_MARGIN or
                rend.height >= PACK_MAX_HEIGHT - PACK_MARGIN):
            force_inline.append(rend)
            continue
        # Group by format, scale, and sprite atlas
        # Icons pack together with regular images (Apple doesn't separate them)
        atlas_id = rend.sprite_atlas_id  # 0 for regular, non-zero for sprites
        key = (rend.pixel_format, rend.scale, atlas_id)
        if key not in groups:
            groups[key] = []
        groups[key].append(rend)

    # Only pack groups with 2+ distinct imagesets (facets).
    # Appearance variants of the same imageset share an identifier,
    # and the system actool requires at least 2 distinct imagesets
    # before packing into an atlas.
    pack_groups = []
    inline = list(force_inline)

    for (fmt, scale, _atlas_id), rends in sorted(groups.items()):
        distinct_facets = len({r.identifier for r in rends})
        if distinct_facets >= 2:
            pack_groups.append((fmt, scale, rends))
        else:
            inline.extend(rends)

    return pack_groups, inline
