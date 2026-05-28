//! .icon bundle support (modern macOS icon.json + source image).

use crate::bom::BomWriter;
use crate::car::{self, MultisizeImageEntry, Rendition};
use crate::catalog::load_image_as_bgra;
use crate::icns;
use crate::icon_json::IconJson;
use crate::name_hash::hash_name;
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

const MACOS_ICON_SIZES: &[(u32, u32)] = &[
    (16, 1),
    (16, 2),
    (32, 1),
    (32, 2),
    (128, 1),
    (128, 2),
    (256, 1),
    (256, 2),
    (512, 1),
    (512, 2),
];

fn icon_dim2(point_size: u32) -> u16 {
    match point_size {
        16 => 1,
        32 => 2,
        128 => 3,
        256 => 4,
        512 => 5,
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
    let icon_name = app_icon.map(|s| s.to_string()).unwrap_or(bundle_stem);

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
        if standalone_icon_behavior != "none" {
            let icns_path = output_dir.join(format!("{icon_name}.icns"));
            icns::create_icns(&icon_images, &icns_path)?;
            if icns_path.exists() {
                output_files.push(fs::canonicalize(&icns_path).unwrap_or(icns_path));
            }
        }
        let car_path = output_dir.join("Assets.car");
        build_icon_car(&car_path, &icon_name, &icon_images, &layer_assets, platform, min_deploy)?;
        output_files.push(fs::canonicalize(&car_path).unwrap_or(car_path));
        let _ = fs::remove_dir_all(&tmpdir);
    }

    if let Some(path) = info_plist_path {
        write_icon_plist(path, &icon_name, accent_color)?;
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

fn build_icon_car(
    car_path: &Path,
    icon_name: &str,
    icon_images: &[(PathBuf, u32, u32)],
    layer_assets: &[LayerAsset],
    platform: &str,
    min_deploy: &str,
) -> Result<()> {
    let ident = hash_name(icon_name);
    // The actual keyformat is computed from the renditions below — only the
    // attributes they exercise survive. .icon catalogs typically end up with
    // [7, 13, 1, 2, 3, 17, 9, 11, 12] (no direction, no dim1).
    let placeholder_kf: Vec<u16> = Vec::new();
    let mut renditions: Vec<Rendition> = Vec::new();

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
            ..Rendition::default()
        });
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
        Some(car::PART_ICON),
        ident,
    )];
    for asset in layer_assets {
        let asset_ident = hash_name(&asset.facet_name);
        let (pd, w, h, pf) = load_image_as_bgra(&asset.source_path, false)?;
        renditions.push(Rendition {
            name: asset
                .source_path
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .to_string(),
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
            ..Rendition::default()
        });
        facets.push((
            asset.facet_name.clone(),
            car::ELEMENT_UNIVERSAL,
            Some(car::PART_REGULAR),
            asset_ident,
        ));
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

fn write_icon_car(
    car_path: &Path,
    facets: &[FacetEntry],
    keyformat: &[u16],
    all_entries: &[(Vec<u8>, Vec<u8>)],
    platform: &str,
    min_deploy: &str,
) -> Result<()> {
    let mut bom = BomWriter::new();
    bom.add_named_block("CARHEADER", car::make_carheader(all_entries.len() as u32));
    bom.add_named_block("KEYFORMAT", car::make_keyformat(keyformat));
    bom.add_named_block(
        "EXTENDED_METADATA",
        car::make_extended_metadata(platform, min_deploy),
    );
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
    bom.set_inline_key_size(Some(keyformat.len() * 2));
    bom.add_tree("RENDITIONS", all_entries, 4096);
    bom.set_inline_key_size(None);
    bom.add_raw_key_tree("BITMAPKEYS", &[], 1024);
    bom.write(car_path)?;
    Ok(())
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

fn write_icon_plist(
    path: &Path,
    icon_name: &str,
    _accent_color: Option<&str>,
) -> Result<()> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)?;
        }
    }
    let lines = vec![
        r#"<?xml version="1.0" encoding="UTF-8"?>"#.to_string(),
        r#"<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">"#.to_string(),
        r#"<plist version="1.0">"#.to_string(),
        "<dict>".to_string(),
        "\t<key>CFBundleIconFile</key>".to_string(),
        format!("\t<string>{icon_name}</string>"),
        "\t<key>CFBundleIconName</key>".to_string(),
        format!("\t<string>{icon_name}</string>"),
        "</dict>".to_string(),
        "</plist>".to_string(),
        String::new(),
    ];
    fs::write(path, lines.join("\n"))?;
    Ok(())
}
