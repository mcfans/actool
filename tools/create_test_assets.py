"""Create test xcassets with colorset, dataset, imagestack, and spriteatlas."""

import json
import os
from PIL import Image


def create_catalog():
    base = "test_extra/Extra.xcassets"
    os.makedirs(base, exist_ok=True)

    # Root Contents.json
    with open(f"{base}/Contents.json", "w") as f:
        json.dump({"info": {"author": "xcode", "version": 1}}, f, indent=2)

    # === ColorSet: BrandRed ===
    cs_dir = f"{base}/BrandRed.colorset"
    os.makedirs(cs_dir, exist_ok=True)
    with open(f"{cs_dir}/Contents.json", "w") as f:
        json.dump({
            "colors": [
                {
                    "color": {
                        "color-space": "srgb",
                        "components": {
                            "red": "0.918",
                            "green": "0.204",
                            "blue": "0.137",
                            "alpha": "1.000"
                        }
                    },
                    "idiom": "universal"
                },
                {
                    "appearances": [
                        {"appearance": "luminosity", "value": "dark"}
                    ],
                    "color": {
                        "color-space": "srgb",
                        "components": {
                            "red": "1.000",
                            "green": "0.341",
                            "blue": "0.278",
                            "alpha": "1.000"
                        }
                    },
                    "idiom": "universal"
                }
            ],
            "info": {"author": "xcode", "version": 1}
        }, f, indent=2)

    # === ColorSet: AccentColor (simple) ===
    ac_dir = f"{base}/AccentColor.colorset"
    os.makedirs(ac_dir, exist_ok=True)
    with open(f"{ac_dir}/Contents.json", "w") as f:
        json.dump({
            "colors": [
                {
                    "color": {
                        "color-space": "srgb",
                        "components": {
                            "red": "0.200",
                            "green": "0.400",
                            "blue": "0.800",
                            "alpha": "1.000"
                        }
                    },
                    "idiom": "universal"
                }
            ],
            "info": {"author": "xcode", "version": 1}
        }, f, indent=2)

    # === DataSet: Config ===
    ds_dir = f"{base}/Config.dataset"
    os.makedirs(ds_dir, exist_ok=True)
    with open(f"{ds_dir}/config.json", "w") as f:
        json.dump({"app_name": "TestApp", "version": 2}, f)
    with open(f"{ds_dir}/Contents.json", "w") as f:
        json.dump({
            "data": [
                {
                    "filename": "config.json",
                    "idiom": "universal",
                    "universal-type-identifier": "public.json"
                }
            ],
            "info": {"author": "xcode", "version": 1}
        }, f, indent=2)

    # === Simple imageset for reference ===
    is_dir = f"{base}/Star.imageset"
    os.makedirs(is_dir, exist_ok=True)
    img = Image.new("RGBA", (16, 16), (255, 215, 0, 255))
    img.save(f"{is_dir}/Star.png")
    img2 = Image.new("RGBA", (32, 32), (255, 215, 0, 255))
    img2.save(f"{is_dir}/Star@2x.png")
    with open(f"{is_dir}/Contents.json", "w") as f:
        json.dump({
            "images": [
                {"filename": "Star.png", "idiom": "mac", "scale": "1x"},
                {"filename": "Star@2x.png", "idiom": "mac", "scale": "2x"}
            ],
            "info": {"author": "xcode", "version": 1}
        }, f, indent=2)

    # === SpriteAtlas: Sprites ===
    sa_dir = f"{base}/Sprites.spriteatlas"
    os.makedirs(sa_dir, exist_ok=True)
    with open(f"{sa_dir}/Contents.json", "w") as f:
        json.dump({"info": {"author": "xcode", "version": 1},
                    "properties": {"provides-namespace": True}}, f, indent=2)

    # Sprite 1
    sp1_dir = f"{sa_dir}/Coin.imageset"
    os.makedirs(sp1_dir, exist_ok=True)
    img = Image.new("RGBA", (24, 24), (255, 200, 0, 255))
    img.save(f"{sp1_dir}/Coin.png")
    img2 = Image.new("RGBA", (48, 48), (255, 200, 0, 255))
    img2.save(f"{sp1_dir}/Coin@2x.png")
    with open(f"{sp1_dir}/Contents.json", "w") as f:
        json.dump({
            "images": [
                {"filename": "Coin.png", "idiom": "universal", "scale": "1x"},
                {"filename": "Coin@2x.png", "idiom": "universal", "scale": "2x"}
            ],
            "info": {"author": "xcode", "version": 1}
        }, f, indent=2)

    # Sprite 2
    sp2_dir = f"{sa_dir}/Heart.imageset"
    os.makedirs(sp2_dir, exist_ok=True)
    img = Image.new("RGBA", (20, 20), (255, 0, 100, 255))
    img.save(f"{sp2_dir}/Heart.png")
    img2 = Image.new("RGBA", (40, 40), (255, 0, 100, 255))
    img2.save(f"{sp2_dir}/Heart@2x.png")
    with open(f"{sp2_dir}/Contents.json", "w") as f:
        json.dump({
            "images": [
                {"filename": "Heart.png", "idiom": "universal", "scale": "1x"},
                {"filename": "Heart@2x.png", "idiom": "universal", "scale": "2x"}
            ],
            "info": {"author": "xcode", "version": 1}
        }, f, indent=2)

    # === ImageStack: Badge ===
    # ImageStack contains ImageStackLayer dirs, each containing an imageset
    stack_dir = f"{base}/Badge.imagestack"
    os.makedirs(stack_dir, exist_ok=True)
    with open(f"{stack_dir}/Contents.json", "w") as f:
        json.dump({"info": {"author": "xcode", "version": 1},
                    "layers": [
                        {"filename": "Background.imagestacklayer"},
                        {"filename": "Foreground.imagestacklayer"}
                    ]}, f, indent=2)

    # Background layer
    bg_dir = f"{stack_dir}/Background.imagestacklayer"
    os.makedirs(bg_dir, exist_ok=True)
    with open(f"{bg_dir}/Contents.json", "w") as f:
        json.dump({"info": {"author": "xcode", "version": 1}}, f, indent=2)

    bg_img_dir = f"{bg_dir}/Content.imageset"
    os.makedirs(bg_img_dir, exist_ok=True)
    img = Image.new("RGBA", (32, 32), (50, 50, 200, 255))
    img.save(f"{bg_img_dir}/bg.png")
    img2 = Image.new("RGBA", (64, 64), (50, 50, 200, 255))
    img2.save(f"{bg_img_dir}/bg@2x.png")
    with open(f"{bg_img_dir}/Contents.json", "w") as f:
        json.dump({
            "images": [
                {"filename": "bg.png", "idiom": "mac", "scale": "1x"},
                {"filename": "bg@2x.png", "idiom": "mac", "scale": "2x"}
            ],
            "info": {"author": "xcode", "version": 1}
        }, f, indent=2)

    # Foreground layer
    fg_dir = f"{stack_dir}/Foreground.imagestacklayer"
    os.makedirs(fg_dir, exist_ok=True)
    with open(f"{fg_dir}/Contents.json", "w") as f:
        json.dump({"info": {"author": "xcode", "version": 1}}, f, indent=2)

    fg_img_dir = f"{fg_dir}/Content.imageset"
    os.makedirs(fg_img_dir, exist_ok=True)
    img = Image.new("RGBA", (32, 32), (255, 255, 255, 200))
    img.save(f"{fg_img_dir}/fg.png")
    img2 = Image.new("RGBA", (64, 64), (255, 255, 255, 200))
    img2.save(f"{fg_img_dir}/fg@2x.png")
    with open(f"{fg_img_dir}/Contents.json", "w") as f:
        json.dump({
            "images": [
                {"filename": "fg.png", "idiom": "mac", "scale": "1x"},
                {"filename": "fg@2x.png", "idiom": "mac", "scale": "2x"}
            ],
            "info": {"author": "xcode", "version": 1}
        }, f, indent=2)

    print(f"Created test catalog at {base}")
    for root, dirs, files in os.walk(base):
        level = root.replace(base, "").count(os.sep)
        indent = " " * 2 * level
        print(f"{indent}{os.path.basename(root)}/")
        subindent = " " * 2 * (level + 1)
        for file in files:
            print(f"{subindent}{file}")


if __name__ == "__main__":
    create_catalog()
