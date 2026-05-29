//! .icon bundle support (modern macOS icon.json + source image).

use crate::bom::BomWriter;
use crate::car::{self, MultisizeImageEntry, Rendition};
use crate::catalog::load_image_as_bgra;
use crate::icon_json::{Fill, IconJson};
use crate::name_hash::hash_name;
use byteorder::LittleEndian;
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
    format!(
        "icon{point_size}x{point_size}_{appearance_name}_{uuid}-{pid}-{hex}.png"
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
    path.extension().and_then(|s| s.to_str()) == Some("icon")
        && path.join("icon.json").exists()
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
    let icon_json_text = fs::read_to_string(&icon_json_path)?;
    let icon_json_value: serde_json::Value = serde_json::from_str(&icon_json_text)?;
    let parsed: IconJson = IconJson::parse(&icon_json_text)?;
    let source_images = find_all_source_images(icon_path, &icon_json_value);
    if source_images.is_empty() {
        return Ok(Vec::new());
    }
    let has_svg = source_images
        .iter()
        .any(|p| p.to_string_lossy().to_lowercase().ends_with(".svg"));
    let facet_prefix = bundle_facet_prefix(icon_path);
    let layer_assets = collect_layer_assets(icon_path, &parsed, &facet_prefix);
    let group_facet_names: Vec<String> = parsed
        .groups
        .iter()
        .filter_map(|g| g.name.as_ref().map(|n| format!("{facet_prefix}/{n}")))
        .collect();
    let (color_assets, gradient_assets) = if fill_is_automatic(parsed.fill.as_ref()) {
        automatic_fill_assets(&facet_prefix)
    } else {
        (Vec::new(), Vec::new())
    };

    let mut output_files: Vec<PathBuf> = Vec::new();

    if has_svg {
        let car_path = output_dir.join("Assets.car");
        build_svg_icon_car(&car_path, &icon_name, &source_images, platform, min_deploy)?;
        output_files.push(fs::canonicalize(&car_path).unwrap_or(car_path));
    } else {
        let src_img = image::open(&source_images[0])?.to_rgba8();
        let tmpdir = std::env::temp_dir().join(format!("actool_icon_{}", std::process::id()));
        fs::create_dir_all(&tmpdir)?;
        let mut icon_images: Vec<(PathBuf, u32, u32)> = Vec::new();
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
        let car_path = output_dir.join("Assets.car");
        build_icon_car(
            &car_path,
            &facet_prefix,
            &icon_images,
            &layer_assets,
            &group_facet_names,
            &color_assets,
            &gradient_assets,
            platform,
            min_deploy,
        )?;
        output_files.push(fs::canonicalize(&car_path).unwrap_or(car_path));
        let _ = fs::remove_dir_all(&tmpdir);
    }
    // For .icon bundles Apple's actool never emits a standalone .icns
    // regardless of --standalone-icon-behavior (default/all) — the catalog
    // already encodes every appearance + size.
    let _ = standalone_icon_behavior;
    let _ = accent_color;

    // Apple writes an EMPTY plist (`<dict/>`) for .icon bundles; the
    // CFBundleIconFile/CFBundleIconName fields belong to legacy icon-set
    // workflows, not the new IconComposer format.
    if let Some(path) = info_plist_path {
        write_empty_partial_plist(path)?;
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
        let Some(layer_name) = layer.name.as_deref() else {
            continue;
        };
        // Skip SVGs here; the .icon SVG path emits them via build_svg_icon_car.
        if image_name.to_lowercase().ends_with(".svg") {
            continue;
        }
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
        let facet_name = format!("{facet_prefix}_Assets/{layer_name}");
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

fn build_svg_icon_car(
    car_path: &Path,
    icon_name: &str,
    svg_paths: &[PathBuf],
    platform: &str,
    min_deploy: &str,
) -> Result<()> {
    let ident = hash_name(icon_name);
    let keyformat: Vec<u16> = car::KEYFORMAT_ALL.to_vec();

    let mut all_entries: Vec<(Vec<u8>, Vec<u8>)> = Vec::new();
    for (layer_idx, svg_path) in svg_paths.iter().enumerate() {
        let filename = svg_path.file_name().unwrap_or_default().to_string_lossy().to_string();
        let svg_data = fs::read(svg_path)?;
        let csi = car::build_svg_csi(&filename, &svg_data);
        let mut rend = Rendition {
            name: filename.clone(),
            identifier: ident,
            element: car::ELEMENT_UNIVERSAL,
            part: car::PART_ICON,
            scale: 1,
            dim2: (layer_idx as u16) + 1,
            layout: car::LAYOUT_PDF,
            pixel_format: *car::PIXELFMT_SVG,
            keyformat: keyformat.clone(),
            min_deploy: min_deploy.to_string(),
            platform: platform.to_string(),
            ..Rendition::default()
        };
        rend.csi_override = Some(csi);
        let key = rend.build_rendition_key();
        let csi = rend.build_csi();
        all_entries.push((key, csi));
    }
    all_entries.sort_by(|a, b| a.0.cmp(&b.0));

    let facets = vec![(
        icon_name.to_string(),
        car::ELEMENT_UNIVERSAL,
        Some(car::PART_ICON),
        ident,
    )];
    write_icon_car(car_path, &facets, &keyformat, &all_entries, platform, min_deploy)
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
    platform: &str,
    min_deploy: &str,
) -> Result<()> {
    let ident = hash_name(icon_name);
    // The actual keyformat is computed from the renditions below — only the
    // attributes they exercise survive. .icon catalogs typically end up with
    // [7, 13, 1, 2, 3, 17, 9, 11, 12] (no direction, no dim1).
    let placeholder_kf: Vec<u16> = Vec::new();
    let mut renditions: Vec<Rendition> = Vec::new();

    // Split images into atlas candidates (small sizes) and inline (large).
    // For each size load BGRA pixels, then dispatch by point size.
    let mut packed_imgs: Vec<crate::packer::PackedImage> = Vec::new();
    let mut packed_meta: Vec<(String, u32, u32, u32, u16)> = Vec::new();
    for (img_path, pixel_size, scale) in icon_images {
        let (pd, w, h, pf) = load_image_as_bgra(img_path, false)?;
        let point_size = pixel_size / scale;
        let dim2 = icon_dim2(point_size);
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
            packed_imgs.push(pi);
            packed_meta.push((name, *pixel_size, *scale, point_size as u32, dim2));
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
                keyformat: placeholder_kf.clone(),
                min_deploy: min_deploy.to_string(),
                platform: platform.to_string(),
                colorspace_id: car::colorspace_for_pixel_format(&pf),
                // Apple uses bitmapEncoding=0 (original) for the pre-rendered
                // sized PNGs in .icon catalogs; the default (-1 → auto/4)
                // sets the rendition_flags bit that makes CUICatalog look
                // for a template variant that doesn't exist.
                template_rendering_intent: 0,
                ..Rendition::default()
            });
        }
    }
    drop(packed_meta);

    // Pack 16/32/64 into one ZZZZPackedAsset atlas. Emit a packed_atlas
    // rendition for the atlas image and one packed_ref rendition per source
    // image referencing into the atlas via the INLK TLV.
    if !packed_imgs.is_empty() {
        let pf = packed_imgs[0].pixel_format;
        let mut atlases = crate::packer::pack_images_split(packed_imgs, 262, 196);
        for atlas in &mut atlases {
            atlas.render();
            let atlas_name = atlas.name();
            // Apple uses LZFSE (not deepmap2) for atlases whose images are
            // all icons in BGRA — both conditions hold here.
            let force_lzfse = &pf == b"BGRA";
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

    let mut facets: Vec<FacetEntry> = vec![(
        icon_name.to_string(),
        car::ELEMENT_UNIVERSAL,
        Some(car::PART_ICON_COMPOSER),
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
        let csi = car::build_icon_gradient_csi(&grad.facet_name, grad.geometry, &stops);
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

/// A linear gradient asset extracted from icon.json `fill` specs. Stops
/// reference Color assets by facet name (e.g. "icon_Assets/Color-2").
struct GradientAsset {
    facet_name: String,
    geometry: [f32; 4],
    stops: Vec<(f32, String)>,
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
        },
        GradientAsset {
            facet_name: n("Gradient-2"),
            geometry: [0.5, 0.0, 0.5, 1.0],
            stops: vec![(0.0, n("Color-4")), (1.0, n("Color-5"))],
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
        let mut buf = Vec::with_capacity(52);
        buf.write_u32::<LittleEndian>(1).unwrap();
        buf.write_u32::<LittleEndian>(0).unwrap();
        buf.write_u32::<LittleEndian>(40).unwrap();
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
        let n = pre_rendered_name("Icon", 16, 2, "NSAppearanceNameSystem");
        assert!(n.starts_with("icon16x16_NSAppearanceNameSystem_"));
        assert!(n.ends_with(".png"));
        // Pattern: prefix + UUID(8-4-4-4-12) + - + 5 hex + - + 16 hex + .png
        let body = n
            .trim_start_matches("icon16x16_NSAppearanceNameSystem_")
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
