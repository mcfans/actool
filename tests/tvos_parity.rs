//! tvOS platform regression tests.
//!
//! Verifies that `--platform appletvos --app-icon ...` compiles tvOS
//! brandassets/imagestack app icons into a valid CAR with the expected
//! header/keyformat and partial plist.

use actool::compiler;
use actool::car;
use std::path::{Path, PathBuf};

fn workspace_tmp(name: &str) -> PathBuf {
    let dir = PathBuf::from("tmp").join(name);
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn write_png(path: &Path, w: u32, h: u32, rgba: [u8; 4]) {
    let img = image::RgbaImage::from_pixel(w, h, image::Rgba(rgba));
    img.save(path).unwrap();
}

/// Build a minimal tvOS brandassets fixture with a two-layer imagestack.
fn build_tvos_brandassets(root: &Path) {
    let xc = root.join("Assets.xcassets");
    let brand = xc.join("AppIcon.brandassets");
    let stack = brand.join("App Icon.imagestack");
    let front = stack.join("Front.imagestacklayer");
    let back = stack.join("Back.imagestacklayer");
    std::fs::create_dir_all(&front.join("Content.imageset")).unwrap();
    std::fs::create_dir_all(&back.join("Content.imageset")).unwrap();

    std::fs::write(
        xc.join("Contents.json"),
        r#"{"info":{"author":"xcode","version":1}}"#,
    )
    .unwrap();

    std::fs::write(
        brand.join("Contents.json"),
        r#"{
          "assets":[{"size":"400x240","idiom":"tv","filename":"App Icon.imagestack","role":"primary-app-icon"}],
          "info":{"author":"xcode","version":1}
        }"#,
    )
    .unwrap();

    std::fs::write(
        stack.join("Contents.json"),
        r#"{
          "layers":[{"filename":"Back.imagestacklayer"},{"filename":"Front.imagestacklayer"}],
          "info":{"author":"xcode","version":1}
        }"#,
    )
    .unwrap();

    for (layer, color) in [(&back, [255, 0, 0, 255]), (&front, [0, 255, 0, 255])] {
        std::fs::write(
            layer.join("Contents.json"),
            r#"{"info":{"author":"xcode","version":1}}"#,
        )
        .unwrap();
        write_png(
            &layer.join("Content.imageset").join("Layer@1x.png"),
            400,
            240,
            color,
        );
        write_png(
            &layer.join("Content.imageset").join("Layer@2x.png"),
            800,
            480,
            color,
        );
        std::fs::write(
            layer.join("Content.imageset").join("Contents.json"),
            r#"{
              "images":[
                {"size":"400x240","idiom":"tv","filename":"Layer@1x.png","scale":"1x"},
                {"size":"400x240","idiom":"tv","filename":"Layer@2x.png","scale":"2x"}
              ],
              "info":{"author":"xcode","version":1}
            }"#,
        )
        .unwrap();
    }
}

fn compile_tvos(xcassets: &Path, out: &Path) {
    let plist = out.join("partial.plist");
    compiler::compile_catalog(
        xcassets,
        out,
        "appletvos",
        "17.0",
        Some("AppIcon"),
        Some(&plist),
        None,
        None,
        "default",
        None,
        None,
        true,
    )
    .expect("compile");
}

fn read_u32_le(buf: &[u8], off: usize) -> u32 {
    u32::from_le_bytes(buf[off..off + 4].try_into().unwrap())
}

fn keyformat(car: &[u8]) -> Vec<u32> {
    let i = car.windows(4).position(|w| w == b"tmfk").expect("tmfk block");
    let n = read_u32_le(car, i + 8) as usize;
    (0..n)
        .map(|k| read_u32_le(car, i + 12 + 4 * k))
        .collect()
}

#[test]
fn tvos_brandassets_compiles_with_atv_header() {
    let root = workspace_tmp("tvos_brandassets_header");
    build_tvos_brandassets(&root);
    let out = root.join("out");
    std::fs::create_dir_all(&out).unwrap();
    compile_tvos(&root.join("Assets.xcassets"), &out);

    let car = std::fs::read(out.join("Assets.car")).expect("car");

    // tvOS uses the iOS-style 8-column key format without Dimension1/2 extras.
    assert_eq!(keyformat(&car), vec![7, 13, 12, 15, 16, 17, 1, 2]);

    // CARHEADER: CoreUI 972, key-semantics 1.
    let h = car.windows(4).position(|w| w == b"RATC").expect("RATC block");
    assert_eq!(read_u32_le(&car, h + 4), 972, "tvOS CoreUI version");
    assert_eq!(read_u32_le(&car, h + 432), 1, "tvOS key semantics");

    // EXTENDED_METADATA platform is "atv", not "appletvos".
    let m = car.windows(4).position(|w| w == b"META").expect("META block");
    let platform: String = car[m + 516..m + 516 + 8]
        .iter()
        .take_while(|b| **b != 0)
        .map(|b| *b as char)
        .collect();
    assert_eq!(platform, "atv");

    // Partial plist has CFBundleIcons with the icon name as a string;
    // tvOS does not emit CFBundleIcons~ipad.
    let plist = std::fs::read_to_string(out.join("partial.plist")).unwrap();
    assert!(plist.contains("<key>CFBundleIcons</key>"));
    assert!(plist.contains("<key>CFBundlePrimaryIcon</key>"));
    assert!(!plist.contains("<key>CFBundleIcons~ipad</key>"));
}

#[test]
fn tvos_brandassets_emits_flattened_and_layer_renditions() {
    let root = workspace_tmp("tvos_brandassets_renditions");
    build_tvos_brandassets(&root);
    let out = root.join("out");
    std::fs::create_dir_all(&out).unwrap();
    compile_tvos(&root.join("Assets.xcassets"), &out);

    let car = std::fs::read(out.join("Assets.car")).expect("car");
    let kf = keyformat(&car);
    let scale_col = kf.iter().position(|t| *t == 12).unwrap();
    let idiom_col = kf.iter().position(|t| *t == 15).unwrap();
    let part_col = kf.iter().position(|t| *t == 2).unwrap();

    let mut flattened_scales = std::collections::HashSet::new();
    let mut radiosity_scales = std::collections::HashSet::new();
    let mut layer_count = 0;
    for i in (0..car.len().saturating_sub(kf.len() * 2)).step_by(2) {
        let c: Vec<u16> = (0..kf.len())
            .map(|x| u16::from_le_bytes(car[i + x * 2..i + x * 2 + 2].try_into().unwrap()))
            .collect();
        if c[0] == 0
            && c[1] == 0
            && c[idiom_col] == 3
            && (c[part_col] == car::PART_TVOS_FLATTENED
                || c[part_col] == car::PART_TVOS_RADIOSITY
                || c[part_col] == car::PART_REGULAR)
        {
            if c[part_col] == car::PART_TVOS_FLATTENED
                && (c[scale_col] == 1 || c[scale_col] == 2)
            {
                flattened_scales.insert(c[scale_col]);
            }
            if c[part_col] == car::PART_TVOS_RADIOSITY
                && (c[scale_col] == 1 || c[scale_col] == 2)
            {
                radiosity_scales.insert(c[scale_col]);
            }
            if c[part_col] == car::PART_REGULAR {
                layer_count += 1;
            }
        }
    }
    assert!(
        flattened_scales.len() >= 1,
        "expected at least one tv-idiom flattened rendition"
    );
    assert!(
        radiosity_scales.len() >= 1,
        "expected at least one tv-idiom radiosity rendition"
    );
    assert!(layer_count >= 2, "expected at least two tv-idiom layer renditions");
    assert!(flattened_scales.contains(&1), "expected 1x flattened rendition");
    assert!(flattened_scales.contains(&2), "expected 2x flattened rendition");
    assert!(radiosity_scales.contains(&1), "expected 1x radiosity rendition");
    assert!(radiosity_scales.contains(&2), "expected 2x radiosity rendition");

    // tvOS brand assets also emit pre-blurred radiosity images at each scale.
    assert!(
        car.windows("ZZZZRadiosityImage-1.0.0".len())
            .any(|w| w == b"ZZZZRadiosityImage-1.0.0"),
        "expected 1x radiosity rendition name"
    );
    assert!(
        car.windows("ZZZZRadiosityImage-2.0.0".len())
            .any(|w| w == b"ZZZZRadiosityImage-2.0.0"),
        "expected 2x radiosity rendition name"
    );
}
