//! IconComposer-style compositing via CoreGraphics.
//!
//! macOS-26 `.icon` sized renditions aren't the bare layer — they are the
//! layer composited over a gradient background and clipped to the rounded-rect
//! "squircle" icon shape. We reproduce that here with the same CoreGraphics
//! rasterizer Apple's actool uses (so the gradient/mask antialiasing matches),
//! drawing the already-rasterized layer (see [`crate::svg_raster`]) on top.
//!
//! Geometry was measured from `/usr/bin/actool` output: the icon shape is
//! inset 100/1024 of the canvas on each side and the corners have radius
//! 220/1024. The glass/specular/shadow treatments Apple applies on top are not
//! reproduced (no public algorithm); the gradient-squircle background is.

use std::ffi::CString;
use std::os::raw::{c_char, c_double, c_void};
use std::sync::OnceLock;

#[repr(C)]
#[derive(Clone, Copy)]
struct CGPoint {
    x: c_double,
    y: c_double,
}
#[repr(C)]
#[derive(Clone, Copy)]
struct CGSize {
    width: c_double,
    height: c_double,
}
#[repr(C)]
#[derive(Clone, Copy)]
struct CGRect {
    origin: CGPoint,
    size: CGSize,
}

#[cfg(target_os = "macos")]
unsafe extern "C" {
    fn dlopen(filename: *const c_char, flags: i32) -> *mut c_void;
    fn dlsym(handle: *mut c_void, symbol: *const c_char) -> *mut c_void;
}

type FnDeviceRgb = unsafe extern "C" fn() -> *mut c_void;
type FnBitmapCreate =
    unsafe extern "C" fn(*mut c_void, usize, usize, usize, usize, *mut c_void, u32) -> *mut c_void;
type FnBitmapData = unsafe extern "C" fn(*mut c_void) -> *mut u8;
type FnPathRounded =
    unsafe extern "C" fn(CGRect, c_double, c_double, *const c_void) -> *mut c_void;
type FnCtxPath = unsafe extern "C" fn(*mut c_void, *mut c_void);
type FnCtx = unsafe extern "C" fn(*mut c_void);
type FnGradCreate =
    unsafe extern "C" fn(*mut c_void, *const c_double, *const c_double, usize) -> *mut c_void;
type FnDrawLinear = unsafe extern "C" fn(*mut c_void, *mut c_void, CGPoint, CGPoint, u32);
type FnProviderCreate =
    unsafe extern "C" fn(*mut c_void, *const c_void, usize, *const c_void) -> *mut c_void;
type FnImageCreate = unsafe extern "C" fn(
    usize,
    usize,
    usize,
    usize,
    usize,
    *mut c_void,
    u32,
    *mut c_void,
    *const c_double,
    bool,
    u32,
) -> *mut c_void;
type FnDrawImage = unsafe extern "C" fn(*mut c_void, CGRect, *mut c_void);
type FnRelease = unsafe extern "C" fn(*mut c_void);

struct Syms {
    device_rgb: FnDeviceRgb,
    bitmap_create: FnBitmapCreate,
    bitmap_data: FnBitmapData,
    path_rounded: FnPathRounded,
    ctx_add_path: FnCtxPath,
    ctx_clip: FnCtx,
    ctx_save: FnCtx,
    ctx_restore: FnCtx,
    grad_create: FnGradCreate,
    draw_linear: FnDrawLinear,
    provider_create: FnProviderCreate,
    image_create: FnImageCreate,
    draw_image: FnDrawImage,
    path_release: FnRelease,
    grad_release: FnRelease,
    provider_release: FnRelease,
    image_release: FnRelease,
    ctx_release: FnRelease,
    cs_release: FnRelease,
}
unsafe impl Sync for Syms {}
unsafe impl Send for Syms {}

#[cfg(target_os = "macos")]
fn syms() -> Option<&'static Syms> {
    static CELL: OnceLock<Option<Syms>> = OnceLock::new();
    CELL.get_or_init(|| unsafe {
        let cg = dlopen(
            CString::new("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
                .unwrap()
                .as_ptr(),
            0x1,
        );
        if cg.is_null() {
            return None;
        }
        macro_rules! sym {
            ($name:expr, $ty:ty) => {{
                let n = CString::new($name).unwrap();
                let p = dlsym(cg, n.as_ptr());
                if p.is_null() {
                    return None;
                }
                std::mem::transmute::<_, $ty>(p)
            }};
        }
        Some(Syms {
            device_rgb: sym!("CGColorSpaceCreateDeviceRGB", FnDeviceRgb),
            bitmap_create: sym!("CGBitmapContextCreate", FnBitmapCreate),
            bitmap_data: sym!("CGBitmapContextGetData", FnBitmapData),
            path_rounded: sym!("CGPathCreateWithRoundedRect", FnPathRounded),
            ctx_add_path: sym!("CGContextAddPath", FnCtxPath),
            ctx_clip: sym!("CGContextClip", FnCtx),
            ctx_save: sym!("CGContextSaveGState", FnCtx),
            ctx_restore: sym!("CGContextRestoreGState", FnCtx),
            grad_create: sym!("CGGradientCreateWithColorComponents", FnGradCreate),
            draw_linear: sym!("CGContextDrawLinearGradient", FnDrawLinear),
            provider_create: sym!("CGDataProviderCreateWithData", FnProviderCreate),
            image_create: sym!("CGImageCreate", FnImageCreate),
            draw_image: sym!("CGContextDrawImage", FnDrawImage),
            path_release: sym!("CGPathRelease", FnRelease),
            grad_release: sym!("CGGradientRelease", FnRelease),
            provider_release: sym!("CGDataProviderRelease", FnRelease),
            image_release: sym!("CGImageRelease", FnRelease),
            ctx_release: sym!("CGContextRelease", FnRelease),
            cs_release: sym!("CGColorSpaceRelease", FnRelease),
        })
    })
    .as_ref()
}

#[cfg(not(target_os = "macos"))]
fn syms() -> Option<&'static Syms> {
    None
}

/// A two-stop linear gradient background. RGB components are device-RGB 0..1;
/// `start`/`stop` are normalized icon coordinates `[x, y]` with y measured
/// top-down (matching icon.json `orientation`).
pub struct GradientFill {
    pub start_rgb: [f64; 3],
    pub stop_rgb: [f64; 3],
    pub start: [f32; 2],
    pub stop: [f32; 2],
}

/// Icon-shape geometry as a fraction of the canvas edge, measured from Apple's
/// 1024px output: 100px inset, 220px corner radius.
const MARGIN_RATIO: f64 = 100.0 / 1024.0;
const CORNER_RATIO: f64 = 220.0 / 1024.0;

/// Composite a sized rendition: the gradient background and the supplied layer
/// (canvas-sized premultiplied-first BGRA), both clipped to the icon squircle.
/// Returns premultiplied-first BGRA of `pixel_size²`, or `None` if CoreGraphics
/// is unavailable.
pub fn composite_icon(
    pixel_size: u32,
    gradient: &GradientFill,
    layer_bgra: &[u8],
) -> Option<Vec<u8>> {
    let s = syms()?;
    let size = pixel_size as usize;
    if layer_bgra.len() != size * size * 4 {
        return None;
    }
    let margin = pixel_size as f64 * MARGIN_RATIO;
    let corner = pixel_size as f64 * CORNER_RATIO;
    let content = pixel_size as f64 - 2.0 * margin;

    unsafe {
        let cs = (s.device_rgb)();
        const PREMUL_FIRST: u32 = 2;
        const LE32: u32 = 2 << 12;
        let info = PREMUL_FIRST | LE32;
        let ctx = (s.bitmap_create)(
            std::ptr::null_mut(),
            size,
            size,
            8,
            size * 4,
            cs,
            info,
        );
        if ctx.is_null() {
            (s.cs_release)(cs);
            return None;
        }

        // Clip everything to the rounded-rect icon shape.
        let rect = CGRect {
            origin: CGPoint { x: margin, y: margin },
            size: CGSize { width: content, height: content },
        };
        let path = (s.path_rounded)(rect, corner, corner, std::ptr::null());
        (s.ctx_add_path)(ctx, path);
        (s.ctx_clip)(ctx);

        // Background gradient. CoreGraphics y is bottom-up; icon.json y is
        // top-down, so flip the y components when mapping to context points.
        let comps: [c_double; 8] = [
            gradient.start_rgb[0],
            gradient.start_rgb[1],
            gradient.start_rgb[2],
            1.0,
            gradient.stop_rgb[0],
            gradient.stop_rgb[1],
            gradient.stop_rgb[2],
            1.0,
        ];
        let locs: [c_double; 2] = [0.0, 1.0];
        let grad = (s.grad_create)(cs, comps.as_ptr(), locs.as_ptr(), 2);
        // icon.json orientation y and this context share a top-down y axis
        // (y=1 is the top edge), matching the layer draw — map straight through.
        let to_ctx = |p: [f32; 2]| CGPoint {
            x: margin + p[0] as f64 * content,
            y: margin + p[1] as f64 * content,
        };
        // DrawsBeforeStartLocation | DrawsAfterEndLocation = 3 (extend ends).
        (s.draw_linear)(ctx, grad, to_ctx(gradient.start), to_ctx(gradient.stop), 3);
        (s.grad_release)(grad);

        // Layer on top, drawn at full canvas size. Our rasterized BGRA buffer
        // and the gradient already share the context's orientation, so the
        // CGImage lands aligned without an extra flip.
        let provider = (s.provider_create)(
            std::ptr::null_mut(),
            layer_bgra.as_ptr() as *const c_void,
            layer_bgra.len(),
            std::ptr::null(),
        );
        let img = (s.image_create)(
            size,
            size,
            8,
            32,
            size * 4,
            cs,
            info,
            provider,
            std::ptr::null(),
            false,
            0,
        );
        if !img.is_null() {
            let full = CGRect {
                origin: CGPoint { x: 0.0, y: 0.0 },
                size: CGSize { width: size as f64, height: size as f64 },
            };
            (s.ctx_save)(ctx);
            (s.draw_image)(ctx, full, img);
            (s.ctx_restore)(ctx);
            (s.image_release)(img);
        }
        (s.provider_release)(provider);

        let data = std::slice::from_raw_parts((s.bitmap_data)(ctx), size * size * 4).to_vec();
        (s.path_release)(path);
        (s.ctx_release)(ctx);
        (s.cs_release)(cs);
        Some(data)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn composite_masks_to_squircle() {
        let size = 256u32;
        // Fully transparent layer → output is just the gradient, squircle-clipped.
        let layer = vec![0u8; (size * size * 4) as usize];
        let fill = GradientFill {
            start_rgb: [0.5, 0.5, 0.5],
            stop_rgb: [0.9, 0.9, 0.9],
            start: [0.5, 0.0],
            stop: [0.5, 1.0],
        };
        let Some(out) = composite_icon(size, &fill, &layer) else {
            // CoreGraphics unavailable (non-macOS CI) — nothing to assert.
            return;
        };
        assert_eq!(out.len(), (size * size * 4) as usize);
        let alpha = |x: u32, y: u32| out[((y * size + x) * 4 + 3) as usize];
        // Corner (well outside the inset squircle) is clipped away.
        assert_eq!(alpha(2, 2), 0, "corner must be transparent");
        // Center is inside the icon shape and opaque.
        assert_eq!(alpha(size / 2, size / 2), 255, "center must be opaque");
    }
}
