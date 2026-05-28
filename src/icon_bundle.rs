//! .icon bundle support (modern macOS icon.json + source image).

use crate::bom::BomWriter;
use crate::car::{self, MultisizeImageEntry, Rendition};
use crate::catalog::load_image_as_bgra;
use crate::icns;
use crate::name_hash::hash_name;
use anyhow::Result;
use image::imageops::FilterType;
use std::fs;
use std::path::{Path, PathBuf};

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

    let icon_json: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(icon_path.join("icon.json"))?)?;
    let source_images = find_all_source_images(icon_path, &icon_json);
    if source_images.is_empty() {
        return Ok(Vec::new());
    }
    let has_svg = source_images
        .iter()
        .any(|p| p.to_string_lossy().to_lowercase().ends_with(".svg"));

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
        build_icon_car(&car_path, &icon_name, &icon_images, platform, min_deploy)?;
        output_files.push(fs::canonicalize(&car_path).unwrap_or(car_path));
        let _ = fs::remove_dir_all(&tmpdir);
    }

    if let Some(path) = info_plist_path {
        write_icon_plist(path, &icon_name, accent_color)?;
        output_files.push(fs::canonicalize(path).unwrap_or(path.to_path_buf()));
    }
    Ok(output_files)
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

    write_icon_car(car_path, icon_name, ident, &keyformat, &all_entries, platform, min_deploy)
}

fn build_icon_car(
    car_path: &Path,
    icon_name: &str,
    icon_images: &[(PathBuf, u32, u32)],
    platform: &str,
    min_deploy: &str,
) -> Result<()> {
    let ident = hash_name(icon_name);
    let keyformat: Vec<u16> = car::KEYFORMAT_ALL.to_vec();
    let mut renditions: Vec<Rendition> = Vec::new();

    for (img_path, pixel_size, scale) in icon_images {
        let (pd, w, h, pf) = load_image_as_bgra(img_path, false)?;
        let point_size = pixel_size / scale;
        let dim2 = icon_dim2(point_size);
        renditions.push(Rendition {
            name: img_path
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .to_string(),
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
            keyformat: keyformat.clone(),
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
    ms_rend.keyformat = keyformat.clone();
    renditions.push(ms_rend);

    let mut all_entries: Vec<(Vec<u8>, Vec<u8>)> = Vec::new();
    for rend in &renditions {
        let key = rend.build_rendition_key();
        let csi = rend.build_csi();
        all_entries.push((key, csi));
    }
    all_entries.sort_by(|a, b| a.0.cmp(&b.0));

    write_icon_car(car_path, icon_name, ident, &keyformat, &all_entries, platform, min_deploy)
}

fn write_icon_car(
    car_path: &Path,
    icon_name: &str,
    ident: u16,
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
    let facetkey_entries = vec![(
        icon_name.as_bytes().to_vec(),
        car::make_facetkey_value(car::ELEMENT_UNIVERSAL, Some(car::PART_ICON), ident),
    )];
    bom.add_tree("FACETKEYS", &facetkey_entries, 4096);
    bom.set_inline_key_size(Some(keyformat.len() * 2));
    bom.add_tree("RENDITIONS", all_entries, 4096);
    bom.set_inline_key_size(None);
    bom.add_raw_key_tree("BITMAPKEYS", &[], 1024);
    bom.write(car_path)?;
    Ok(())
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
