"""
CAR (Core Asset Repository) format structures.

CAR files use a BOM container with specific named blocks for
asset catalog data. All CAR-internal structures use little-endian byte order.
"""

import struct
import uuid
from dataclasses import dataclass, field

from .name_hash import hash_name as _hash_name

try:
    import liblzfse as lzfse
    HAS_LZFSE = True
except ImportError:
    HAS_LZFSE = False


# Rendition attribute types (key format tokens)
# 7=ThemeAppearance, 13=Unknown13, 1=Element, 2=Part, 3=Size,
# 17=Identifier, 8=Dimension1, 9=Dimension2, 11=Layer, 12=Scale
#
# Base tokens always included:
KEYFORMAT_BASE = [7, 13, 1, 2, 3, 17, 11, 12]
# Optional tokens included only when used by any rendition:
KEYFORMAT_OPTIONAL = {4, 8, 9}  # Direction, Dim1, Dim2
# Full ordered list (insertion position matters for key construction):
KEYFORMAT_ALL = [7, 13, 1, 2, 3, 4, 17, 8, 9, 11, 12]
# Legacy aliases
KEYFORMAT_ATTRS_ICON = KEYFORMAT_ALL
KEYFORMAT_ATTRS_NO_ICON = [7, 13, 1, 2, 3, 4, 17, 8, 11, 12]
KEYFORMAT_ATTRS = KEYFORMAT_ALL

# Language direction values (token 4)
DIRECTION_DEFAULT = 0
DIRECTION_RTL = 4
DIRECTION_LTR = 5

ELEMENT_UNIVERSAL = 85  # 0x55
ELEMENT_PACKED = 9  # Element for packed assets
PART_ICON = 220  # 0xDC - app icon part
PART_ICON_MULTISIZE = 218  # 0xDA - multisize image descriptor
PART_REGULAR = 181  # 0xB5
PART_COLOR = 217  # 0xD9 - color rendition
PART_SPRITE_ATLAS = 127  # 0x7F - sprite atlas metadata

LAYOUT_PDF = 9
LAYOUT_ONE_PART_SCALE = 12
LAYOUT_RAW_DATA = 1000
LAYOUT_PACKED_IMAGE = 1003
LAYOUT_NAME_LIST = 1004  # PackedAsset
LAYOUT_METADATA = 1005  # CoreStructuredImage for sprite atlas metadata
LAYOUT_COLOR = 1009
LAYOUT_MULTISIZE_IMAGE = 1010

# Pixel format for raw data
PIXELFMT_DATA = b"ATAD"  # 'DATA' as LE uint32
PIXELFMT_PDF = b" FDP"   # 'PDF ' as LE uint32


def compute_keyformat(renditions, force_dim1: bool = False) -> list[int]:
    """Compute the dynamic KEYFORMAT based on which attributes are used.

    Only includes optional tokens (Direction, Dim1, Dim2) if any rendition
    uses a non-zero value for them. force_dim1 includes Dim1 even if no
    rendition explicitly uses it (needed when packed assets will use it).
    """
    used_direction = any(r.direction != 0 for r in renditions)
    used_dim1 = force_dim1 or any(r.dim1 != 0 for r in renditions)
    used_dim2 = any(r.dim2 != 0 for r in renditions)

    tokens = []
    for t in KEYFORMAT_ALL:
        if t in KEYFORMAT_OPTIONAL:
            if t == 4 and not used_direction:
                continue
            if t == 8 and not used_dim1:
                continue
            if t == 9 and not used_dim2:
                continue
        tokens.append(t)
    return tokens


def make_carheader(rendition_count: int) -> bytes:
    """Build a CARHEADER block."""
    buf = bytearray(436)
    buf[0:4] = b"RATC"  # 'CTAR' as LE uint32
    struct.pack_into("<I", buf, 4, 972)  # coreuiVersion
    struct.pack_into("<I", buf, 8, 17)  # storageVersion
    struct.pack_into("<I", buf, 12, 0)  # storageTimestamp
    struct.pack_into("<I", buf, 16, rendition_count)
    # mainVersionString (128 bytes at offset 20)
    main_ver = b"@(#)PROGRAM:CoreUI  PROJECT:CoreUI-972.1\n"
    buf[20:20 + len(main_ver)] = main_ver
    # versionString (256 bytes at offset 148)
    ver_str = b"IBCocoaTouchImageCatalogTool-17.0\n"
    buf[148:148 + len(ver_str)] = ver_str
    # uuid (16 bytes at offset 404) - all zeros
    # associatedChecksum (4 bytes at offset 420) - 0
    struct.pack_into("<I", buf, 424, 2)  # schemaVersion
    struct.pack_into("<I", buf, 428, 1)  # colorSpaceID (sRGB)
    struct.pack_into("<I", buf, 432, 1)  # keySemantics
    return bytes(buf)


def make_extended_metadata(platform: str, min_deploy: str) -> bytes:
    """Build an EXTENDED_METADATA block."""
    buf = bytearray(1028)
    buf[0:4] = b"META"  # Tag stored as literal bytes
    # thinningArguments (256 bytes at offset 4) - empty
    # deploymentPlatformVersion (256 bytes at offset 260)
    deploy_ver = min_deploy.encode("ascii")
    buf[260:260 + len(deploy_ver)] = deploy_ver
    # deploymentPlatform (256 bytes at offset 516)
    plat = platform.encode("ascii")
    buf[516:516 + len(plat)] = plat
    # authoringTool (256 bytes at offset 772)
    tool = b"actool"
    buf[772:772 + len(tool)] = tool
    return bytes(buf)


def make_keyformat(attrs: list[int] = None) -> bytes:
    """Build a KEYFORMAT block."""
    if attrs is None:
        attrs = KEYFORMAT_ATTRS
    buf = b"tmfk" + struct.pack("<II", 0, len(attrs))  # 'kfmt' as LE
    for attr in attrs:
        buf += struct.pack("<I", attr)
    return buf


def make_rendition_key(appearance: int = 0, unknown13: int = 0,
                       element: int = 0, part: int = 0, size: int = 0,
                       direction: int = 0,
                       identifier: int = 0, dim1: int = 0, dim2: int = 0,
                       layer: int = 0, scale: int = 0,
                       keyformat: list[int] = None,
                       has_icon: bool = True) -> bytes:
    """Build a rendition key matching the given keyformat tokens."""
    attr_values = {
        7: appearance, 13: unknown13, 1: element, 2: part, 3: size,
        4: direction, 17: identifier, 8: dim1, 9: dim2, 11: layer,
        12: scale,
    }
    if keyformat is not None:
        vals = [attr_values.get(t, 0) for t in keyformat]
        return struct.pack(f"<{len(vals)}H", *vals)
    # Legacy fallback
    if has_icon:
        return struct.pack("<10H", appearance, unknown13, element, part, size,
                           identifier, dim1, dim2, layer, scale)
    else:
        return struct.pack("<9H", appearance, unknown13, element, part, size,
                           identifier, dim1, layer, scale)


def make_facetkey_value(element: int, part: int, identifier: int) -> bytes:
    """Build a renditionkeytoken value for FACETKEYS."""
    # cursorHotSpot (4 bytes) + numberOfAttributes (2 bytes) + attributes
    attrs = [(1, element)]
    if part is not None:
        attrs.append((2, part))
    attrs.append((17, identifier))
    buf = struct.pack("<HHH", 0, 0, len(attrs))
    for name, val in attrs:
        buf += struct.pack("<HH", name, val)
    return buf


def _parse_version(ver_str: str) -> tuple:
    """Parse '10.11' or '11.0' into a comparable tuple."""
    try:
        return tuple(int(x) for x in ver_str.split("."))
    except (ValueError, AttributeError):
        return (0,)


# LZFSE was introduced in macOS 10.11 / iOS 9.0.
_MIN_LZFSE_VERSION = {"macosx": (10, 11), "iphoneos": (9, 0),
                       "appletvos": (9, 0), "watchos": (2, 0)}

# Deepmap2 (DMP2) is used by the system actool for macOS >= 11.0.
_MIN_DMP2_VERSION = {"macosx": (11, 0), "iphoneos": (14, 0),
                      "appletvos": (14, 0), "watchos": (7, 0)}


def compress_data(pixel_data: bytes, pixel_format: bytes,
                  width: int, height: int,
                  min_deploy: str = "10.11",
                  platform: str = "macosx",
                  allow_dmp2: bool = False,
                  dmp2_inline: bool = False) -> bytes:
    """Compress pixel data and return the rendition payload (CELM block).

    Compression selection:
    - macOS >= 11.0 with allow_dmp2: DMP2 via vImage
      - dmp2_inline=True: CELM ver=0, raw DMP2 (for inline images)
      - dmp2_inline=False: CELM ver=0 with sub-header (for packed atlases)
    - All targets: gzip (CELM ver=0, comp=2) for data > 256 bytes
    - Fallback: uncompressed (CELM ver=0, comp=0)
    """
    from . import deepmap2

    deploy_ver = _parse_version(min_deploy)

    # Try DMP2 on deployment targets that support it
    if allow_dmp2:
        dmp2_min = _MIN_DMP2_VERSION.get(platform, (11, 0))
        if deploy_ver >= dmp2_min and len(pixel_data) > 256:
            dmp2_data = deepmap2.encode(pixel_data, pixel_format, width, height)
            if dmp2_data is not None:
                return deepmap2.make_celm_dmp2(dmp2_data, pixel_format,
                                               inline=dmp2_inline)

    # Gzip compression (CELM ver=0, comp=2).
    # The system actool uses gzip for pre-LZFSE targets (macOS < 10.11)
    # and KCBC-chunked LZFSE for >= 10.11.  We use gzip for both since
    # we don't implement the KCBC chunked container, and CoreUI decodes
    # gzip at all deployment targets.
    if len(pixel_data) > 256:
        import zlib
        gz = zlib.compress(pixel_data, wbits=15 + 16)  # gzip format
        if len(gz) < len(pixel_data):
            celm = struct.pack("<4sIII", b"MLEC", 0, 2, len(gz))
            return celm + gz

    # Uncompressed fallback
    celm = struct.pack("<4sIII", b"MLEC", 0, 0, len(pixel_data))
    return celm + pixel_data


def make_csi_header(width: int, height: int, scale_factor: int,
                    pixel_format: bytes, layout: int, name: str,
                    rendition_flags: int = 0, colorspace_id: int = 1) -> bytes:
    """Build a CSI header (184 bytes)."""
    buf = bytearray(184)
    buf[0:4] = b"ISTC"  # 'CTSI' as LE uint32
    struct.pack_into("<I", buf, 4, 1)  # version
    struct.pack_into("<I", buf, 8, rendition_flags)
    struct.pack_into("<I", buf, 12, width)
    struct.pack_into("<I", buf, 16, height)
    struct.pack_into("<I", buf, 20, scale_factor)
    buf[24:28] = pixel_format
    struct.pack_into("<I", buf, 28, colorspace_id & 0xF)
    # csimetadata
    struct.pack_into("<I", buf, 32, 0)  # modtime
    struct.pack_into("<H", buf, 36, layout)
    struct.pack_into("<H", buf, 38, 0)  # zero
    # name (128 bytes at offset 40)
    name_bytes = name.encode("ascii")[:127]
    buf[40:40 + len(name_bytes)] = name_bytes
    # csibitmaplist fields are set later
    return bytes(buf)


def build_csi(width: int, height: int, scale_factor: int,
              pixel_format: bytes, layout: int, name: str,
              tlv_data: bytes = b"", rendition_data: bytes = b"",
              rendition_flags: int = 0, colorspace_id: int = 1,
              bitmaplist_unknown: int = 0) -> bytes:
    """Build a complete CSI block (header + TLV + rendition data)."""
    header = bytearray(make_csi_header(width, height, scale_factor,
                                        pixel_format, layout, name,
                                        rendition_flags, colorspace_id))
    # Fill in csibitmaplist
    struct.pack_into("<I", header, 168, len(tlv_data))  # tvlLength
    struct.pack_into("<I", header, 172, bitmaplist_unknown)  # unknown
    struct.pack_into("<I", header, 176, 0)  # zero
    struct.pack_into("<I", header, 180, len(rendition_data))  # renditionLength
    return bytes(header) + tlv_data + rendition_data


def make_slices_tlv(width: int, height: int) -> bytes:
    """Build a Slices TLV entry."""
    # tag(4) + length(4) + data
    slice_data = struct.pack("<IIIII", 1, 0, 0, width, height)
    return struct.pack("<II", 0x03E9, len(slice_data)) + slice_data


def make_metrics_tlv(width: int, height: int) -> bytes:
    """Build a Metrics TLV entry."""
    metrics_data = struct.pack("<IIIIIII", 1, 0, 0, 0, 0, width, height)
    return struct.pack("<II", 0x03EB, len(metrics_data)) + metrics_data


def make_blend_opacity_tlv() -> bytes:
    """Build a BlendModeAndOpacity TLV (default: normal blend, opacity=1.0)."""
    blend_data = struct.pack("<If", 0, 1.0)
    return struct.pack("<II", 0x03EC, len(blend_data)) + blend_data


def make_exif_orientation_tlv(orientation: int = 1) -> bytes:
    """Build an EXIFOrientation TLV."""
    data = struct.pack("<I", orientation)
    return struct.pack("<II", 0x03EE, len(data)) + data


def _check_opaque(pixel_data: bytes, pixel_format: bytes,
                  width: int, height: int) -> bool:
    """Check if all alpha values in pixel data are 255 (fully opaque)."""
    if pixel_format == b"BGRA":
        # Alpha is byte 3 of each 4-byte pixel
        bpr = width * 4
        for row in range(height):
            row_start = row * bpr
            for col in range(width):
                if pixel_data[row_start + col * 4 + 3] != 255:
                    return False
        return True
    elif pixel_format == b" 8AG":
        # Alpha is byte 1 of each 2-byte pixel
        bpr = width * 2
        for row in range(height):
            row_start = row * bpr
            for col in range(width):
                if pixel_data[row_start + col * 2 + 1] != 255:
                    return False
        return True
    return False


def aligned_bytes_per_row(width: int, pixel_format: bytes) -> int:
    """Calculate row stride aligned to 32 bytes.

    CoreUI always computes stride using 4 bytes per pixel regardless of
    the actual pixel format (even GA8 which is 2 bpp).  Mismatched
    strides cause BOMStream buffer overflows in CoreUI at runtime.
    """
    exact = width * 4
    return ((exact + 31) // 32) * 32


def make_bytes_per_row_tlv(width: int, pixel_format: bytes,
                           aligned: bool = True) -> bytes:
    """Build a BytesPerRow TLV (0x03EF).

    When aligned=True (packed atlases), uses 32-byte row alignment.
    When aligned=False (inline images), uses exact width*4 stride.
    """
    if aligned:
        bpr = aligned_bytes_per_row(width, pixel_format)
    else:
        bpr = width * 4
    data = struct.pack("<I", bpr)
    return struct.pack("<II", 0x03EF, len(data)) + data


def make_inlk_tlv(x: int, y: int, width: int, height: int,
                   pixel_format: bytes, scale: int,
                   atlas_identifier: int = 0,
                   atlas_dim1: int = 0) -> bytes:
    """Build an INLK TLV (0x03f2) for packed image references.

    atlas_identifier: if non-zero, this is a sprite atlas reference and the
    atlas ID is included in the trailing key attributes.
    atlas_dim1: the Dim1 value of the target atlas rendition.
    """
    # KLNI tag + version + x + y + w + h
    inlk = struct.pack("<4sI", b"KLNI", 0)
    inlk += struct.pack("<IIII", x, y, width, height)
    # Build trailing attribute data: padding(2) + (token_id, value) pairs + terminator(2)
    # CoreUI reads: skip 1 uint16 padding, then read pairs until token_id=0
    attr_data = struct.pack("<H", 0)  # padding: single uint16
    attr_data += struct.pack("<HH", 1, ELEMENT_PACKED)  # Element = 9
    attr_data += struct.pack("<HH", 2, PART_REGULAR)  # Part = 181
    if atlas_dim1:
        attr_data += struct.pack("<HH", 8, atlas_dim1)  # Dim1 (omitted when 0)
    if atlas_identifier:
        attr_data += struct.pack("<HH", 17, atlas_identifier)  # Identifier
    attr_data += struct.pack("<HH", 12, scale)  # Scale
    attr_data += struct.pack("<H", 0)  # terminator: single uint16
    # Header: constant (12) + attr data byte count
    inlk += struct.pack("<HH", 12, len(attr_data))
    inlk += attr_data
    return struct.pack("<II", 0x03F2, len(inlk)) + inlk


def build_packed_image_csi(name: str, width: int, height: int,
                           scale: int, pixel_format: bytes,
                           x: int, y: int,
                           atlas_identifier: int = 0,
                           atlas_dim1: int = 0,
                           rendition_flags: int = 0) -> bytes:
    """Build a CSI for a packed image reference (layout 1003)."""
    scale_factor = scale * 100

    # TLV section
    tlv = make_slices_tlv(width, height)
    tlv += make_metrics_tlv(width, height)
    tlv += make_inlk_tlv(x, y, width, height, pixel_format, scale,
                         atlas_identifier=atlas_identifier,
                         atlas_dim1=atlas_dim1)
    tlv += make_blend_opacity_tlv()
    tlv += make_exif_orientation_tlv()

    cs_id = 2 if pixel_format == b" 8AG" else 1
    return build_csi(
        width=width, height=height, scale_factor=scale_factor,
        pixel_format=pixel_format, layout=LAYOUT_PACKED_IMAGE,
        name=name, tlv_data=tlv, rendition_data=b"",
        rendition_flags=rendition_flags,
        colorspace_id=cs_id, bitmaplist_unknown=1,
    )


def build_packed_asset_csi(name: str, width: int, height: int,
                           scale: int, pixel_format: bytes,
                           pixel_data: bytes,
                           min_deploy: str = "10.11",
                           platform: str = "macosx") -> bytes:
    """Build a CSI for a packed asset atlas (layout 1004)."""
    scale_factor = scale * 100

    # TLV section (packed assets: slices + blend + exif + bytes_per_row, NO metrics)
    # Atlas slices must be (0,0) — non-zero dimensions interfere with INLK extraction
    tlv = make_slices_tlv(0, 0)
    tlv += make_blend_opacity_tlv()
    tlv += make_exif_orientation_tlv()
    tlv += make_bytes_per_row_tlv(width, pixel_format)

    # Compress the atlas pixel data (DMP2 eligible for packed atlases)
    rend_data = compress_data(pixel_data, pixel_format, width, height,
                              min_deploy=min_deploy, platform=platform,
                              allow_dmp2=True)

    # GA8 images use colorspace 2 (gray gamma 2.2)
    cs_id = 2 if pixel_format == b" 8AG" else 1

    return build_csi(
        width=width, height=height, scale_factor=scale_factor,
        pixel_format=pixel_format, layout=LAYOUT_NAME_LIST,
        name=name, tlv_data=tlv, rendition_data=rend_data,
        colorspace_id=cs_id, bitmaplist_unknown=1,
    )


def make_color_blend_opacity_tlv() -> bytes:
    """Build a BlendModeAndOpacity TLV for color renditions (opacity=0.0)."""
    blend_data = struct.pack("<If", 0, 0.0)
    return struct.pack("<II", 0x03EC, len(blend_data)) + blend_data


def build_color_csi(name: str, red: float, green: float, blue: float,
                    alpha: float, colorspace_id: int = 1) -> bytes:
    """Build a CSI for a color rendition (layout 1009)."""
    # Color rendition data: COLR tag + version + colorspace + count + components
    colr = struct.pack("<4sI", b"RLOC", 1)  # 'COLR' as LE, version=1
    colr += struct.pack("<I", colorspace_id & 0xFF)  # colorspace byte (1=sRGB)
    colr += struct.pack("<I", 4)  # number of components (RGBA)
    # Values are already at the precision Apple's actool produces;
    # _parse_color_component applies float32 casts where appropriate.
    colr += struct.pack("<4d", red, green, blue, alpha)

    tlv = make_color_blend_opacity_tlv()
    tlv += make_exif_orientation_tlv()

    return build_csi(
        width=0, height=0, scale_factor=0,
        pixel_format=b"\x00\x00\x00\x00",
        layout=LAYOUT_COLOR, name=name,
        tlv_data=tlv, rendition_data=colr,
        colorspace_id=0,  # CSI header always 0 for colors; COLR has the real cs
        bitmaplist_unknown=1,
    )


def build_sprite_atlas_metadata_csi(name: str,
                                     sprite_names: list[str] = None) -> bytes:
    """Build a CSI for sprite atlas metadata (layout 1005)."""
    tlv = make_blend_opacity_tlv()
    tlv += make_exif_orientation_tlv()

    # TLV 0x03F5: sprite atlas contents list
    if sprite_names:
        contents = struct.pack("<II", len(sprite_names), 0)
        for sn in sprite_names:
            sn_bytes = sn.encode("ascii")
            contents += struct.pack("<I", len(sn_bytes))
            contents += sn_bytes
        tlv += struct.pack("<II", 0x03F5, len(contents)) + contents

    return build_csi(
        width=0, height=0, scale_factor=100,
        pixel_format=b"\x00\x00\x00\x00",
        layout=LAYOUT_METADATA, name="CoreStructuredImage",
        tlv_data=tlv, rendition_data=b"",
        colorspace_id=0,
        bitmaplist_unknown=1,
    )


def build_data_csi(name: str, raw_data: bytes) -> bytes:
    """Build a CSI for a raw data rendition (layout 1000)."""
    # RAWD header
    rawd = struct.pack("<4sII", b"DWAR", 0, len(raw_data))
    rawd += raw_data

    tlv = make_blend_opacity_tlv()
    tlv += make_exif_orientation_tlv()

    return build_csi(
        width=0, height=0, scale_factor=0,
        pixel_format=PIXELFMT_DATA,
        layout=LAYOUT_RAW_DATA, name="CoreStructuredImage",
        tlv_data=tlv, rendition_data=rawd,
        bitmaplist_unknown=1,
    )


def build_pdf_csi(filename: str, pdf_data: bytes) -> bytes:
    """Build a CSI for a PDF image rendition (layout 9).

    The system actool stores PDF images with their original filename,
    RAWD-wrapped raw bytes, pixel format ' FDP', and layout 9.
    """
    rawd = struct.pack("<4sII", b"DWAR", 0, len(pdf_data))
    rawd += pdf_data

    tlv = make_blend_opacity_tlv()
    tlv += make_exif_orientation_tlv()

    return build_csi(
        width=0, height=0, scale_factor=0,
        pixel_format=PIXELFMT_PDF,
        layout=LAYOUT_PDF, name=filename,
        tlv_data=tlv, rendition_data=rawd,
        rendition_flags=0x04,  # bitmapEncoding=1
        colorspace_id=0,
        bitmaplist_unknown=1,
    )


@dataclass
class Rendition:
    """A single rendition (image/asset) in the CAR file."""
    name: str
    identifier: int
    element: int = ELEMENT_UNIVERSAL
    part: int = PART_REGULAR
    scale: int = 1
    width: int = 0
    height: int = 0
    pixel_data: bytes = b""
    pixel_format: bytes = b"BGRA"
    layout: int = LAYOUT_ONE_PART_SCALE
    dim1: int = 0
    dim2: int = 0
    appearance: int = 0
    direction: int = 0
    is_template: bool = False  # Deprecated, use template_rendering_intent
    template_rendering_intent: int = -1  # bitmapEncoding: -1=auto, 0=original, 4=automatic, 2=template
    colorspace_id: int = 1
    locale: str = ""  # Empty = non-localized, "en"/"fr"/etc = localized
    sprite_atlas_id: int = 0  # Non-zero = belongs to a sprite atlas

    has_icon: bool = True
    keyformat: list[int] = None  # Dynamic keyformat tokens
    min_deploy: str = "10.11"  # Minimum deployment target
    platform: str = "macosx"

    def build_rendition_key(self) -> bytes:
        locale_id = _hash_name(self.locale) if self.locale else 0
        return make_rendition_key(
            appearance=self.appearance,
            unknown13=locale_id,
            element=self.element,
            part=self.part,
            direction=self.direction,
            identifier=self.identifier,
            dim1=self.dim1,
            dim2=self.dim2,
            scale=self.scale,
            keyformat=self.keyformat,
            has_icon=self.has_icon,
        )

    def build_csi(self) -> bytes:
        """Build the complete CSI data for this rendition."""
        scale_factor = self.scale * 100

        # Build TLV section
        tlv = b""
        if self.layout == LAYOUT_ONE_PART_SCALE:
            tlv += make_slices_tlv(self.width, self.height)
            tlv += make_metrics_tlv(self.width, self.height)
            tlv += make_blend_opacity_tlv()
            tlv += make_exif_orientation_tlv()
            if self.pixel_data:
                tlv += make_bytes_per_row_tlv(self.width, self.pixel_format,
                                              aligned=False)

        # Inline images use exact width*bpp stride (no padding).
        # Pixel data is passed directly to the compressor.
        rend_data = b""
        if self.pixel_data:
            pixel_data = self.pixel_data
            # GA8 inline images use DMP2 ver=0; BGRA inline uses LZFSE
            use_dmp2 = self.pixel_format == b" 8AG"
            rend_data = compress_data(pixel_data, self.pixel_format,
                                      self.width, self.height,
                                      min_deploy=self.min_deploy,
                                      platform=self.platform,
                                      allow_dmp2=use_dmp2,
                                      dmp2_inline=True)

        # Template rendering intent → bitmapEncoding field (bits 2-5):
        #   0 (0x00) = original,  4 (0x10) = automatic,  2 (0x08) = template
        # Resolve intent: explicit template_rendering_intent takes priority,
        # then legacy is_template bool, then default = automatic (4).
        intent = self.template_rendering_intent
        if intent < 0:  # auto-detect from is_template / default
            intent = 2 if self.is_template else 4
        flags = intent << 2
        # Note: the isOpaque flag (bit 1) is never set by the system
        # actool, even for fully-opaque images.  We match that behaviour.

        return build_csi(
            width=self.width,
            height=self.height,
            scale_factor=scale_factor,
            pixel_format=self.pixel_format,
            layout=self.layout,
            name=self.name,
            tlv_data=tlv,
            rendition_data=rend_data,
            rendition_flags=flags,
            colorspace_id=self.colorspace_id,
            bitmaplist_unknown=1 if self.pixel_data else 0,
        )


@dataclass
class MultisizeImageEntry:
    """Entry in a multisize image rendition."""
    width: int
    height: int
    index: int  # dim2 index


def build_multisize_rendition(name: str, identifier: int,
                              entries: list[MultisizeImageEntry]) -> Rendition:
    """Build a multisize image rendition (layout 1010) for app icons."""
    # The rendition data contains a MSIS (MultisizeImage) structure
    # MSIS: tag(4) + version(4) + count(4) + entries[count]
    # Each entry: unknown(4) + width(2) + height(2) + index(2) + padding(2)
    msis_entries = b""
    for e in entries:
        msis_entries += struct.pack("<III", e.width, e.height, e.index)
    msis_data = struct.pack("<4sII", b"SISM", 1, len(entries)) + msis_entries

    # TLV for multisize: blend with opacity=0 (no pixel data) + exif
    tlv = make_color_blend_opacity_tlv()
    tlv += make_exif_orientation_tlv()

    rend = Rendition(
        name=name,
        identifier=identifier,
        element=ELEMENT_UNIVERSAL,
        part=PART_ICON_MULTISIZE,
        scale=1,
        width=0,
        height=0,
        layout=LAYOUT_MULTISIZE_IMAGE,
        pixel_format=b"\x00\x00\x00\x00",
        colorspace_id=0,
        template_rendering_intent=0,  # Icons are always original
    )
    # Override the CSI build
    rend._csi_override = build_csi(
        width=0, height=0, scale_factor=0,
        pixel_format=b"\x00\x00\x00\x00",
        layout=LAYOUT_MULTISIZE_IMAGE,
        name=name,
        tlv_data=tlv,
        rendition_data=msis_data,
        colorspace_id=0,
        bitmaplist_unknown=1,
    )
    return rend
