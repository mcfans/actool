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

**Glass / specular / translucency / blur — not rendered yet.** Their parameters
are resolved and ready, but none of the current fixtures exercise them visibly
(feishin's layer is blank; element-web disables them; variant-axis bundles are
recomposed by CUICatalog so their sized rendition isn't shown directly), so
there is no ground truth to tune against. They need a fixture with a visible
glass/specular layer before implementing. See `docs/icon-bundle-parity.md`.
