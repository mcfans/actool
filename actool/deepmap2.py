"""
Dynamic loading of vImage's Deepmap2 compression API.

Uses ctypes to load vImageDeepmap2EncodeCreateBuffer from the Accelerate
framework at runtime. This is an undocumented Apple API available when
linked against Accelerate.framework.

The API is available on macOS and provides better compression than plain
LZFSE for image data by using color transforms, prediction, and palette
optimisation.
"""

import ctypes
import ctypes.util
import struct

# Pixel format mapping: our format bytes → deepmap2 format ID
_PIXFMT_MAP = {
    b"BGRA": 4,  # 4 channels, 8-bit (RGBA for deepmap2)
    b" 8AG": 2,  # 2 channels, 8-bit (gray + alpha)
}

# Deepmap2 options struct
class _Deepmap2Options(ctypes.Structure):
    _fields_ = [
        ("compressionType", ctypes.c_uint32),
        ("quality", ctypes.c_uint32),
        ("param", ctypes.c_uint32),
    ]


# vImage_Buffer struct
class _vImageBuffer(ctypes.Structure):
    _fields_ = [
        ("data", ctypes.c_void_p),
        ("height", ctypes.c_ulong),
        ("width", ctypes.c_ulong),
        ("rowBytes", ctypes.c_ulong),
    ]


def _pixel_size(pixel_format: bytes) -> int:
    """Return bytes per pixel for a given format."""
    if pixel_format == b"BGRA":
        return 4
    elif pixel_format == b" 8AG":
        return 2
    return 0


# Lazily loaded function pointer
_encode_fn = None
_load_attempted = False


def _load_vimage():
    """Try to load vImageDeepmap2EncodeCreateBuffer from vImage framework."""
    global _encode_fn, _load_attempted
    if _load_attempted:
        return _encode_fn
    _load_attempted = True

    try:
        # vImage is inside Accelerate.framework
        lib = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/Accelerate.framework/Accelerate"
        )
    except OSError:
        try:
            path = ctypes.util.find_library("Accelerate")
            if path is None:
                return None
            lib = ctypes.cdll.LoadLibrary(path)
        except OSError:
            return None

    try:
        fn = lib.vImageDeepmap2EncodeCreateBuffer
        # size_t vImageDeepmap2EncodeCreateBuffer(
        #     vImage_Buffer *src, uint32_t pixelFormat,
        #     Deepmap2Options *opts, void **outBuf)
        fn.restype = ctypes.c_size_t
        fn.argtypes = [
            ctypes.POINTER(_vImageBuffer),
            ctypes.c_uint32,
            ctypes.POINTER(_Deepmap2Options),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        _encode_fn = fn
    except AttributeError:
        pass

    return _encode_fn


def is_available() -> bool:
    """Check if the deepmap2 encoder is available on this system."""
    return _load_vimage() is not None


def encode(pixel_data: bytes, pixel_format: bytes,
           width: int, height: int) -> bytes | None:
    """Encode pixel data using vImageDeepmap2EncodeCreateBuffer.

    Returns the raw dmp2 encoded data, or None if encoding fails or is
    unavailable. The caller is responsible for wrapping this in the
    appropriate CELM envelope.
    """
    fn = _load_vimage()
    if fn is None:
        return None

    dm_fmt = _PIXFMT_MAP.get(pixel_format)
    if dm_fmt is None:
        return None

    bpp = _pixel_size(pixel_format)
    # Row stride must match the actual pixel buffer layout (32-byte aligned)
    exact = width * bpp
    row_bytes = ((exact + 31) // 32) * 32

    # Create a mutable buffer for vImage
    src_buf = (ctypes.c_uint8 * len(pixel_data)).from_buffer_copy(pixel_data)

    vimg = _vImageBuffer()
    vimg.data = ctypes.cast(src_buf, ctypes.c_void_p)
    vimg.height = height
    vimg.width = width
    vimg.rowBytes = row_bytes

    opts = _Deepmap2Options()
    opts.compressionType = 2  # will be overridden by EncodeCreateBuffer
    opts.quality = 1
    opts.param = 10  # 0x0a — matches system actool behaviour

    out_ptr = ctypes.c_void_p(None)

    encoded_size = fn(
        ctypes.byref(vimg),
        ctypes.c_uint32(dm_fmt),
        ctypes.byref(opts),
        ctypes.byref(out_ptr),
    )

    if encoded_size == 0 or not out_ptr.value:
        return None

    # Copy the result and free the malloc'd buffer
    result = ctypes.string_at(out_ptr.value, encoded_size)
    ctypes.cdll.LoadLibrary("libSystem.B.dylib").free(out_ptr)

    return result


def make_celm_dmp2(dmp2_data: bytes, pixel_format: bytes) -> bytes:
    """Wrap raw dmp2 data in a CELM ver=2 comp=11 envelope.

    The system actool uses a 16-byte sub-header between the CELM header
    and the dmp2 payload:
        [version=1(4)][deepmap2_pixfmt(4)][dmp2_len(4)][zero(4)]
    """
    dm_fmt = _PIXFMT_MAP.get(pixel_format, 0)
    dmp2_len = len(dmp2_data)

    # Sub-header: version=1, pixfmt, dmp2_len, zero
    sub_header = struct.pack("<IIII", 1, dm_fmt, dmp2_len, 0)
    total_payload = sub_header + dmp2_data
    total_len = len(total_payload)

    # CELM header: "MLEC" + ver=2 + comp=11 + total_payload_len
    celm = struct.pack("<4sIII", b"MLEC", 2, 11, total_len)
    return celm + total_payload
