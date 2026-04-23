//! Asset catalog compiler.
//!
//! Orchestrates the compilation of xcassets into Assets.car, .icns, and
//! the partial info plist.

use crate::bom::BomWriter;
use crate::car::{self, RenditionKeyParts};
use crate::catalog::{AssetCatalog, Facet};
use crate::icns;
use crate::packer::{self, PackedImage};
use anyhow::Result;
use byteorder::{LittleEndian, WriteBytesExt};
use indexmap::IndexMap;
use std::fs;
use std::path::{Path, PathBuf};

#[allow(clippy::too_many_arguments)]
pub fn compile_catalog(
    xcassets_path: &Path,
    output_dir: &Path,
    platform: &str,
    min_deploy: &str,
    app_icon: Option<&str>,
    info_plist_path: Option<&Path>,
    accent_color: Option<&str>,
    widget_background_color: Option<&str>,
    standalone_icon_behavior: &str,
    include_languages: Option<Vec<String>>,
    development_region: Option<String>,
    plist_localizations: bool,
) -> Result<Vec<PathBuf>> {
    fs::create_dir_all(output_dir)?;
    let has_icon = app_icon.is_some();

    let mut catalog = AssetCatalog::new(
        xcassets_path.to_path_buf(),
        platform.to_string(),
        min_deploy.to_string(),
        app_icon.map(|s| s.to_string()),
        include_languages,
        development_region,
    );
    let (mut renditions, facets) = catalog.parse()?;

    let deploy_ver: (u32, u32) = {
        let mut parts = min_deploy.split('.');
        let a: u32 = parts.next().and_then(|s| s.parse().ok()).unwrap_or(0);
        let b: u32 = parts.next().and_then(|s| s.parse().ok()).unwrap_or(0);
        (a, b)
    };
    let min_pack_ver = car::min_pack_version(platform);
    let (mut pack_groups, inline_indices) = if deploy_ver >= min_pack_ver {
        packer::group_for_packing(&renditions)
    } else {
        (Vec::new(), (0..renditions.len()).collect())
    };

    // Compute dynamic keyformat: include dim1 when atlases exceed distinct scales.
    let mut trial_atlas_count = 0;
    let mut trial_scales = std::collections::HashSet::new();
    for (_fmt, scale, idxs) in &pack_groups {
        trial_scales.insert(*scale);
        let imgs: Vec<PackedImage> = idxs
            .iter()
            .map(|i| {
                let r = &renditions[*i];
                let mut pi = PackedImage::new(
                    r.name.clone(),
                    r.identifier as u32,
                    r.width,
                    r.height,
                );
                pi.pixel_format = r.pixel_format;
                pi.scale = r.scale as u32;
                pi.part = r.part as u32;
                pi.dim2 = r.dim2 as u32;
                pi
            })
            .collect();
        trial_atlas_count += packer::pack_images_split(imgs, 262, 196).len();
    }
    let uses_dim1 = trial_atlas_count > trial_scales.len();
    let keyformat = car::compute_keyformat(&renditions, uses_dim1);

    for rend in &mut renditions {
        rend.has_icon = has_icon;
        rend.keyformat = keyformat.clone();
        rend.min_deploy = min_deploy.to_string();
        rend.platform = platform.to_string();
    }

    let mut all_entries: Vec<(Vec<u8>, Vec<u8>)> = Vec::new();
    let mut dim1_by_scale: IndexMap<u16, u16> = IndexMap::new();

    // Sort pack groups: (scale ascending, then GA8 before BGRA) matching Python.
    // In Python: key=lambda g: (g[1], 0 if g[0] == b"BGRA" else 1), but
    // b"BGRA" (0x42) comes after b" 8AG" (0x20) lexically. The Python comment
    // says "BGRA sorts after GA8, so use reverse fmt order" — the lambda puts
    // BGRA first (0) then GA8 (1). That matches: BGRA before GA8 within a scale.
    pack_groups.sort_by_key(|(fmt, scale, _)| (*scale, if fmt == b"BGRA" { 0 } else { 1 }));

    for (fmt, scale, idxs) in &pack_groups {
        let sprite_atlas_id =
            idxs.first().map(|i| renditions[*i].sprite_atlas_id).unwrap_or(0);

        let packed_imgs: Vec<PackedImage> = idxs
            .iter()
            .map(|i| {
                let r = &renditions[*i];
                let intent = if r.template_rendering_intent < 0 {
                    if r.is_template {
                        2
                    } else {
                        4
                    }
                } else {
                    r.template_rendering_intent
                };
                PackedImage {
                    name: r.name.clone(),
                    identifier: r.identifier as u32,
                    width: r.width,
                    height: r.height,
                    x: 0,
                    y: 0,
                    pixel_data: r.pixel_data.clone(),
                    pixel_format: r.pixel_format,
                    scale: r.scale as u32,
                    is_template: r.is_template,
                    template_rendering_intent: intent,
                    part: r.part as u32,
                    dim2: r.dim2 as u32,
                    appearance: r.appearance as u32,
                    direction: r.direction as u32,
                }
            })
            .collect();

        let mut atlases = packer::pack_images_split(packed_imgs, 262, 196);
        for atlas in &mut atlases {
            let dim1_counter = *dim1_by_scale.get(scale).unwrap_or(&0);
            atlas.dim1 = dim1_counter as u32;
            atlas.render();

            let all_icons = atlas.images.iter().all(|i| i.part == car::PART_ICON as u32);
            let force_lzfse = fmt == b"BGRA" && all_icons;

            let atlas_name = if sprite_atlas_id != 0 {
                atlas.name().replace("ZZZZPackedAsset", "ZZZZExplicitlyPackedAsset")
            } else {
                atlas.name()
            };

            let atlas_key = car::make_rendition_key(
                RenditionKeyParts {
                    element: car::ELEMENT_PACKED,
                    part: car::PART_REGULAR,
                    identifier: sprite_atlas_id,
                    dim1: dim1_counter,
                    scale: *scale,
                    ..Default::default()
                },
                &keyformat,
            );

            let atlas_csi = car::build_packed_asset_csi(
                &atlas_name,
                atlas.width,
                atlas.height,
                *scale,
                fmt,
                &atlas.pixel_data,
                min_deploy,
                platform,
                force_lzfse,
            );
            all_entries.push((atlas_key, atlas_csi));

            for img in &atlas.images {
                let ref_key = car::make_rendition_key(
                    RenditionKeyParts {
                        element: car::ELEMENT_UNIVERSAL,
                        part: img.part as u16,
                        identifier: img.identifier as u16,
                        dim2: img.dim2 as u16,
                        appearance: img.appearance as u16,
                        direction: img.direction as u16,
                        scale: *scale,
                        ..Default::default()
                    },
                    &keyformat,
                );
                let inlk_y = atlas.height - img.y - img.height;
                let ref_csi = car::build_packed_image_csi(
                    &img.name,
                    img.width,
                    img.height,
                    *scale,
                    fmt,
                    img.x,
                    inlk_y,
                    sprite_atlas_id,
                    dim1_counter,
                    (img.template_rendering_intent as u32) << 2,
                );
                all_entries.push((ref_key, ref_csi));
            }
            dim1_by_scale.insert(*scale, dim1_counter + 1);
        }
    }

    // Sprite atlas metadata renditions
    let mut atlas_sprites: IndexMap<u16, Vec<String>> = IndexMap::new();
    for (name, facet) in &facets {
        for rend in &renditions {
            if rend.sprite_atlas_id != 0 && rend.identifier == facet.identifier {
                let entry = atlas_sprites.entry(rend.sprite_atlas_id).or_default();
                if !entry.contains(name) {
                    entry.push(name.clone());
                }
                break;
            }
        }
    }
    for (atlas_id, mut sprite_names) in atlas_sprites {
        let meta_key = car::make_rendition_key(
            RenditionKeyParts {
                element: car::ELEMENT_PACKED,
                part: car::PART_SPRITE_ATLAS,
                identifier: atlas_id,
                scale: 1,
                ..Default::default()
            },
            &keyformat,
        );
        if all_entries.iter().any(|(k, _)| k == &meta_key) {
            continue;
        }
        sprite_names.sort();
        let meta_csi = car::build_sprite_atlas_metadata_csi(&sprite_names);
        all_entries.push((meta_key, meta_csi));
    }

    // Inline renditions
    for i in inline_indices {
        let r = &renditions[i];
        let key = r.build_rendition_key();
        let csi = r.build_csi();
        all_entries.push((key, csi));
    }

    all_entries.sort_by(|a, b| a.0.cmp(&b.0));

    let mut bom = BomWriter::new();
    bom.add_named_block("CARHEADER", car::make_carheader(all_entries.len() as u32));
    bom.add_tree("RENDITIONS", &all_entries, 4096);

    let mut facetkey_entries: Vec<(Vec<u8>, Vec<u8>)> = Vec::new();
    let mut facet_names: Vec<_> = facets.keys().cloned().collect();
    facet_names.sort();
    for name in &facet_names {
        let f = &facets[name];
        let key_data = name.as_bytes().to_vec();
        let value_data = car::make_facetkey_value(f.element, f.part, f.identifier);
        facetkey_entries.push((key_data, value_data));
    }
    bom.add_tree("FACETKEYS", &facetkey_entries, 4096);

    let has_appearances = renditions.iter().any(|r| r.appearance != 0);
    if has_appearances {
        let mut ap_entries: Vec<(Vec<u8>, Vec<u8>)> = Vec::new();
        let mut v1 = Vec::new();
        v1.write_u16::<LittleEndian>(1).unwrap();
        ap_entries.push((b"NSAppearanceNameDarkAqua".to_vec(), v1));
        let mut v0 = Vec::new();
        v0.write_u16::<LittleEndian>(0).unwrap();
        ap_entries.push((b"NSAppearanceNameSystem".to_vec(), v0));
        bom.add_tree("APPEARANCEKEYS", &ap_entries, 4096);
    }

    bom.add_named_block("KEYFORMAT", car::make_keyformat(&keyformat));
    bom.add_named_block(
        "EXTENDED_METADATA",
        car::make_extended_metadata(platform, min_deploy),
    );

    let bitmap_entries = build_bitmapkeys(&facets, &all_entries, &keyformat);
    bom.add_raw_key_tree("BITMAPKEYS", &bitmap_entries, 1024);

    let produce_car = !all_entries.is_empty();
    let car_path = output_dir.join("Assets.car");
    if produce_car {
        bom.write(&car_path)?;
    }

    let mut output_files: Vec<PathBuf> = Vec::new();

    // Loose JPEG files for deployment targets below the per-platform
    // JPEG-in-CAR threshold. Named after the imageset stem with the
    // source file's extension preserved (matches host actool).
    for (imageset_stem, src) in &catalog.loose_jpegs {
        let ext = src
            .extension()
            .and_then(|s| s.to_str())
            .unwrap_or("jpg");
        let dest = output_dir.join(format!("{imageset_stem}.{ext}"));
        fs::copy(src, &dest)?;
        output_files.push(fs::canonicalize(&dest).unwrap_or(dest));
    }

    if let Some(icon_name) = app_icon {
        if standalone_icon_behavior != "none" {
            let icons = catalog.get_icon_images()?;
            if !icons.is_empty() {
                let icns_path = output_dir.join(format!("{icon_name}.icns"));
                icns::create_icns(&icons, &icns_path)?;
                output_files.push(fs::canonicalize(&icns_path).unwrap_or(icns_path));
            }
        }
    }

    if produce_car {
        output_files.push(fs::canonicalize(&car_path).unwrap_or(car_path));
    }

    if let Some(path) = info_plist_path {
        let locales = if plist_localizations {
            catalog.get_locales_used()
        } else {
            Vec::new()
        };
        write_info_plist(
            path,
            app_icon,
            accent_color,
            widget_background_color,
            &locales,
        )?;
        output_files.push(fs::canonicalize(path).unwrap_or(path.to_path_buf()));
    }

    Ok(output_files)
}

fn build_bitmapkeys(
    facets: &IndexMap<String, Facet>,
    rendition_entries: &[(Vec<u8>, Vec<u8>)],
    keyformat: &[u16],
) -> Vec<(u32, Vec<u8>)> {
    let wildcard_attrs: std::collections::HashSet<u16> = [1, 2, 17].into_iter().collect();

    let mut id_keys: IndexMap<u16, Vec<Vec<u16>>> = IndexMap::new();
    let id_pos = keyformat.iter().position(|t| *t == 17);
    for (key_data, _csi) in rendition_entries {
        let n_vals = key_data.len() / 2;
        let vals: Vec<u16> = (0..n_vals)
            .map(|i| u16::from_le_bytes(key_data[i * 2..i * 2 + 2].try_into().unwrap()))
            .collect();
        if let Some(p) = id_pos {
            if p < vals.len() {
                let ident = vals[p];
                id_keys.entry(ident).or_default().push(vals);
            }
        }
    }

    let mut entries: Vec<(u32, Vec<u8>)> = Vec::new();
    let mut sorted_facets: Vec<(&String, &Facet)> = facets.iter().collect();
    sorted_facets.sort_by_key(|(_, f)| f.identifier);
    for (_name, facet) in sorted_facets {
        if facet.identifier == 0 {
            continue;
        }
        let mut attr_masks: Vec<u32> = Vec::with_capacity(keyformat.len());
        let keys_for_id = id_keys.get(&facet.identifier).cloned().unwrap_or_default();
        for (i, attr_id) in keyformat.iter().enumerate() {
            if wildcard_attrs.contains(attr_id) {
                attr_masks.push(0xFFFFFFFF);
            } else {
                let mut bitmask: u32 = 0;
                for key_vals in &keys_for_id {
                    if i < key_vals.len() {
                        let v = key_vals[i];
                        if v < 32 {
                            bitmask |= 1u32 << v;
                        }
                    }
                }
                attr_masks.push(if bitmask == 0 { 1 } else { bitmask });
            }
        }

        let n_attrs = keyformat.len() as u32;
        let data_size = 4 + n_attrs * 4;
        let mut value = Vec::new();
        value.write_u32::<LittleEndian>(1).unwrap();
        value.write_u32::<LittleEndian>(0).unwrap();
        value.write_u32::<LittleEndian>(data_size).unwrap();
        value.write_u32::<LittleEndian>(n_attrs).unwrap();
        for mask in attr_masks {
            value.write_u32::<LittleEndian>(mask).unwrap();
        }
        entries.push((facet.identifier as u32, value));
    }
    entries
}

fn write_info_plist(
    path: &Path,
    app_icon: Option<&str>,
    accent_color: Option<&str>,
    widget_background_color: Option<&str>,
    localizations: &[String],
) -> Result<()> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)?;
        }
    }
    let mut lines = vec![
        r#"<?xml version="1.0" encoding="UTF-8"?>"#.to_string(),
        r#"<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">"#.to_string(),
        r#"<plist version="1.0">"#.to_string(),
        "<dict>".to_string(),
    ];
    if let Some(name) = app_icon {
        lines.push("\t<key>CFBundleIconFile</key>".to_string());
        lines.push(format!("\t<string>{name}</string>"));
        lines.push("\t<key>CFBundleIconName</key>".to_string());
        lines.push(format!("\t<string>{name}</string>"));
    }
    if let Some(n) = accent_color {
        lines.push("\t<key>NSAccentColorName</key>".to_string());
        lines.push(format!("\t<string>{n}</string>"));
    }
    if let Some(n) = widget_background_color {
        lines.push("\t<key>NSWidgetBackgroundColorName</key>".to_string());
        lines.push(format!("\t<string>{n}</string>"));
    }
    if !localizations.is_empty() {
        lines.push("\t<key>CFBundleLocalizations</key>".to_string());
        lines.push("\t<array>".to_string());
        for loc in localizations {
            lines.push(format!("\t\t<string>{loc}</string>"));
        }
        lines.push("\t</array>".to_string());
    }
    lines.push("</dict>".to_string());
    lines.push("</plist>".to_string());
    lines.push(String::new());
    fs::write(path, lines.join("\n"))?;
    Ok(())
}
