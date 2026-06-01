//! .icon bundle support (modern macOS icon.json + source image).

use crate::bom::BomWriter;
use crate::car::{self, MultisizeImageEntry, Rendition};
use crate::catalog::load_image_as_bgra;
use crate::icon_json::{Fill, IconJson};
use crate::name_hash::hash_name;
use byteorder::LittleEndian;

static TMP_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
use anyhow::Result;
use image::imageops::FilterType;
use std::fs;
use std::path::{Path, PathBuf};

/// One facet entry in the FACETKEYS tree: (facet_name, element, part, identifier).
type FacetEntry = (String, u16, Option<u16>, u16);

/// Bundle stem used as the prefix for asset facet names: e.g.
/// `<stem>_Assets/<layer_name>`. Matches Apple's actool naming.
fn bundle_facet_prefix(icon_path: &Path) -> String {
    icon_path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("Assets")
        .to_string()
}

/// Build a deterministic UUID-shaped string derived from `inputs`. Apple's
/// iconNxN_NSAppearanceName..._UUID-PID-HEX.png names embed a per-rendition
/// UUID; we don't need it to be cryptographic — just stable per input so
/// regenerating the same catalog yields the same byte stream.
fn deterministic_uuid(inputs: &[&str]) -> String {
    use std::hash::{Hash, Hasher};
    let mut h1 = std::collections::hash_map::DefaultHasher::new();
    let mut h2 = std::collections::hash_map::DefaultHasher::new();
    "uuid-lo".hash(&mut h1);
    "uuid-hi".hash(&mut h2);
    for s in inputs {
        s.hash(&mut h1);
        s.hash(&mut h2);
    }
    let lo = h1.finish();
    let hi = h2.finish();
    let bytes: [u8; 16] = {
        let mut out = [0u8; 16];
        out[..8].copy_from_slice(&lo.to_be_bytes());
        out[8..].copy_from_slice(&hi.to_be_bytes());
        out
    };
    format!(
        "{:02X}{:02X}{:02X}{:02X}-{:02X}{:02X}-{:02X}{:02X}-{:02X}{:02X}-{:02X}{:02X}{:02X}{:02X}{:02X}{:02X}",
        bytes[0], bytes[1], bytes[2], bytes[3],
        bytes[4], bytes[5],
        bytes[6], bytes[7],
        bytes[8], bytes[9],
        bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15],
    )
}

/// Name a pre-rendered icon size the way Apple's actool does inside .car:
/// `iconNxN_<appearance>_<UUID>-<pid>-<hex>.png`. The pid+hex tail is a
/// stable hash-derived suffix; CoreUI keys renditions by attribute tuple,
/// not by name, so the precise format doesn't affect lookup.
fn pre_rendered_name(
    icon_name: &str,
    point_size: u32,
    scale: u32,
    appearance_name: &str,
) -> String {
    let scale_label = format!("{scale}x");
    let pt_label = format!("{point_size}");
    let uuid = deterministic_uuid(&[icon_name, &pt_label, &scale_label, appearance_name]);
    let tail_uuid =
        deterministic_uuid(&[icon_name, &pt_label, &scale_label, "tail"]);
    let tail_hex: String = tail_uuid.chars().filter(|c| *c != '-').collect();
    let pid = &tail_hex[0..5];
    let hex = &tail_hex[5..21];
    // Apple prefixes the pre-rendered name with the bundle stem (the
    // `--app-icon` name), not a literal "icon": feishin.icon → "feishin16x16…".
    // element-web's stem happens to be "icon", which is why the old literal
    // matched it.
    format!(
        "{icon_name}{point_size}x{point_size}_{appearance_name}_{uuid}-{pid}-{hex}.png"
    )
}

/// Sizes Apple's actool emits for `.icon` bundles: one rendition per point
/// size, all @2x except the 1024pt slot which is @1x. The 16/32/64 slots
/// get bundled into a packed atlas; the rest are stored inline.
const MACOS_ICON_SIZES: &[(u32, u32)] = &[
    (16, 2),
    (32, 2),
    (64, 2),
    (128, 2),
    (256, 2),
    (512, 2),
    (1024, 1),
];

/// Point sizes Apple atlases into ZZZZPackedAsset; the rest are inline.
const ATLAS_POINT_SIZES: &[u32] = &[16, 32, 64];

fn icon_dim2(point_size: u32) -> u16 {
    match point_size {
        16 => 1,
        32 => 2,
        64 => 3,
        128 => 4,
        256 => 5,
        512 => 6,
        1024 => 7,
        _ => 0,
    }
}

pub fn is_icon_bundle(path: &Path) -> bool {
    // Dispatch on `.icon` extension alone. `compile_icon_bundle` produces
    // a clean error when the path isn't a directory or icon.json is missing,
    // rather than silently falling through to the legacy xcassets path
    // (which emits an empty info.plist and no Assets.car).
    path.extension().and_then(|s| s.to_str()) == Some("icon")
}

#[allow(clippy::too_many_arguments)]
pub fn compile_icon_bundle(
    icon_path: &Path,
    output_dir: &Path,
    platform: &str,
    min_deploy: &str,
    app_icon: Option<&str>,
    info_plist_path: Option<&Path>,
    accent_color: Option<&str>,
    standalone_icon_behavior: &str,
) -> Result<Vec<PathBuf>> {
    fs::create_dir_all(output_dir)?;
    let bundle_stem = icon_path
        .file_stem()
        .unwrap_or_default()
        .to_string_lossy()
        .to_string();
    // --app-icon controls the .icns filename and Info.plist value, but
    // Apple's actool always names the in-CAR facet after the bundle stem.
    let icon_name = app_icon.map(|s| s.to_string()).unwrap_or(bundle_stem.clone());

    let icon_json_path = icon_path.join("icon.json");
    let icon_json_text = fs::read_to_string(&icon_json_path).map_err(|e| {
        anyhow::anyhow!("could not read {}: {e}", icon_json_path.display())
    })?;
    let icon_json_value: serde_json::Value = serde_json::from_str(&icon_json_text)?;
    let parsed: IconJson = IconJson::parse(&icon_json_text)?;

    // Apple errors on `{}` (no `groups` key) but accepts `{"groups": []}`.
    // The serde-default Vec collapses both cases, so detect the distinction
    // on the raw JSON value before falling through to layer validation.
    if icon_json_value.get("groups").is_none() {
        anyhow::bail!("icon.json missing required `groups` field");
    }

    // Apple bails when any layer references a missing `image-name` or an
    // image file that doesn't resolve in `Assets/` or the bundle root.
    // Validate up front so we surface a clean error rather than silently
    // emitting an empty catalog.
    validate_layer_image_refs(icon_path, &icon_json_value)?;

    let source_images = find_all_source_images(icon_path, &icon_json_value);
    if source_images.is_empty() {
        return Ok(Vec::new());
    }
    let facet_prefix = bundle_facet_prefix(icon_path);
    let layer_assets = collect_layer_assets(icon_path, &parsed, &facet_prefix);
    // Apple emits a `<stem>/<group_name>` facet for every group even when
    // the group has no `name` field. Anonymous groups fall back to
    // "Group", then "Group 2", "Group 3", … so each facet stays unique
    // across multi-group bundles like ding_icon.
    let group_facet_names: Vec<String> = {
        let mut anon_seq: u32 = 0;
        parsed
            .groups
            .iter()
            .map(|g| {
                let n = match g.name.as_deref() {
                    Some(n) => n.to_string(),
                    None => {
                        anon_seq += 1;
                        if anon_seq == 1 {
                            "Group".to_string()
                        } else {
                            format!("Group {anon_seq}")
                        }
                    }
                };
                format!("{facet_prefix}/{n}")
            })
            .collect()
    };
    let (color_assets, gradient_assets) =
        fill_assets(&facet_prefix, parsed.fill.as_ref(), &parsed)
            .unwrap_or_else(|| (Vec::new(), Vec::new()));

    let mut output_files: Vec<PathBuf> = Vec::new();

    // Route both PNG- and SVG-source `.icon` bundles through the same
    // IconComposer emit path. SVG layers are rasterized to PNGs in a
    // temp dir at 1024x1024 (full size for the layer asset) and at each
    // MACOS_ICON_SIZE (for the multisize + atlas pipeline).
    // Scratch dir for rasterized PNGs from SVG layers and the per-scale
    // resized images. Includes a monotonically incrementing counter so
    // concurrent compiles in the same process (e.g. parallel integration
    // tests) don't share the same path and tear each other down.
    let tmp_seq = TMP_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let tmpdir = std::env::temp_dir().join(format!(
        "actool_icon_{}_{}",
        std::process::id(),
        tmp_seq
    ));
    fs::create_dir_all(&tmpdir)?;
    let primary_source = &source_images[0];
    let primary_is_svg = primary_source
        .to_string_lossy()
        .to_lowercase()
        .ends_with(".svg");

    // (filepath, pixel_size, scale) — fed to build_icon_car
    let mut icon_images: Vec<(PathBuf, u32, u32)> = Vec::new();
    if primary_is_svg {
        let svg_data = fs::read(primary_source)?;
        let (sw, sh) = crate::svg_raster::parse_svg_dimensions(&svg_data);
        if sw == 0 || sh == 0 {
            anyhow::bail!(
                "could not determine intrinsic size of {}",
                primary_source.display()
            );
        }
        for (point_size, scale) in MACOS_ICON_SIZES {
            let pixel_size = point_size * scale;
            let bgra =
                crate::svg_raster::rasterize_svg(&svg_data, *point_size, *point_size, *scale)?;
            let pixel_pf = b"BGRA";
            let mut rgba = bgra.clone();
            for px in rgba.chunks_exact_mut(4) {
                px.swap(0, 2);
            }
            let img = image::RgbaImage::from_raw(pixel_size, pixel_size, rgba)
                .ok_or_else(|| anyhow::anyhow!("svg rasterization size mismatch"))?;
            let filename = format!("Icon{pixel_size}x{pixel_size}.png");
            let filepath = tmpdir.join(&filename);
            img.save(&filepath)?;
            icon_images.push((filepath, pixel_size, *scale));
            let _ = pixel_pf;
        }
    } else {
        let src_img = image::open(primary_source)?.to_rgba8();
        for (point_size, scale) in MACOS_ICON_SIZES {
            let pixel_size = point_size * scale;
            let resized = image::imageops::resize(
                &src_img,
                pixel_size,
                pixel_size,
                FilterType::Lanczos3,
            );
            let filename = format!("Icon{pixel_size}x{pixel_size}.png");
            let filepath = tmpdir.join(&filename);
            resized.save(&filepath)?;
            icon_images.push((filepath, pixel_size, *scale));
        }
    }

    // SVG-sourced layers are emitted as Vector renditions (raw SVG) by
    // build_icon_car, so they pass through untouched here.
    // Top-level fill-specializations triggers Apple's appearance-variant
    // expansion: a second set of pre-rendered sized renditions + an
    // alternate atlas keyed on attribute 24 = 1. Verified on scrumdinger.
    let emit_variant_axis = parsed
        .fill_specializations
        .as_ref()
        .map(|v| !v.is_empty())
        .unwrap_or(false);

    let car_path = output_dir.join("Assets.car");
    build_icon_car(
        &car_path,
        &facet_prefix,
        &icon_images,
        &layer_assets,
        &group_facet_names,
        &color_assets,
        &gradient_assets,
        parsed.groups.first(),
        emit_variant_axis,
        platform,
        min_deploy,
    )?;
    output_files.push(fs::canonicalize(&car_path).unwrap_or(car_path));

    // Apple's empirical rule for `.icon` bundles: emit `<icon_name>.icns`
    // and a populated partial plist (CFBundleIconFile/CFBundleIconName)
    // iff `--app-icon` matches the bundle's filename stem case-sensitively.
    // Verified by toggling the stem against the flag — neither
    // `supported-platforms` nor `--standalone-icon-behavior` affects this.
    let names_match = icon_name == bundle_stem;
    if names_match {
        let icns_path = output_dir.join(format!("{icon_name}.icns"));
        crate::icns::create_icns(&icon_images, &icns_path)?;
        if icns_path.exists() {
            output_files.push(fs::canonicalize(&icns_path).unwrap_or(icns_path));
        }
    }

    let _ = fs::remove_dir_all(&tmpdir);
    let _ = accent_color;
    let _ = standalone_icon_behavior;

    if let Some(path) = info_plist_path {
        if names_match {
            write_populated_partial_plist(path, &icon_name)?;
        } else {
            write_empty_partial_plist(path)?;
        }
        output_files.push(fs::canonicalize(path).unwrap_or(path.to_path_buf()));
    }
    Ok(output_files)
}

/// Build a `<stem>_Assets/<layer_name>` facet entry for each layer that
/// references an image. Source paths are resolved against `<bundle>/Assets/`
/// first, then the bundle root. Layers without a resolvable image are
/// skipped silently — they don't correspond to a source asset.
fn collect_layer_assets(
    bundle: &Path,
    json: &IconJson,
    facet_prefix: &str,
) -> Vec<LayerAsset> {
    let mut out = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for (_group, layer) in json.iter_layers() {
        let Some(image_name) = layer.image_name.as_deref() else {
            continue;
        };
        // Both SVG and raster source layers flow through the same path.
        // SVG layers are rasterized to PNG via materialize_svg_layer_assets
        // before they reach load_image_as_bgra.
        let assets_path = bundle.join("Assets").join(image_name);
        let resolved = if assets_path.exists() {
            assets_path
        } else {
            let root_path = bundle.join(image_name);
            if !root_path.exists() {
                continue;
            }
            root_path
        };
        // Apple names the asset facet after the image-file stem (no
        // extension), not the layer's `name` field. Verified against
        // element-web (element.png → "element"), tagspaces (Image 2.png
        // → "Image 2") and KYA (AppIcon.svg → "AppIcon", layer.name = "Logo").
        let stem = std::path::Path::new(image_name)
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or(image_name);
        let facet_name = format!("{facet_prefix}_Assets/{stem}");
        if !seen.insert(facet_name.clone()) {
            continue;
        }
        out.push(LayerAsset {
            facet_name,
            source_path: resolved,
        });
    }
    out
}

/// Apple's actool errors when a layer has no `image-name` or references an
/// image that doesn't exist in `Assets/` or the bundle root. Mirror that
/// behaviour with explicit messages instead of silently dropping layers
/// (which is what `find_all_source_images` does).
fn validate_layer_image_refs(bundle: &Path, json: &serde_json::Value) -> Result<()> {
    let groups = match json.get("groups").and_then(|v| v.as_array()) {
        Some(g) => g,
        None => return Ok(()),
    };
    for group in groups {
        let Some(layers) = group.get("layers").and_then(|v| v.as_array()) else {
            continue;
        };
        for layer in layers {
            let layer_name = layer
                .get("name")
                .and_then(|v| v.as_str())
                .unwrap_or("(unnamed)");
            let image_name = match layer.get("image-name").and_then(|v| v.as_str()) {
                Some(n) => n,
                None => {
                    anyhow::bail!(
                        "the layer \"{layer_name}\" does not have an image name"
                    );
                }
            };
            let in_assets = bundle.join("Assets").join(image_name);
            let in_root = bundle.join(image_name);
            if !in_assets.exists() && !in_root.exists() {
                anyhow::bail!(
                    "the layer \"{layer_name}\" references an image named \
                     \"{image_name}\" that does not exist"
                );
            }
        }
    }
    Ok(())
}

fn find_all_source_images(bundle: &Path, json: &serde_json::Value) -> Vec<PathBuf> {
    let mut out = Vec::new();
    let mut seen = std::collections::HashSet::new();
    if let Some(groups) = json.get("groups").and_then(|v| v.as_array()) {
        for group in groups {
            if let Some(layers) = group.get("layers").and_then(|v| v.as_array()) {
                for layer in layers {
                    let Some(name) = layer.get("image-name").and_then(|v| v.as_str())
                    else {
                        continue;
                    };
                    if !seen.insert(name.to_string()) {
                        continue;
                    }
                    let assets_path = bundle.join("Assets").join(name);
                    if assets_path.exists() {
                        out.push(assets_path);
                        continue;
                    }
                    let root_path = bundle.join(name);
                    if root_path.exists() {
                        out.push(root_path);
                    }
                }
            }
        }
    }
    out
}

#[allow(clippy::too_many_arguments)]
fn build_icon_car(
    car_path: &Path,
    icon_name: &str,
    icon_images: &[(PathBuf, u32, u32)],
    layer_assets: &[LayerAsset],
    group_facet_names: &[String],
    color_assets: &[ColorAsset],
    gradient_assets: &[GradientAsset],
    group: Option<&crate::icon_json::Group>,
    emit_variant_axis: bool,
    platform: &str,
    min_deploy: &str,
) -> Result<()> {
    let ident = hash_name(icon_name);
    // The actual keyformat is computed from the renditions below — only the
    // attributes they exercise survive. .icon catalogs typically end up with
    // [7, 13, 1, 2, 3, 17, 9, 11, 12] (no direction, no dim1, no variant).
    let placeholder_kf: Vec<u16> = Vec::new();
    let mut renditions: Vec<Rendition> = Vec::new();

    // Each variant (0 = primary, 1 = alternate) gets its own packed_imgs
    // list so the packer routes them into separate atlases (the alternate
    // atlas gets gamut=1 in its name and attribute 24 = 1 in its key).
    let variants: &[u16] = if emit_variant_axis { &[0u16, 1] } else { &[0u16] };
    let mut packed_imgs_per_variant: Vec<Vec<crate::packer::PackedImage>> =
        variants.iter().map(|_| Vec::new()).collect();

    // The sized rendition is the layer composited over the icon's background
    // gradient and clipped to the macOS squircle. The primary variant uses the
    // light gradient (Gradient-1), the alternate uses the dark one (Gradient-2);
    // verified by decoding Apple's GA8/GA16 renditions with libdm2's KCBC path.
    // With no gradient we fall back to the raw layer.
    let light_fill = gradient_assets.first().and_then(|g| resolve_gradient_fill(g, color_assets));
    let dark_fill = gradient_assets
        .get(1)
        .and_then(|g| resolve_gradient_fill(g, color_assets));

    // Drop shadow, resolved per appearance from the group's effect
    // specializations (primary variant → light, alternate → dark).
    use crate::icon_effects::{resolve_icon_effects, Appearance};
    let light_shadow = group.map(|g| resolve_icon_effects(g, Appearance::Light).shadow);
    let dark_shadow = group.map(|g| resolve_icon_effects(g, Appearance::Dark).shadow);

    // Split images into atlas candidates (small sizes) and inline (large).
    // For each size load BGRA pixels, then dispatch by point size. When
    // `emit_variant_axis` is set, every sized rendition is duplicated for
    // the alternate variant (same pixels — the variant axis is structural;
    // CUICatalog reads it to pick which alternate to display per-appearance).
    for (img_path, pixel_size, scale) in icon_images {
        // Force BGRA: the compositor and the GA8/GA16 conversion below both
        // need true 4-byte pixels (a grayscale source would otherwise load as
        // GA8 and be misread as BGRA, halving the rows).
        let (layer_bgra, w, h, _pf_bgra) = load_image_as_bgra(img_path, true)?;
        let point_size = pixel_size / scale;
        let dim2 = icon_dim2(point_size);
        for &variant in variants {
            // Composite the layer over the variant's background gradient,
            // clipped to the squircle. The alternate variant uses the dark
            // gradient when present.
            let fill = if variant == 1 {
                dark_fill.as_ref().or(light_fill.as_ref())
            } else {
                light_fill.as_ref()
            };
            let shadow = shadow_params(
                if variant == 1 { dark_shadow } else { light_shadow },
                *pixel_size,
            );
            let composited = match fill {
                Some(f) => crate::icon_render::composite_icon(
                    *pixel_size,
                    f,
                    &layer_bgra,
                    shadow.as_ref(),
                )
                .unwrap_or_else(|| layer_bgra.clone()),
                None => layer_bgra.clone(),
            };
            // With the variant axis the composite is stored as a grayscale
            // image (variant 0 → GA8 cspace 2, variant 1 → GA16 cspace 6);
            // otherwise it stays BGRA.
            let (pd, pf, cs_id): (Vec<u8>, [u8; 4], u32) = if emit_variant_axis {
                if variant == 0 {
                    (crate::catalog::bgra_to_ga8_force(&composited), *b" 8AG", 2)
                } else {
                    (crate::catalog::bgra_to_ga16_force(&composited), *b"61AG", 6)
                }
            } else {
                (composited, *b"BGRA", car::colorspace_for_pixel_format(b"BGRA"))
            };
            let name = pre_rendered_name(
                icon_name,
                point_size,
                *scale,
                "NSAppearanceNameSystem",
            );
            if ATLAS_POINT_SIZES.contains(&point_size) {
                let mut pi = crate::packer::PackedImage::new(
                    name.clone(),
                    ident as u32,
                    w,
                    h,
                );
                pi.pixel_data = pd;
                pi.pixel_format = pf;
                pi.scale = *scale;
                pi.part = car::PART_ICON as u32;
                pi.dim2 = dim2 as u32;
                pi.variant = variant as u32;
                packed_imgs_per_variant[variant as usize].push(pi);
            } else {
                renditions.push(Rendition {
                    name,
                    identifier: ident,
                    element: car::ELEMENT_UNIVERSAL,
                    part: car::PART_ICON,
                    scale: *scale as u16,
                    width: w,
                    height: h,
                    pixel_data: pd,
                    pixel_format: pf,
                    layout: car::LAYOUT_ONE_PART_SCALE,
                    dim2,
                    variant,
                    keyformat: placeholder_kf.clone(),
                    min_deploy: min_deploy.to_string(),
                    platform: platform.to_string(),
                    colorspace_id: cs_id,
                    // Apple uses bitmapEncoding=0 (original) for the pre-rendered
                    // sized PNGs in .icon catalogs; the default (-1 → auto/4)
                    // sets the rendition_flags bit that makes CUICatalog look
                    // for a template variant that doesn't exist.
                    template_rendering_intent: 0,
                    ..Rendition::default()
                });
            }
        }
    }

    // Pack 16/32/64 into ZZZZPackedAsset atlases — one atlas per variant
    // when the variant axis is active. Each atlas's gamut field controls
    // its name suffix and routes through to the rendition's attribute 24.
    for (variant_idx, packed_imgs) in packed_imgs_per_variant.into_iter().enumerate() {
        if packed_imgs.is_empty() {
            continue;
        }
        let pf = packed_imgs[0].pixel_format;
        let variant = variant_idx as u16;
        let mut atlases = crate::packer::pack_images_split(packed_imgs, 262, 196);
        for atlas in &mut atlases {
            atlas.gamut = variant_idx as u32;
            atlas.render();
            let atlas_name = atlas.name();
            // Apple uses LZFSE (not deepmap2) for icon atlases regardless of
            // pixel format — BGRA, GA8 (variant 0), and GA16 (variant 1) all
            // go through LZFSE.
            let force_lzfse = matches!(&pf, b"BGRA" | b" 8AG" | b"61AG");
            let atlas_csi = car::build_packed_asset_csi(
                &atlas_name,
                atlas.width,
                atlas.height,
                2,
                &pf,
                &atlas.pixel_data,
                min_deploy,
                platform,
                force_lzfse,
            );
            renditions.push(Rendition {
                name: atlas_name.clone(),
                identifier: 0,
                element: car::ELEMENT_PACKED,
                part: car::PART_REGULAR,
                scale: 2,
                width: atlas.width,
                height: atlas.height,
                pixel_data: Vec::new(),
                pixel_format: pf,
                layout: car::LAYOUT_NAME_LIST,
                variant,
                keyformat: placeholder_kf.clone(),
                min_deploy: min_deploy.to_string(),
                platform: platform.to_string(),
                colorspace_id: car::colorspace_for_pixel_format(&pf),
                csi_override: Some(atlas_csi),
                ..Rendition::default()
            });

            for img in &atlas.images {
                let inlk_y = atlas.height - img.y - img.height;
                let ref_csi = car::build_packed_image_csi(
                    &img.name,
                    img.width,
                    img.height,
                    img.scale as u16,
                    &pf,
                    img.x,
                    inlk_y,
                    0,
                    0,
                    0,
                    0,
                );
                renditions.push(Rendition {
                    name: img.name.clone(),
                    identifier: ident,
                    element: car::ELEMENT_UNIVERSAL,
                    part: car::PART_ICON,
                    scale: img.scale as u16,
                    dim2: img.dim2 as u16,
                    width: img.width,
                    height: img.height,
                    pixel_data: Vec::new(),
                    pixel_format: pf,
                    layout: car::LAYOUT_PACKED_IMAGE,
                    variant,
                    keyformat: placeholder_kf.clone(),
                    min_deploy: min_deploy.to_string(),
                    platform: platform.to_string(),
                    colorspace_id: car::colorspace_for_pixel_format(&pf),
                    csi_override: Some(ref_csi),
                    ..Rendition::default()
                });
            }
        }
    }

    let mut ms_entries: Vec<MultisizeImageEntry> = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for (_, pixel_size, scale) in icon_images {
        let pt = pixel_size / scale;
        if seen.insert(pt) {
            ms_entries.push(MultisizeImageEntry {
                width: pt,
                height: pt,
                index: icon_dim2(pt) as u32,
            });
        }
    }
    let mut ms_rend = car::build_multisize_rendition(icon_name, ident, &ms_entries);
    ms_rend.keyformat = placeholder_kf.clone();
    renditions.push(ms_rend);

    // Apple's FACETKEYS entry for the main icon facet carries part = PART_ICON
    // (220), the part of its pre-rendered sized renditions — not the
    // PART_ICON_COMPOSER (245) of the iconstack rendition.
    let mut facets: Vec<FacetEntry> = vec![(
        icon_name.to_string(),
        car::ELEMENT_UNIVERSAL,
        Some(car::PART_ICON),
        ident,
    )];
    // Group facets — `<stem>/<group_name>` — appear in Apple's FACETKEYS
    // alongside the main icon. We don't emit IconGroup renditions yet, but
    // registering the facet lets CoreUI enumerate them.
    for gname in group_facet_names {
        let gid = hash_name(gname);
        facets.push((
            gname.clone(),
            car::ELEMENT_UNIVERSAL,
            Some(car::PART_ICON_GROUP),
            gid,
        ));
    }
    for asset in layer_assets {
        let asset_ident = hash_name(&asset.facet_name);
        let is_svg = asset
            .source_path
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| e.eq_ignore_ascii_case("svg"))
            .unwrap_or(false);
        if is_svg {
            // An SVG-sourced layer is stored as a Vector rendition holding the
            // raw SVG (named "image.svg"), not a rasterized bitmap. Apple keeps
            // the vector so the layer can be re-rendered at any scale.
            let svg_data = fs::read(&asset.source_path)?;
            let csi = car::build_svg_csi("image.svg", &svg_data);
            renditions.push(Rendition {
                name: "image.svg".to_string(),
                identifier: asset_ident,
                element: car::ELEMENT_UNIVERSAL,
                part: car::PART_REGULAR,
                scale: 1,
                width: 0,
                height: 0,
                pixel_data: Vec::new(),
                pixel_format: *car::PIXELFMT_SVG,
                layout: car::LAYOUT_PDF,
                keyformat: placeholder_kf.clone(),
                min_deploy: min_deploy.to_string(),
                platform: platform.to_string(),
                colorspace_id: 0,
                csi_override: Some(csi),
                ..Rendition::default()
            });
            facets.push((
                asset.facet_name.clone(),
                car::ELEMENT_UNIVERSAL,
                Some(car::PART_REGULAR),
                asset_ident,
            ));
            continue;
        }
        let (pd, w, h, pf) = load_image_as_bgra(&asset.source_path, false)?;
        // Apple's actool normalizes the in-CAR rendition name for a layer's
        // source image to "image.png" rather than the original filename.
        // CoreUI keys renditions by attribute tuple, not by name, so this is
        // metadata-only — but it matches Apple's catalog byte-for-byte for
        // the single-layer case.
        renditions.push(Rendition {
            name: "image.png".to_string(),
            identifier: asset_ident,
            element: car::ELEMENT_UNIVERSAL,
            part: car::PART_REGULAR,
            scale: 1,
            width: w,
            height: h,
            pixel_data: pd,
            pixel_format: pf,
            layout: car::LAYOUT_ONE_PART_SCALE,
            keyformat: placeholder_kf.clone(),
            min_deploy: min_deploy.to_string(),
            platform: platform.to_string(),
            colorspace_id: car::colorspace_for_pixel_format(&pf),
            // Apple stores layer source images as non-opaque (CELM ver=0)
            // even when the source image has alpha=255 everywhere, so the
            // layer composites with alpha against other stack layers.
            force_non_opaque: true,
            ..Rendition::default()
        });
        facets.push((
            asset.facet_name.clone(),
            car::ELEMENT_UNIVERSAL,
            Some(car::PART_REGULAR),
            asset_ident,
        ));
    }

    for color in color_assets {
        let cident = hash_name(&color.facet_name);
        let csi = car::build_icon_color_csi(
            &color.facet_name,
            color.colorspace_id,
            &color.components,
        );
        // Apple's Color rendition KEY has scale=1 (even though the CSI's own
        // scale_factor field is 0). CUICatalog filters lookups by scale=1
        // by default, so scale=0 keys are invisible to colorWithName:.
        renditions.push(Rendition {
            name: color.facet_name.clone(),
            identifier: cident,
            element: car::ELEMENT_UNIVERSAL,
            part: car::PART_COLOR,
            scale: 1,
            width: 0,
            height: 0,
            pixel_data: Vec::new(),
            pixel_format: *b"\0\0\0\0",
            layout: car::LAYOUT_COLOR,
            keyformat: placeholder_kf.clone(),
            min_deploy: min_deploy.to_string(),
            platform: platform.to_string(),
            colorspace_id: 0,
            csi_override: Some(csi),
            ..Rendition::default()
        });
        facets.push((
            color.facet_name.clone(),
            car::ELEMENT_UNIVERSAL,
            Some(car::PART_COLOR),
            cident,
        ));
    }

    for grad in gradient_assets {
        let gident = hash_name(&grad.facet_name);
        let stops: Vec<(f32, &str)> = grad
            .stops
            .iter()
            .map(|(p, name)| (*p, name.as_str()))
            .collect();
        let csi = car::build_icon_gradient_csi(
            &grad.facet_name,
            grad.geometry,
            &stops,
            grad.kind,
        );
        // Gradient KEYs follow the same scale=1 convention as Colors.
        renditions.push(Rendition {
            name: grad.facet_name.clone(),
            identifier: gident,
            element: car::ELEMENT_UNIVERSAL,
            part: car::PART_ICON_GRADIENT,
            scale: 1,
            width: 0,
            height: 0,
            pixel_data: Vec::new(),
            pixel_format: *b"\0\0\0\0",
            layout: car::LAYOUT_GRADIENT,
            keyformat: placeholder_kf.clone(),
            min_deploy: min_deploy.to_string(),
            platform: platform.to_string(),
            colorspace_id: 0,
            csi_override: Some(csi),
            ..Rendition::default()
        });
        facets.push((
            grad.facet_name.clone(),
            car::ELEMENT_UNIVERSAL,
            Some(car::PART_ICON_GRADIENT),
            gident,
        ));
    }

    // Apple emits iconstack + IconGroup per non-system appearance for .icon
    // bundles. Only meaningful when we have a group + at least one image
    // layer + gradients to back the appearance variants.
    let layered = !group_facet_names.is_empty()
        && !layer_assets.is_empty()
        && !gradient_assets.is_empty();
    if layered {
        let group_facet = &group_facet_names[0];
        let group_ident = hash_name(group_facet);
        let main_ident = hash_name(icon_name);
        let stack_name = format!("{icon_name}.iconstack");
        let layer_asset = &layer_assets[0];
        let layer_ident = hash_name(&layer_asset.facet_name);
        // Map appearance ID -> gradient facet name. 1=DarkAqua uses the
        // second (dark) gradient; 8=Aqua and 10=Tintable use the first.
        let grad1_ident = hash_name(&gradient_assets[0].facet_name);
        let grad2_ident = hash_name(
            &gradient_assets
                .get(1)
                .map(|g| g.facet_name.clone())
                .unwrap_or_else(|| gradient_assets[0].facet_name.clone()),
        );
        for appearance in [1u16, 8, 10] {
            let grad_id = if appearance == 1 { grad2_ident } else { grad1_ident };
            let stack_csi = car::build_iconstack_csi(
                &stack_name,
                1024,
                &[
                    car::LayerRef {
                        part: car::PART_ICON_GRADIENT,
                        identifier: grad_id,
                    },
                    car::LayerRef {
                        part: car::PART_ICON_GROUP,
                        identifier: group_ident,
                    },
                ],
            );
            renditions.push(Rendition {
                name: stack_name.clone(),
                identifier: main_ident,
                element: car::ELEMENT_UNIVERSAL,
                part: car::PART_ICON_COMPOSER,
                scale: 1,
                appearance,
                width: 1024,
                height: 1024,
                pixel_data: Vec::new(),
                pixel_format: *car::PIXELFMT_DATA,
                layout: car::LAYOUT_ICONSTACK,
                keyformat: placeholder_kf.clone(),
                min_deploy: min_deploy.to_string(),
                platform: platform.to_string(),
                colorspace_id: 0,
                csi_override: Some(stack_csi),
                ..Rendition::default()
            });

            let group_csi = car::build_icongroup_csi(
                "IconGroup",
                1024,
                &[car::LayerRef {
                    part: car::PART_REGULAR,
                    identifier: layer_ident,
                }],
            );
            renditions.push(Rendition {
                name: "IconGroup".to_string(),
                identifier: group_ident,
                element: car::ELEMENT_UNIVERSAL,
                part: car::PART_ICON_GROUP,
                scale: 1,
                appearance,
                width: 0,
                height: 0,
                pixel_data: Vec::new(),
                pixel_format: *car::PIXELFMT_DATA,
                layout: car::LAYOUT_ICON_GROUP,
                keyformat: placeholder_kf.clone(),
                min_deploy: min_deploy.to_string(),
                platform: platform.to_string(),
                colorspace_id: 0,
                csi_override: Some(group_csi),
                ..Rendition::default()
            });
        }
    }

    let keyformat = car::compute_keyformat(&renditions, false);
    for rend in &mut renditions {
        rend.keyformat = keyformat.clone();
    }

    let mut all_entries: Vec<(Vec<u8>, Vec<u8>)> = Vec::new();
    for rend in &renditions {
        let key = rend.build_rendition_key();
        let csi = rend.build_csi();
        all_entries.push((key, csi));
    }
    all_entries.sort_by(|a, b| a.0.cmp(&b.0));

    write_icon_car(car_path, &facets, &keyformat, &all_entries, platform, min_deploy)
}

/// A layer's source image, emitted as a `<stem>_Assets/<layer_name>` facet
/// referencing an inline rendition of the image bytes.
pub struct LayerAsset {
    pub facet_name: String,
    pub source_path: PathBuf,
}

/// A solid color asset extracted from icon.json `fill` / `fill-specializations`.
struct ColorAsset {
    facet_name: String,
    colorspace_id: u32,
    components: Vec<f64>,
}

/// A gradient asset extracted from icon.json `fill` specs. Stops reference
/// Color assets by facet name (e.g. "icon_Assets/Color-2").
struct GradientAsset {
    facet_name: String,
    geometry: [f32; 4],
    stops: Vec<(f32, String)>,
    /// 0 = single-color (radial-style; Apple emits this for the user
    /// color in `automatic-gradient`), 1 = linear top-to-bottom.
    kind: u32,
}

/// Treat a Color's stored components as device-RGB for compositing. Gray /
/// extended-gray (2 components) expand to a neutral triple; 4-component
/// spaces (srgb / p3) pass their RGB through (p3→sRGB primaries differ, but
/// the gray-axis backgrounds we composite are unaffected).
fn color_to_rgb(c: &ColorAsset) -> [f64; 3] {
    match c.components.as_slice() {
        [g, _a] => [*g, *g, *g],
        [r, g, b, _a] => [*r, *g, *b],
        _ => [0.0, 0.0, 0.0],
    }
}

/// Build per-size CoreGraphics drop-shadow parameters from a resolved
/// `ShadowSpec`. `None` when there is no shadow. The colour alpha is set so
/// that, once Gaussian-blurred over the squircle edge, the halo peaks near the
/// measured `PEAK_ALPHA`; neutral and layer-color shadows are both approximated
/// as black (dark-mode backgrounds make the distinction negligible).
fn shadow_params(
    spec: Option<crate::icon_effects::ShadowSpec>,
    pixel_size: u32,
) -> Option<crate::icon_render::ShadowParams> {
    use crate::icon_effects::shadow_geometry::{BLUR_RATIO, OFFSET_Y_RATIO, PEAK_ALPHA};
    use crate::icon_effects::ShadowKind;
    let spec = spec?;
    if spec.kind == ShadowKind::None || spec.opacity <= 0.0 {
        return None;
    }
    let alpha = (2.0 * PEAK_ALPHA * spec.opacity as f64).min(1.0);
    Some(crate::icon_render::ShadowParams {
        color: [0.0, 0.0, 0.0, alpha],
        blur: pixel_size as f64 * BLUR_RATIO,
        offset: [0.0, pixel_size as f64 * OFFSET_Y_RATIO],
    })
}

/// Resolve a GradientAsset into a renderable background fill, looking up each
/// stop's RGB in the palette. A single-stop gradient fills both ends with the
/// same color.
fn resolve_gradient_fill(
    grad: &GradientAsset,
    colors: &[ColorAsset],
) -> Option<crate::icon_render::GradientFill> {
    let rgb = |facet: &str| -> Option<[f64; 3]> {
        colors.iter().find(|c| c.facet_name == facet).map(color_to_rgb)
    };
    let start_rgb = rgb(&grad.stops.first()?.1)?;
    let stop_rgb = rgb(&grad.stops.last()?.1)?;
    let g = grad.geometry;
    Some(crate::icon_render::GradientFill {
        start_rgb,
        stop_rgb,
        start: [g[0], g[1]],
        stop: [g[2], g[3]],
    })
}

/// Apple's default palette for `fill: "automatic"`. Empirically observed in
/// actool output: a special white anchor color, light-mode bg gradient
/// (Color-2 → Color-3), and dark-mode bg gradient (Color-4 → Color-5).
///
/// Apple's actool stores the gray channel as `f64(f32(v))` — i.e. promotes
/// from a 32-bit float to a 64-bit field. We round-trip through f32 so the
/// emitted CSI bytes match Apple's exactly.
fn automatic_fill_assets(facet_prefix: &str) -> (Vec<ColorAsset>, Vec<GradientAsset>) {
    let n = |s: &str| format!("{facet_prefix}_Assets/{s}");
    let g = |v: f32| (v as f32) as f64;
    let colors = vec![
        ColorAsset {
            facet_name: n("Color-1"),
            colorspace_id: 6,
            components: vec![g(1.0), g(1.0)],
        },
        ColorAsset {
            facet_name: n("Color-2"),
            colorspace_id: 2,
            components: vec![g(1.0), g(1.0)],
        },
        ColorAsset {
            facet_name: n("Color-3"),
            colorspace_id: 2,
            components: vec![g(0.925), g(1.0)],
        },
        ColorAsset {
            facet_name: n("Color-4"),
            colorspace_id: 2,
            components: vec![g(0.192), g(1.0)],
        },
        ColorAsset {
            facet_name: n("Color-5"),
            colorspace_id: 2,
            components: vec![g(0.078), g(1.0)],
        },
    ];
    let gradients = vec![
        GradientAsset {
            facet_name: n("Gradient-1"),
            geometry: [0.5, 0.0, 0.5, 1.0],
            stops: vec![(0.0, n("Color-2")), (1.0, n("Color-3"))],
            kind: 1,
        },
        GradientAsset {
            facet_name: n("Gradient-2"),
            geometry: [0.5, 0.0, 0.5, 1.0],
            stops: vec![(0.0, n("Color-4")), (1.0, n("Color-5"))],
            kind: 1,
        },
    ];
    (colors, gradients)
}

/// Determine whether `fill` is the "automatic" keyword (the only fill shape
/// we currently generate Color/Gradient renditions for). Returns true for an
/// explicit `"automatic"` string AND for an absent fill — Apple's actool
/// treats both the same way.
fn fill_is_automatic(fill: Option<&Fill>) -> bool {
    match fill {
        None => true,
        Some(Fill::Keyword(k)) => k == "automatic",
        Some(Fill::Structured(_)) => false,
    }
}

/// A parsed Apple color-spec string like `srgb:0.97288,0.97288,0.97288,1.0`
/// or `extended-gray:0.84536,1.0`. Returns (colorspace_id, components).
fn parse_color_spec(spec: &str) -> Option<(u32, Vec<f64>)> {
    let (name, rest) = spec.split_once(':')?;
    // Apple rounds color-spec literals to 3 decimal places before promoting
    // to f32 and storing as f64 — verified empirically: spec "0.97288"
    // becomes f32(0.973) = 0.9729999…, not f32(0.97288). Mirror that.
    let comps: Vec<f64> = rest
        .split(',')
        .map(|s| {
            let f: f64 = s.trim().parse().ok()?;
            let rounded = (f * 1000.0).round() / 1000.0;
            Some(rounded as f32 as f64)
        })
        .collect::<Option<Vec<_>>>()?;
    // Empirical colorspace ids from Apple's RLOC blobs:
    //   srgb:r,g,b,a              → cspace=1 (4 components)
    //   gray:gray,alpha           → cspace=2 (2 components)
    //   display-p3:r,g,b,a        → cspace=3 (4 components)  [classhub]
    //   extended-srgb:r,g,b,a     → cspace=4 (4 components)  [recipe-scraper]
    //   extended-gray:gray,alpha  → cspace=6 (2 components)
    let cspace = match name {
        "srgb" => 1,
        "gray" => 2,
        "display-p3" => 3,
        "extended-srgb" => 4,
        "extended-gray" => 6,
        _ => return None,
    };
    Some((cspace, comps))
}

/// Apple's palette for a `fill: {"solid": "<spec>"}` icon. Empirically
/// observed for tagspaces (srgb solid): 4 Colors + 1 Gradient.
/// Color-1: fixed white anchor (cspace=6)
/// Color-2: the user-provided color (cspace from spec)
/// Color-3: 0.192 extended-gray (dark mode mid-gray)
/// Color-4: 0.078 extended-gray (dark mode bottom)
/// Gradient-1: Color-3 → Color-4 (linear top-to-bottom, dark mode bg)
fn solid_fill_assets(
    facet_prefix: &str,
    spec: &str,
) -> Option<(Vec<ColorAsset>, Vec<GradientAsset>)> {
    let (user_cspace, user_components) = parse_color_spec(spec)?;
    let n = |s: &str| format!("{facet_prefix}_Assets/{s}");
    let g = |v: f32| (v as f32) as f64;
    let colors = vec![
        ColorAsset {
            facet_name: n("Color-1"),
            colorspace_id: 6,
            components: vec![g(1.0), g(1.0)],
        },
        ColorAsset {
            facet_name: n("Color-2"),
            colorspace_id: user_cspace,
            components: user_components,
        },
        ColorAsset {
            facet_name: n("Color-3"),
            colorspace_id: 2,
            components: vec![g(0.192), g(1.0)],
        },
        ColorAsset {
            facet_name: n("Color-4"),
            colorspace_id: 2,
            components: vec![g(0.078), g(1.0)],
        },
    ];
    let gradients = vec![GradientAsset {
        facet_name: n("Gradient-1"),
        geometry: [0.5, 0.0, 0.5, 1.0],
        stops: vec![(0.0, n("Color-3")), (1.0, n("Color-4"))],
        kind: 1,
    }];
    Some((colors, gradients))
}

/// Apple's palette for `fill: {"linear-gradient": ["<stop0>", "<stop1>"]}` —
/// observed in classhub (display-p3 stops) and recipe-scraper (extended-srgb
/// stops). Identical structure to the "automatic" palette: 5 Colors + 2
/// linear Gradients, but the two USER-PROVIDED stops fill Color-2/Color-3
/// in their declared colorspace and back the light-mode Gradient-1.
fn linear_gradient_fill_assets(
    facet_prefix: &str,
    stops_spec: &[&str],
) -> Option<(Vec<ColorAsset>, Vec<GradientAsset>)> {
    if stops_spec.len() < 2 {
        return None;
    }
    let (cs0, c0) = parse_color_spec(stops_spec[0])?;
    let (cs1, c1) = parse_color_spec(stops_spec[1])?;
    let n = |s: &str| format!("{facet_prefix}_Assets/{s}");
    let g = |v: f32| (v as f32) as f64;
    let colors = vec![
        ColorAsset {
            facet_name: n("Color-1"),
            colorspace_id: 6,
            components: vec![g(1.0), g(1.0)],
        },
        ColorAsset {
            facet_name: n("Color-2"),
            colorspace_id: cs0,
            components: c0,
        },
        ColorAsset {
            facet_name: n("Color-3"),
            colorspace_id: cs1,
            components: c1,
        },
        ColorAsset {
            facet_name: n("Color-4"),
            colorspace_id: 2,
            components: vec![g(0.192), g(1.0)],
        },
        ColorAsset {
            facet_name: n("Color-5"),
            colorspace_id: 2,
            components: vec![g(0.078), g(1.0)],
        },
    ];
    let gradients = vec![
        GradientAsset {
            facet_name: n("Gradient-1"),
            geometry: [0.5, 0.0, 0.5, 1.0],
            stops: vec![(0.0, n("Color-2")), (1.0, n("Color-3"))],
            kind: 1,
        },
        GradientAsset {
            facet_name: n("Gradient-2"),
            geometry: [0.5, 0.0, 0.5, 1.0],
            stops: vec![(0.0, n("Color-4")), (1.0, n("Color-5"))],
            kind: 1,
        },
    ];
    Some((colors, gradients))
}

/// Apple's palette for `fill: {"automatic-gradient": "<color spec>"}` —
/// observed in ding_icon. Same 4 base Colors as the solid case, but
/// emits TWO gradients: Gradient-1 is a single-stop "user-color"
/// gradient (kind=0) pointing at Color-2, and Gradient-2 is the
/// standard dark-mode background (Color-3 → Color-4).
fn automatic_gradient_fill_assets(
    facet_prefix: &str,
    spec: &str,
) -> Option<(Vec<ColorAsset>, Vec<GradientAsset>)> {
    let (user_cspace, user_components) = parse_color_spec(spec)?;
    let n = |s: &str| format!("{facet_prefix}_Assets/{s}");
    let g = |v: f32| (v as f32) as f64;
    let colors = vec![
        ColorAsset {
            facet_name: n("Color-1"),
            colorspace_id: 6,
            components: vec![g(1.0), g(1.0)],
        },
        ColorAsset {
            facet_name: n("Color-2"),
            colorspace_id: user_cspace,
            components: user_components,
        },
        ColorAsset {
            facet_name: n("Color-3"),
            colorspace_id: 2,
            components: vec![g(0.192), g(1.0)],
        },
        ColorAsset {
            facet_name: n("Color-4"),
            colorspace_id: 2,
            components: vec![g(0.078), g(1.0)],
        },
    ];
    let gradients = vec![
        GradientAsset {
            facet_name: n("Gradient-1"),
            geometry: [0.5, 0.0, 0.5, 1.0],
            stops: vec![(0.0, n("Color-2"))],
            kind: 0,
        },
        GradientAsset {
            facet_name: n("Gradient-2"),
            geometry: [0.5, 0.0, 0.5, 1.0],
            stops: vec![(0.0, n("Color-3")), (1.0, n("Color-4"))],
            kind: 1,
        },
    ];
    Some((colors, gradients))
}

/// Apple's palette for `fill: "system-dark"` — observed in KYA:
/// 3 colors (white anchor + mid-dark + deep-dark) plus TWO identical
/// dark gradients. Per-layer fills are appended afterwards as Color-N+1.
fn system_dark_fill_assets(facet_prefix: &str) -> (Vec<ColorAsset>, Vec<GradientAsset>) {
    let n = |s: &str| format!("{facet_prefix}_Assets/{s}");
    let g = |v: f32| (v as f32) as f64;
    let colors = vec![
        ColorAsset {
            facet_name: n("Color-1"),
            colorspace_id: 6,
            components: vec![g(1.0), g(1.0)],
        },
        ColorAsset {
            facet_name: n("Color-2"),
            colorspace_id: 2,
            components: vec![g(0.192), g(1.0)],
        },
        ColorAsset {
            facet_name: n("Color-3"),
            colorspace_id: 2,
            components: vec![g(0.078), g(1.0)],
        },
    ];
    let gradients = vec![
        GradientAsset {
            facet_name: n("Gradient-1"),
            geometry: [0.5, 0.0, 0.5, 1.0],
            stops: vec![(0.0, n("Color-2")), (1.0, n("Color-3"))],
            kind: 1,
        },
        GradientAsset {
            facet_name: n("Gradient-2"),
            geometry: [0.5, 0.0, 0.5, 1.0],
            stops: vec![(0.0, n("Color-2")), (1.0, n("Color-3"))],
            kind: 1,
        },
    ];
    (colors, gradients)
}

/// Append a Color-N rendition for each layer-level solid color we haven't
/// already emitted. Apple aggregates these from:
///   * layer.fill = {"solid": "<spec>"}             — KYA's case
///   * layer.fill-specializations[*].value.solid    — recipe-scraper's case
/// Only specs that don't already appear in the base palette get a new
/// Color-N. The fixtures we've inspected stable-sort by document order.
fn append_layer_fill_colors(
    facet_prefix: &str,
    json: &IconJson,
    colors: &mut Vec<ColorAsset>,
) {
    fn try_add(
        spec: &str,
        facet_prefix: &str,
        colors: &mut Vec<ColorAsset>,
    ) {
        let Some((cspace, comps)) = parse_color_spec(spec) else {
            return;
        };
        let already = colors
            .iter()
            .any(|c| c.colorspace_id == cspace && c.components == comps);
        if already {
            return;
        }
        let next_idx = colors.len() + 1;
        colors.push(ColorAsset {
            facet_name: format!("{facet_prefix}_Assets/Color-{next_idx}"),
            colorspace_id: cspace,
            components: comps,
        });
    }
    for (_group, layer) in json.iter_layers() {
        if let Some(Fill::Structured(v)) = layer.fill.as_ref() {
            if let Some(spec) = v.get("solid").and_then(|s| s.as_str()) {
                try_add(spec, facet_prefix, colors);
            }
        }
        if let Some(specs) = layer.fill_specializations.as_ref() {
            for sp in specs {
                let Some(value) = sp.get("value") else { continue };
                let Some(solid) = value.get("solid").and_then(|s| s.as_str()) else {
                    continue;
                };
                try_add(solid, facet_prefix, colors);
            }
        }
    }
}

/// Resolve a fill-specialization keyword `value` to its background gray pair
/// (top stop, bottom stop). "system-light"/"system-dark" are fixed; bare
/// "automatic" resolves by the entry's appearance (dark → dark pair, else
/// the light pair). Returns None for keywords we don't model.
fn keyword_bg_pair(keyword: &str, appearance: Option<&str>) -> Option<(f64, f64)> {
    match keyword {
        "system-light" => Some((1.0, 0.925)),
        "system-dark" => Some((0.192, 0.078)),
        "automatic" => {
            if appearance == Some("dark") {
                Some((0.192, 0.078))
            } else {
                Some((1.0, 0.925))
            }
        }
        _ => None,
    }
}

/// Add a Color, deduplicating by (colorspace, components). Returns the facet
/// name of the existing or newly-created Color so gradients can reference it.
fn palette_add_color(
    colors: &mut Vec<ColorAsset>,
    facet_prefix: &str,
    colorspace_id: u32,
    components: Vec<f64>,
) -> String {
    if let Some(existing) = colors
        .iter()
        .find(|c| c.colorspace_id == colorspace_id && c.components == components)
    {
        return existing.facet_name.clone();
    }
    let idx = colors.len() + 1;
    let name = format!("{facet_prefix}_Assets/Color-{idx}");
    colors.push(ColorAsset {
        facet_name: name.clone(),
        colorspace_id,
        components,
    });
    name
}

/// Add a linear Gradient, deduplicating by (geometry, stops). A layer-level
/// keyword fill that resolves to a background pair already emitted (e.g.
/// scrumdinger's redundant dark "automatic") collapses to nothing here.
fn palette_add_gradient(
    gradients: &mut Vec<GradientAsset>,
    facet_prefix: &str,
    geometry: [f32; 4],
    stops: Vec<(f32, String)>,
) {
    if gradients
        .iter()
        .any(|g| g.kind == 1 && g.geometry == geometry && g.stops == stops)
    {
        return;
    }
    let idx = gradients.len() + 1;
    gradients.push(GradientAsset {
        facet_name: format!("{facet_prefix}_Assets/Gradient-{idx}"),
        geometry,
        stops,
        kind: 1,
    });
}

/// Read a gradient `orientation` object into geometry [start.x, start.y,
/// stop.x, stop.y]. Defaults to a top-to-bottom gradient when absent.
fn parse_orientation(obj: &serde_json::Map<String, serde_json::Value>) -> [f32; 4] {
    let default = [0.5f32, 0.0, 0.5, 1.0];
    let Some(orient) = obj.get("orientation") else {
        return default;
    };
    let pt = |key: &str, dx: f32, dy: f32| -> (f32, f32) {
        let p = orient.get(key);
        let x = p
            .and_then(|o| o.get("x"))
            .and_then(|v| v.as_f64())
            .map(|v| v as f32)
            .unwrap_or(dx);
        let y = p
            .and_then(|o| o.get("y"))
            .and_then(|v| v.as_f64())
            .map(|v| v as f32)
            .unwrap_or(dy);
        (x, y)
    };
    let (sx, sy) = pt("start", 0.5, 0.0);
    let (ex, ey) = pt("stop", 0.5, 1.0);
    [sx, sy, ex, ey]
}

/// Fold one fill-specialization `value` into the running palette. Handles the
/// three value shapes Apple emits: a keyword ("system-light"/"system-dark"/
/// "automatic") → a two-stop gray background gradient; a structured
/// `{linear-gradient: [...], orientation}` → its stops + an oriented gradient;
/// and `{solid: "<spec>"}` → a single color.
fn process_fill_value(
    value: &serde_json::Value,
    appearance: Option<&str>,
    facet_prefix: &str,
    colors: &mut Vec<ColorAsset>,
    gradients: &mut Vec<GradientAsset>,
) {
    let g = |v: f64| (v as f32) as f64;
    if let Some(s) = value.as_str() {
        if let Some((top, bottom)) = keyword_bg_pair(s, appearance) {
            let c0 = palette_add_color(colors, facet_prefix, 2, vec![g(top), g(1.0)]);
            let c1 = palette_add_color(colors, facet_prefix, 2, vec![g(bottom), g(1.0)]);
            palette_add_gradient(
                gradients,
                facet_prefix,
                [0.5, 0.0, 0.5, 1.0],
                vec![(0.0, c0), (1.0, c1)],
            );
        }
        return;
    }
    let Some(obj) = value.as_object() else {
        return;
    };
    if let Some(arr) = obj.get("linear-gradient").and_then(|x| x.as_array()) {
        let specs: Vec<&str> = arr.iter().filter_map(|x| x.as_str()).collect();
        if specs.len() < 2 {
            return;
        }
        let geometry = parse_orientation(obj);
        let mut names = Vec::with_capacity(specs.len());
        for spec in &specs {
            let Some((cspace, comps)) = parse_color_spec(spec) else {
                return;
            };
            names.push(palette_add_color(colors, facet_prefix, cspace, comps));
        }
        let last = (names.len() - 1) as f32;
        let stops: Vec<(f32, String)> = names
            .into_iter()
            .enumerate()
            .map(|(i, n)| (i as f32 / last, n))
            .collect();
        palette_add_gradient(gradients, facet_prefix, geometry, stops);
    } else if let Some(solid) = obj.get("solid").and_then(|x| x.as_str()) {
        if let Some((cspace, comps)) = parse_color_spec(solid) {
            palette_add_color(colors, facet_prefix, cspace, comps);
        }
    }
}

/// Apple's palette for a top-level `fill-specializations` block. The white
/// anchor Color-1 is always first; then each top-level specialization is
/// folded in document order, followed by every layer's fill and
/// fill-specializations. Reverse-engineered against feishin (custom p3
/// gradient + system-dark + layer gradient → 8 Colors / 3 Gradients) and
/// scrumdinger (system-light + automatic → 5 Colors / 2 Gradients).
fn fill_specializations_assets(
    facet_prefix: &str,
    json: &IconJson,
) -> (Vec<ColorAsset>, Vec<GradientAsset>) {
    let g = |v: f32| (v as f32) as f64;
    let mut colors = vec![ColorAsset {
        facet_name: format!("{facet_prefix}_Assets/Color-1"),
        colorspace_id: 6,
        components: vec![g(1.0), g(1.0)],
    }];
    let mut gradients: Vec<GradientAsset> = Vec::new();
    if let Some(specs) = json.fill_specializations.as_ref() {
        for sp in specs {
            let appearance = sp.get("appearance").and_then(|a| a.as_str());
            if let Some(v) = sp.get("value") {
                process_fill_value(v, appearance, facet_prefix, &mut colors, &mut gradients);
            }
        }
    }
    for (_group, layer) in json.iter_layers() {
        if let Some(Fill::Structured(v)) = layer.fill.as_ref() {
            process_fill_value(v, None, facet_prefix, &mut colors, &mut gradients);
        }
        if let Some(specs) = layer.fill_specializations.as_ref() {
            for sp in specs {
                let appearance = sp.get("appearance").and_then(|a| a.as_str());
                if let Some(v) = sp.get("value") {
                    process_fill_value(v, appearance, facet_prefix, &mut colors, &mut gradients);
                }
            }
        }
    }
    (colors, gradients)
}

/// Try to derive Color/Gradient assets from an arbitrary fill spec.
/// Returns None when the spec shape is unrecognized — caller falls back
/// to no palette and the catalog stays self-consistent.
fn fill_assets(
    facet_prefix: &str,
    fill: Option<&Fill>,
    parsed: &IconJson,
) -> Option<(Vec<ColorAsset>, Vec<GradientAsset>)> {
    // A top-level `fill-specializations` block drives its own appearance-keyed
    // palette, distinct from the single-`fill` shapes below.
    if parsed
        .fill_specializations
        .as_ref()
        .map(|v| !v.is_empty())
        .unwrap_or(false)
    {
        return Some(fill_specializations_assets(facet_prefix, parsed));
    }
    if fill_is_automatic(fill) {
        let (mut colors, gradients) = automatic_fill_assets(facet_prefix);
        append_layer_fill_colors(facet_prefix, parsed, &mut colors);
        return Some((colors, gradients));
    }
    let fill_val = fill?;
    match fill_val {
        Fill::Keyword(k) if k == "system-dark" => {
            let (mut colors, gradients) = system_dark_fill_assets(facet_prefix);
            append_layer_fill_colors(facet_prefix, parsed, &mut colors);
            Some((colors, gradients))
        }
        Fill::Structured(v) => {
            if let Some(spec) = v.get("solid").and_then(|s| s.as_str()) {
                let (mut colors, gradients) = solid_fill_assets(facet_prefix, spec)?;
                append_layer_fill_colors(facet_prefix, parsed, &mut colors);
                return Some((colors, gradients));
            }
            if let Some(spec) = v.get("automatic-gradient").and_then(|s| s.as_str()) {
                let (mut colors, gradients) =
                    automatic_gradient_fill_assets(facet_prefix, spec)?;
                append_layer_fill_colors(facet_prefix, parsed, &mut colors);
                return Some((colors, gradients));
            }
            if let Some(arr) = v.get("linear-gradient").and_then(|s| s.as_array()) {
                let specs: Vec<&str> = arr.iter().filter_map(|x| x.as_str()).collect();
                let (mut colors, gradients) =
                    linear_gradient_fill_assets(facet_prefix, &specs)?;
                append_layer_fill_colors(facet_prefix, parsed, &mut colors);
                return Some((colors, gradients));
            }
            None
        }
        _ => None,
    }
}

fn write_icon_car(
    car_path: &Path,
    facets: &[FacetEntry],
    keyformat: &[u16],
    all_entries: &[(Vec<u8>, Vec<u8>)],
    platform: &str,
    min_deploy: &str,
) -> Result<()> {
    let mut bom = BomWriter::new();
    // Declare CoreUI 975 so the IconComposer code paths in CoreUI activate
    // — older values cause silent `imagesWithName:` empty results even when
    // FACETKEYS / RENDITIONS are byte-identical to Apple's output.
    bom.add_named_block(
        "CARHEADER",
        car::make_carheader_versioned(all_entries.len() as u32, 975),
    );
    // The named-block ORDER below matches Apple's actool exactly. CUICatalog
    // appears to scan named blocks during initWithURL: in BOM order and
    // expects RENDITIONS to register early, before the auxiliary trees.
    bom.set_inline_key_size(Some(keyformat.len() * 2));
    bom.add_tree("RENDITIONS", all_entries, 4096);
    bom.set_inline_key_size(None);

    let mut facetkey_entries: Vec<(Vec<u8>, Vec<u8>)> = facets
        .iter()
        .map(|(name, element, part, ident)| {
            (
                name.as_bytes().to_vec(),
                car::make_facetkey_value(*element, *part, *ident),
            )
        })
        .collect();
    facetkey_entries.sort_by(|a, b| a.0.cmp(&b.0));
    bom.add_tree("FACETKEYS", &facetkey_entries, 4096);

    let mut appearance_entries = car::make_appearancekeys_entries();
    appearance_entries.sort_by(|a, b| a.0.cmp(&b.0));
    bom.add_tree("APPEARANCEKEYS", &appearance_entries, 4096);

    bom.add_named_block("KEYFORMAT", car::make_keyformat(keyformat));
    bom.add_named_block(
        "EXTENDED_METADATA",
        car::make_extended_metadata(platform, min_deploy),
    );

    let bitmap_entries = build_bitmapkeys(all_entries, keyformat);
    bom.add_raw_key_tree("BITMAPKEYS", &bitmap_entries, 1024);
    bom.write(car_path)?;
    Ok(())
}

/// Build BITMAPKEYS entries. CUICatalog uses these to resolve `imagesWithName:`
/// — without them, every facet lookup returns an empty array. Each entry
/// maps a facet identifier (raw u32 key) to a 52-byte attribute-mask blob:
///   u32 version=1, u32 zero, u32 size=40, u32 attr_count=keyformat.len(),
///   then keyformat.len() × i32 masks. Attributes that vary across renditions
///   (appearance, element, part, identifier) are always -1; the rest get a
///   bitmask of the values seen across renditions sharing that identifier.
fn build_bitmapkeys(
    entries: &[(Vec<u8>, Vec<u8>)],
    keyformat: &[u16],
) -> Vec<(u32, Vec<u8>)> {
    use byteorder::WriteBytesExt;
    use std::collections::BTreeMap;
    let identifier_pos = keyformat.iter().position(|&a| a == 17);
    let mut per_ident: BTreeMap<u16, Vec<Vec<u16>>> = BTreeMap::new();
    for (key, _) in entries {
        if key.len() < keyformat.len() * 2 {
            continue;
        }
        let mut attrs = Vec::with_capacity(keyformat.len());
        for i in 0..keyformat.len() {
            let v = u16::from_le_bytes([key[i * 2], key[i * 2 + 1]]);
            attrs.push(v);
        }
        let Some(ip) = identifier_pos else { continue };
        let ident = attrs[ip];
        if ident == 0 {
            continue;
        }
        per_ident.entry(ident).or_default().push(attrs);
    }
    let mut out: Vec<(u32, Vec<u8>)> = Vec::new();
    for (ident, rows) in per_ident {
        // 4 bytes for the attr count + 4 bytes per attribute mask.
        let body_size = 4 + 4 * keyformat.len() as u32;
        let mut buf = Vec::with_capacity(16 + body_size as usize);
        buf.write_u32::<LittleEndian>(1).unwrap();
        buf.write_u32::<LittleEndian>(0).unwrap();
        buf.write_u32::<LittleEndian>(body_size).unwrap();
        buf.write_u32::<LittleEndian>(keyformat.len() as u32).unwrap();
        for &attr in keyformat {
            // Apple always emits -1 for these "variable" attrs even when
            // every rendition in the facet has the same value.
            let always_variable = matches!(attr, 7 | 1 | 2 | 17);
            if always_variable {
                buf.write_i32::<LittleEndian>(-1).unwrap();
                continue;
            }
            let attr_pos = keyformat.iter().position(|a| *a == attr).unwrap();
            let mut mask: u32 = 0;
            for row in &rows {
                let v = row[attr_pos];
                if v < 32 {
                    mask |= 1u32 << v;
                }
            }
            if mask == 0 {
                mask = 1;
            }
            buf.write_u32::<LittleEndian>(mask).unwrap();
        }
        out.push((ident as u32, buf));
    }
    out.sort_by_key(|(k, _)| *k);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pre_rendered_name_matches_apple_pattern() {
        let n = pre_rendered_name("feishin", 16, 2, "NSAppearanceNameSystem");
        assert!(n.starts_with("feishin16x16_NSAppearanceNameSystem_"));
        assert!(n.ends_with(".png"));
        // Pattern: prefix + UUID(8-4-4-4-12) + - + 5 hex + - + 16 hex + .png
        let body = n
            .trim_start_matches("feishin16x16_NSAppearanceNameSystem_")
            .trim_end_matches(".png");
        let parts: Vec<&str> = body.split('-').collect();
        assert_eq!(parts.len(), 7, "expected UUID-PID-HEX shape, got {n:?}");
    }

    #[test]
    fn pre_rendered_name_is_deterministic() {
        let a = pre_rendered_name("Icon", 32, 2, "NSAppearanceNameSystem");
        let b = pre_rendered_name("Icon", 32, 2, "NSAppearanceNameSystem");
        assert_eq!(a, b);
    }

    #[test]
    fn pre_rendered_name_differs_by_size_and_scale() {
        let a = pre_rendered_name("Icon", 32, 1, "NSAppearanceNameSystem");
        let b = pre_rendered_name("Icon", 32, 2, "NSAppearanceNameSystem");
        let c = pre_rendered_name("Icon", 64, 2, "NSAppearanceNameSystem");
        assert_ne!(a, b);
        assert_ne!(b, c);
    }

    fn palette(json: &str) -> (Vec<ColorAsset>, Vec<GradientAsset>) {
        let parsed = crate::icon_json::IconJson::parse(json).unwrap();
        fill_specializations_assets("X", &parsed)
    }

    #[test]
    fn fill_specializations_feishin_palette() {
        // Top-level custom p3 gradient + system-dark, then a layer with a
        // default solid, a dark gradient (whose 2nd stop dedups to Color-2),
        // and a tinted solid → Apple's 8 Colors / 3 Gradients.
        let (colors, gradients) = palette(
            r#"{
              "fill-specializations":[
                {"value":{"linear-gradient":[
                    "display-p3:0.87416,0.87416,0.87416,1.0",
                    "display-p3:0.99575,0.99575,0.99575,1.0"],
                  "orientation":{"start":{"x":0.5,"y":1},"stop":{"x":0.5,"y":0.3}}}},
                {"appearance":"dark","value":"system-dark"}
              ],
              "groups":[{"layers":[{"image-name":"f.svg","name":"f",
                "fill-specializations":[
                  {"value":{"solid":"extended-gray:0.0,1.0"}},
                  {"appearance":"dark","value":{"linear-gradient":[
                      "display-p3:0.78674,0.78674,0.78674,1.0",
                      "display-p3:0.87416,0.87416,0.87416,1.0"],
                    "orientation":{"start":{"x":0.5,"y":1},"stop":{"x":0.5,"y":0}}}},
                  {"appearance":"tinted","value":{"solid":"gray:1.0,1.0"}}
                ]}]}]
            }"#,
        );
        assert_eq!(colors.len(), 8, "feishin should have 8 colors");
        assert_eq!(gradients.len(), 3, "feishin should have 3 gradients");
        // Colorspaces: anchor(6), 2×p3(3), 2×gray(2), ext-gray(6), p3(3), gray(2)
        let cs: Vec<u32> = colors.iter().map(|c| c.colorspace_id).collect();
        assert_eq!(cs, vec![6, 3, 3, 2, 2, 6, 3, 2]);
        // The layer dark-gradient's second stop dedups onto Color-2.
        assert_eq!(gradients[2].stops[0].1, "X_Assets/Color-7");
        assert_eq!(gradients[2].stops[1].1, "X_Assets/Color-2");
        // Gradient-1 carries the JSON orientation (top→0.3), not the default.
        assert_eq!(gradients[0].geometry, [0.5, 1.0, 0.5, 0.3]);
    }

    #[test]
    fn fill_specializations_scrumdinger_palette() {
        // system-light + dark automatic, plus a redundant layer dark automatic
        // that dedups entirely → 5 Colors / 2 Gradients (no Gradient-3).
        let (colors, gradients) = palette(
            r#"{
              "fill-specializations":[
                {"value":"system-light"},
                {"appearance":"dark","value":"automatic"}
              ],
              "groups":[{"layers":[{"image-name":"1.png","name":"1",
                "fill-specializations":[{"appearance":"dark","value":"automatic"}]}]}]
            }"#,
        );
        assert_eq!(colors.len(), 5, "scrumdinger should have 5 colors");
        assert_eq!(gradients.len(), 2, "redundant layer automatic must dedup");
        let cs: Vec<u32> = colors.iter().map(|c| c.colorspace_id).collect();
        assert_eq!(cs, vec![6, 2, 2, 2, 2]);
    }
}

fn write_populated_partial_plist(path: &Path, icon_name: &str) -> Result<()> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)?;
        }
    }
    let body = format!(
        concat!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n",
            "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n",
            "<plist version=\"1.0\">\n",
            "<dict>\n",
            "\t<key>CFBundleIconFile</key>\n",
            "\t<string>{name}</string>\n",
            "\t<key>CFBundleIconName</key>\n",
            "\t<string>{name}</string>\n",
            "</dict>\n",
            "</plist>\n",
        ),
        name = icon_name,
    );
    fs::write(path, body)?;
    Ok(())
}

fn write_empty_partial_plist(path: &Path) -> Result<()> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)?;
        }
    }
    let body = concat!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n",
        "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n",
        "<plist version=\"1.0\">\n",
        "<dict/>\n",
        "</plist>\n",
    );
    fs::write(path, body)?;
    Ok(())
}
