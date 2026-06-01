# Remaining `.icon` shaders — requirements & implementation plan

Status of the IconComposer render pipeline after the position / blend / opacity
/ glass / specular / multi-group work, and what it would take to close the rest.
Grounded in measurements against Apple's output (`tools/extract_pixels`, GA8
decode). The render pass lives in `icon_bundle::render_layer_stack` +
`icon_render`; effect parameters are already resolved by `icon_effects`.

## What's already done

squircle mask · background gradient (+ direction) · drop shadow · multi-layer
compositing in paint order · per-layer affine position (native viewBox size /
aspect) · blend modes · opacity · glass (frosted relief **and** opaque-glossy,
gated by translucency) · specular rim · multi-group per-layer fill palette.

## The hard ceiling (won't change)

Byte-for-byte parity is impossible regardless of shader work: Apple embeds a
fresh **random UUID** in every rendition name (two Apple runs differ), and the
sized pixels are Apple's proprietary CoreSVG+compositor output. The target is
structural + visual closeness. A standing ~6/luma gradient residual also comes
from interpolating in device-RGB rather than Apple's working space.

## Remaining shaders

### 1. Per-region glass detail — **not a real gap (close it as done)**

Hypothesis was that the frosted relief should carry the layer's internal
luminance/edges. Measured on Apple's scrumdinger GA8: within a fixed y-band the
output varies only **≈2 luma** across input luminance 80→240. The relief is
essentially the vertical gradient (already reproduced); the ~5-luma residual is
edge anti-aliasing on the segment boundaries, not a missing shading term.
**Plan: none.** Update the docs to stop calling this an open shader.

### 2. blur-material — **real, low visible impact, pure-Rust feasible**

*What it is.* A group property (`blur_material`, 0..1; `None` = off) that frosts
the group's backdrop. On a **frosted** glass group it softens the relief itself
(KYALauncher: `system-dark` + translucency-on + blur 0.5 renders the cup as a
*soft* dark emboss). On an **opaque** group it would soften the backdrop showing
through — but every available fixture has a smooth gradient backdrop, so the
visible effect is tiny and there is **no fixture to verify it precisely**.

*Data.* `IconEffects.blur_material: Option<f32>` (already resolved per
appearance).

*Feasibility.* We composite into our own straight-RGBA buffer, so no new FFI is
needed — a separable box blur applied 3× approximates a Gaussian. Radius scales
with the value and `pixel_size` (needs a fixture sweep to fix the constant;
≈value·k·pixel_size).

*Implementation steps.*
1. Add `blur: f32` (resolved group blur) to the per-group data feeding
   `render_layer_stack` (currently per-layer; this needs a per-group grouping —
   see "Refactor note").
2. `fn box_blur(buf, w, radius)` + `fn gaussian3(buf, w, radius)` on RGBA.
3. For a frosted group: blur the `glass_cov` mask by `radius` before the relief
   pass (softens the relief — the KYALauncher look).
4. For an opaque group with blur: blur the accumulated backdrop region under the
   group's coverage before compositing the group.

*Cost / risk.* ~½ day. Low risk (additive, gated on `blur_material.is_some()`),
but **unverifiable** beyond eyeballing KYALauncher — no clean numeric target.

### 3. lighting (`individual` / `combined`) — **real, no measurable fixture**

*What it is.* Per group; decides whether the bevel/specular is computed per
layer (`individual`) or once over the merged group shape (`combined`). Only
matters for **multi-layer glass groups**.

*Data.* `IconEffects.lighting` (resolved). Today we always union a group's glass
coverage and emboss once — i.e. effectively `combined`.

*Implementation steps.*
1. For `individual`: emboss each glass layer's own coverage separately (and
   keep separate frosted coverage), rather than the union.
2. For `combined`: current behaviour.

*Cost / risk.* ~½ day once the per-group refactor exists. **Blocker: no fixture
exercises it measurably** — the only one with `lighting` specializations is
feishin, whose layer is blank (broken SVG filter) and whose `combined` is
tinted-only (an appearance we don't even emit). So it can be implemented but not
verified; correctness would be a guess.

## Refactor note (shared prerequisite)

blur-material and lighting are **group** properties, but `render_layer_stack`
currently flattens all groups into one reversed layer list and unions all glass
coverage globally. To honour per-group blur / lighting the stack must be
restructured to **composite group-by-group** (back-to-front): render each
group's layers (with its own glass union, blur, lighting), then composite the
finished group onto the canvas. This is the main cost item (~1 day) and also the
*correct* model for per-group `shadow`/`translucency`/`specular`, which we
currently read only from the first group.

## Recommended order

1. **Mark per-region glass "done"** (doc-only) — it isn't a gap.
2. **Per-group compositing refactor** — unlocks blur, lighting, and correct
   per-group shadow/specular/translucency. Highest leverage.
3. **blur-material** on the frosted path (verify visually vs KYALauncher).
4. **lighting** — implement both modes, but flag as unverifiable.

## Honest assessment

The remaining shaders are **diminishing returns**: per-region glass is
negligible, and blur-material + lighting have no fixture that lets us verify
them numerically (smooth backdrops / blank-layer feishin). The per-group
compositing refactor is worth doing for its own sake (correct per-group
shadow/specular), and blur-material rides on it cheaply; lighting is
implementable but a guess. None move the byte-parity needle (impossible), and
none are as impactful as the work already landed.
