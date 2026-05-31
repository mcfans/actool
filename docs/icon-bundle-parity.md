# `.icon` (IconComposer) CAR parity status

How close our `.icon` output is to `/usr/bin/actool`, what is byte-matched,
and what is fundamentally out of reach. Reference fixtures:
`third_party/element-web/.../icon.icon` (simple `fill: automatic`),
`third_party/scrumdinger_app/.../ScumAppIcon.icon` (keyword
fill-specializations), `third_party/feishin/media/feishin.icon` (custom-
gradient fill-specializations + SVG layer — the richest fixture).

## Byte-for-byte parity is impossible for `.icon`

Apple's actool embeds a **fresh random UUID** in every pre-rendered
rendition name (`feishin128x128_…_<UUID>-<pid>-<hex>.png`). Two consecutive
Apple runs of the *same* bundle differ in raw bytes (verified: file sizes and
per-rendition `SizeOnDisk` are identical between runs, only the name bytes
move). So the achievable target is **structural / functional parity**, not a
byte-identical `.car`.

## What we match (verified against the reference)

* **Rendition-type counts** match exactly. feishin: 8 Color, 14 Icon Image,
  3 IconGroup, 3 IconImageStack, 1 MultiSized, 3 Named Gradient, 2
  PackedImage, 1 Vector — identical multiset to Apple.
* **Color / Gradient palette** is byte-identical, including colorspaces
  (extended-gray=6, gray-gamma-2.2=2, display-p3=3), `f64(f32(round3(v)))`
  component encoding, gradient stop references (with dedup), and gradient
  orientation geometry. See `fill_specializations_assets` in
  `icon_bundle.rs`.
* **Rendition names** use the bundle stem (`feishin16x16_…`), not a literal
  `icon` prefix.
* **SVG layer source** is stored as a `Vector` rendition holding the raw SVG
  (`image.svg`, LAYOUT_PDF), matching Apple — not rasterized to `image.png`.
* **Main facet part** is `PART_ICON` (220), as Apple emits, not
  `PART_ICON_COMPOSER` (245).
* **`imagesWithName:` / `colorWithName:` behaviour** matches Apple's own
  output: feishin reports 12 OK / 2 FAIL via `validate_car` — the same 2
  facets (the IconGroup and the SVG Vector) that fail in Apple's `.car` too.

### fill-specializations palette model

Folded in document order: a white anchor `Color-1` (extended-gray `[1,1]`),
then each top-level specialization, then every layer's `fill` and
`fill-specializations`. Each `value`:

* keyword — `system-light` → gray pair `(1.0, 0.925)`; `system-dark` →
  `(0.192, 0.078)`; bare `automatic` resolves by the entry's appearance
  (dark → dark pair, else light). Emits the two gray stops (gray-gamma-2.2)
  and a top→bottom gradient.
* `{linear-gradient: [s0, s1], orientation}` — each stop becomes a Color in
  its declared space; the gradient carries `[start.x, start.y, stop.x,
  stop.y]` from `orientation` (default `[0.5, 0, 0.5, 1]`).
* `{solid: "<spec>"}` — one Color.

Colors dedup by `(colorspace, components)`; gradients by `(geometry, stops)`.
This makes scrumdinger's redundant layer `automatic` collapse to 5 Colors / 2
Gradients, while feishin's distinct layer gradient adds Color-7 + Gradient-3
(its second stop dedups onto Color-2) for 8 Colors / 3 Gradients.

## What we do NOT match — and why (renderer-bound)

These are all the *rendered* outputs of Apple's proprietary IconComposer
renderer. None affect catalog loading; CUICatalog uses the data we emit for
every functional lookup.

* **Pre-rendered sized renditions** (Icon Image 16…1024) and the
  **ZZZZPackedAsset atlases**. Apple composites the full icon stack
  (gradients + layer + shadow + blur + specular + translucency) at each size;
  we rasterize the source. Pixels — and therefore compressed `SizeOnDisk` —
  differ. Same class as the dropped iOS app-icon atlas geometry
  (`atlas-packing.md`).
* **IconGroup CSI geometry** (TLV `0x03F4`). Apple stores the group's
  *computed* bounding box (e.g. feishin `[off 106,62, size 890,890]`) derived
  from the group `position.scale` (2.2) and the layer's scale/translation; we
  emit a placeholder. TLV `0x03FC` additionally embeds the child layer's
  facet-name string (`feishin_Assets/feishin`) where we store a numeric id.
  Both require the IconComposer layout engine. The IconGroup facet is
  non-functional in Apple's own `.car` (fails `imagesWithName:`), so this is
  cosmetic.

## Reproduce

```
./target/debug/actool --compile out --platform macosx \
  --minimum-deployment-target 11.0 --app-icon feishin \
  --output-partial-info-plist out/p.plist third_party/feishin/media/feishin.icon
/usr/bin/actool --compile ref  --platform macosx \
  --minimum-deployment-target 11.0 --app-icon feishin \
  --output-partial-info-plist ref/p.plist third_party/feishin/media/feishin.icon
python3 tools/compare_car.py out/Assets.car ref/Assets.car   # structural diff
./tools/validate_car out/Assets.car                          # 12 OK / 2 FAIL
```
