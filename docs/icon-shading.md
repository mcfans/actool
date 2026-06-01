# `.icon` shading effects — data model and rendering plan

How macOS-26 IconComposer shades the icon stack (drop shadow, specular, glass,
translucency, blur), what `icon.json` data drives each effect, and how the
CoreGraphics render pass (`icon_render.rs`) should consume it. The *parameters*
are parsed and resolved by `icon_effects.rs`; this doc is the bridge to the
render pass that uses them.

## Where the parameters come from

Every effect is either a plain field or an appearance-keyed
`*-specializations` list (the list supersedes the plain field; a no-appearance
entry is the default/light value and the fallback for any appearance without
its own entry). `icon_effects::resolve_icon_effects(group, appearance)` returns
an `IconEffects` for one of `Light` / `Dark` / `Tinted`:

| Field | icon.json source | Meaning |
|-------|------------------|---------|
| `shadow` (`kind`, `opacity`) | group `shadow` / `shadow-specializations` | drop shadow under the icon |
| `specular` (bool) | group `specular(-specializations)` | bright sheen highlight |
| `translucency` (`enabled`, `value`) | group `translucency(-specializations)` | glass see-through amount |
| `blur_material` (0..1) | group `blur-material(-specializations)` | frosted backdrop behind the layer |
| `lighting` | group `lighting-specializations` | `individual` vs `combined` light model |
| `blend_mode` | group/layer `blend-mode-specializations` | compositing mode (`normal`, `soft-light`, …) |
| per-layer `glass` | layer `glass(-specializations)` | render the layer as glass |
| per-layer `opacity` | layer `opacity-specializations` | layer alpha |

The renderer maps render variants to appearances: variant 0 → `Light`, variant
1 → `Dark` (a future tinted variant → `Tinted`).

## Drop shadow — measured, ready to implement

Measured from Apple's 1024px feishin output (`shadow_geometry` constants):

- **Colour**: `neutral` → black; `layer-color` → tinted by the layer's dominant
  colour; `none` → skip.
- **Blur**: Gaussian, radius ≈ `20/1024` of the canvas edge.
- **Offset**: nudged down ≈ `8/1024` (bottom halo heavier than top).
- **Strength**: peak alpha ≈ `0.17` just outside the squircle edge at
  `opacity = 1`, scaled by `shadow.opacity`; fades to 0 ≈35px out.

Render: before filling the squircle, set
`CGContextSetShadowWithColor(offset, blur, color·alpha)` (or draw a separately
blurred black squircle behind). This is concrete and should land first.

## The glass effects — parameters ready, render is approximate

These are Apple's proprietary "liquid glass" treatment; the exact shader is not
public. The parsed parameters let us approximate:

- **glass** (per layer): when true, the layer is a translucent glass slab.
  Approximate with an edge bevel — a top-light → bottom-dark gradient masked to
  the layer's alpha — plus the translucency below.
- **translucency** (`enabled`, `value`): when enabled, multiply the layer's
  alpha toward `value` so the gradient shows through the glass.
- **blur_material**: Gaussian-blur the backdrop (gradient + lower layers)
  behind a glass layer by a radius scaled by the strength, for the frosted look.
- **specular**: when true, add a soft white highlight (a small top-positioned
  radial/linear white gradient) over the glass. feishin only enables it for the
  `tinted` appearance, so it can't be measured from the light render yet —
  needs a `specular:true` light fixture to pin down position/intensity.
- **lighting** `individual` vs `combined`: whether bevel/specular are computed
  per layer or once for the whole stack. Affects multi-layer icons
  (scrumdinger); single-layer icons are unaffected.
- **blend_mode**: map to CG blend modes (`normal` → Normal, `soft-light` →
  `kCGBlendModeSoftLight`) when compositing each layer.

## Status

`icon_effects.rs` resolves all of the above into typed per-appearance values
(unit-tested against feishin's specialization forms and element-web's plain
fields).

**Drop shadow — implemented.** `icon_render::composite_icon` takes a
`ShadowParams` and casts it from the squircle before clipping (`icon_bundle`
derives it per variant via `shadow_params`). The rendered halo matches Apple's
feishin output within Gaussian tolerance (sides 34/22/14/7 vs Apple
25/18/12/7 at 5px steps; ≈35px reach). `kind: none` skips it; neutral and
layer-color are both approximated as black.

**Glass — implemented (approximate).** `icon_bundle::render_layer_stack`
composites *all* a group's layers (previously only the first was used) into one
premultiplied-first BGRA, which the compositor draws over the gradient. Glass
layers are merged into a coverage mask and rendered as Apple's frosted relief:
the layer colour is stripped and replaced by a near-black overlay. A layer is
glass if it opts in, or if the group is a *glass context* (translucency/blur
enabled, or a sibling is glass) and it hasn't opted out with `glass: false`.

The glass darkening is **only ≈3%** — recovered by decoding Apple's scrumdinger
GA8 and dividing the layer-region luma by the local background: out/bg ≈0.975
(top) → 0.965 (bottom). The pronounced top-light → bottom-dark relief the eye
sees is **almost entirely the background gradient** (252→236) showing through
the nearly-clear glass, not the glass itself. The earlier 7–11% overlay was far
too strong and flattened that gradient. With the subtle overlay the layer region
grades 245→230 vs Apple's 246→229 (mean ≈5 luma over the shape). The residual is
Apple's faint per-region (luminance-dependent) detail.

This only works because the background gradient renders the right way up:
`resolve_gradient_fill` anchors the *first* stop to the top edge regardless of
how the stored geometry orders its endpoints (feishin's `[0.5,1]→[0.5,0.3]` is
unchanged; scrumdinger/automatic `[0.5,0]→[0.5,1]` was rendering upside down).
element-web (non-glass) keeps full colour; only its frame flips white-to-top.

**Layer order / native size.** Layers paint back-to-front (icon.json lists them
front-to-back, so `collect_stack_layers` reverses), and each is rendered at its
**native viewBox size and aspect** scaled by base·group·layer, not stretched to
a square — so transmission's non-1024 parts (HandleShaft 256×410, Handle
782×284, Plate 868×869, …) keep their proportions and stack in the right order.
Together these turn transmission from a scrambled mess into a recognisable
red-capsule-on-striped-plate (mean diff ≈26, capsule area within 4% of Apple).

**Layer position — implemented.** `render_layer_stack` places each layer with
its resolved affine transform instead of drawing it 1:1. A `scale = 1` layer is
drawn into the icon content area (824/1024 of the canvas — `LAYER_BASE_SCALE`);
`position.scale` multiplies it and `translation-in-points` (in that same scaled
space) shifts it, with the group's `position` composed over the layer's
(`scale = base·gscale·lscale`, `tx = base·(gscale·ltx + gtx)`). Reverse-
engineered against tagspaces (a non-glass positioned layer): with the base scale
our layer lands at 1.004× Apple's size, centre within ~1px (mean ≈6 luma over
the icon). element-web (no position) now insets its layer to y[182,922] like
Apple instead of filling the canvas.

**Blend modes + opacity — implemented.** Each non-glass layer composites with
its resolved blend mode (`composite_blend`, the W3C separable blends: normal,
multiply, screen, overlay, soft-light, hard-light, darken, lighten) and its
opacity scales the source alpha. Because blend modes differ between appearances
(scrumdinger/transmission use `soft-light`/`overlay` only in dark), the stack is
rendered **per appearance** — the primary variant uses the light stack, the
alternate the dark one. Glass layers ignore blend/opacity (they become relief).

**Translucency gates the glass mode; specular — implemented.** A glass layer is
*frosted* (the faint see-through relief) only when translucency is **enabled**
(scrumdinger). With translucency **disabled** it is **opaque** glass: the layer
keeps its colour (blacks lifted toward a grey floor ≈45/255 — the glass
material) and, when the group's `specular` is on, gets a directional sheen —
`apply_specular` brightens top-facing edges of the layer and shadows
bottom-facing ones (light from the top), the raised "liquid glass" rim.
Reverse-engineered against KeepingYouAwake (a non-variant `specular: true` glass
icon): our coffee cup matches Apple's — body lum 39 vs 45, rim-highlight peak
209 vs 209, cup centre (549,533) vs (550,524) once the SVG scaling was fixed
(below). feishin's specular is `tinted`-only, so it stays unrendered there.

> SVG layers are scaled to fit their target size. `svg_raster::rasterize_svg`
> used to draw the SVG at its intrinsic size and only apply an integer scale, so
> a 1024-pt layer asked for at the 824-px content size was *clipped* to its
> top-left corner — the KYA cup landed oversized and offset. It now scales the
> SVG by `target / native` (a no-op, hence byte-identical, when they already
> match, as in the xcassets path). Mean diff over the KYA icon dropped 48 → 15.

**Blur-material / lighting — not rendered; per-region glass detail — measured
negligible.** Parameters are resolved. Decoding Apple's scrumdinger GA8 shows
the glass relief depends on input luminance by only ≈2/luma within a fixed
y-band, so "per-region glass detail" is not a real gap — the residual is
edge anti-aliasing. blur-material and lighting are group properties with no
fixture that exercises them measurably (smooth backdrops / blank-layer feishin),
and need a per-group compositing refactor first. Full analysis + implementation
plan in `docs/icon-shading-plan.md`.

**Per-layer fill gradients (multi-group palette).** A multi-group icon's layers
can each declare their own `fill-specializations` linear-gradients (transmission's
ArrowLines / OuterEdge, Rectangle's Dots). These are now folded into the
Color/Gradient palette via `append_layer_fills`, in document order, per
appearance. Crucially the dedup scope differs by fill kind: a layer **solid**
deduplicates against the whole palette (recipe-scraper's solid collapses onto
its base gradient stop), but a **gradient-stop / keyword** colour deduplicates
only against fold colours, not the hardcoded base — so Apple keeps transmission's
`Color-12` (0.078) distinct from the automatic-gradient's `Color-4` (also 0.078).
transmission's catalog now matches Apple's exactly (12 Colors / 6 Gradients,
`validate_car` 19 OK / 32 — identical), as does Rectangle (7 / 3, 11 OK / 15).
