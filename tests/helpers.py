"""Shared test helpers for actool tests."""

import json
import os
import shutil
import struct
import tempfile

from PIL import Image

REF_SAMPLES = os.path.join(os.path.dirname(__file__), "ref_samples")
REF_XCASSETS = os.path.join(REF_SAMPLES, "Catalog.xcassets")
REF_CAR = os.path.join(REF_SAMPLES, "ref_output", "Assets.car")
REF_PLIST = os.path.join(REF_SAMPLES, "ref_output", "AppIcon.Info.plist")
ASSETUTIL = "/usr/bin/assetutil"


def has_assetutil():
    return os.path.isfile(ASSETUTIL) and os.access(ASSETUTIL, os.X_OK)


def has_ref_car():
    return os.path.isfile(REF_CAR)


def make_temp_catalog(imagesets, tmpdir=None, groups=None):
    """Create a temporary xcassets catalog.

    imagesets: list of (name, mode) where mode is 'RGBA' or 'LA'.
    groups: optional dict of {group_name: [(name, mode), ...]} for nested
            group subdirectories.
    Returns (catalog_path, tmpdir).
    """
    if tmpdir is None:
        tmpdir = tempfile.mkdtemp(prefix="actool_test_")
    catalog = os.path.join(tmpdir, "Test.xcassets")
    os.makedirs(catalog, exist_ok=True)

    with open(os.path.join(catalog, "Contents.json"), "w") as f:
        json.dump({"info": {"author": "xcode", "version": 1}}, f)

    def _add_imageset(parent_dir, name, mode):
        iset = os.path.join(parent_dir, f"{name}.imageset")
        os.makedirs(iset, exist_ok=True)
        color = (200, 100, 50, 255) if mode == "RGBA" else (128, 255)
        Image.new(mode, (16, 16), color).save(os.path.join(iset, f"{name}.png"))
        Image.new(mode, (32, 32), color).save(os.path.join(iset, f"{name}@2x.png"))
        with open(os.path.join(iset, "Contents.json"), "w") as f:
            json.dump({
                "images": [
                    {"filename": f"{name}.png", "idiom": "mac", "scale": "1x"},
                    {"filename": f"{name}@2x.png", "idiom": "mac", "scale": "2x"},
                ],
                "info": {"author": "xcode", "version": 1},
            }, f)

    for name, mode in imagesets:
        _add_imageset(catalog, name, mode)

    if groups:
        for group_name, group_imagesets in groups.items():
            group_dir = os.path.join(catalog, group_name)
            os.makedirs(group_dir, exist_ok=True)
            with open(os.path.join(group_dir, "Contents.json"), "w") as f:
                json.dump({"info": {"author": "xcode", "version": 1}}, f)
            for name, mode in group_imagesets:
                _add_imageset(group_dir, name, mode)

    return catalog, tmpdir


def parse_car_layouts(car_path):
    """Parse a CAR file and return {rendition_name: layout_type}.

    Note: names may not be unique (e.g., packed assets share names).
    Use parse_car_info()['layout_counts'] for accurate counting.
    """
    with open(car_path, 'rb') as f:
        data = f.read()

    idx_off = struct.unpack('>I', data[16:20])[0]
    idx_data = data[idx_off:]
    n = struct.unpack('>I', idx_data[:4])[0]
    blocks = []
    for i in range(n):
        off, ln = struct.unpack('>II', idx_data[4 + i * 8:12 + i * 8])
        blocks.append((off, ln))

    vars_off, vars_ln = struct.unpack('>II', data[24:32])
    vd = data[vars_off:vars_off + vars_ln]
    nv = struct.unpack('>I', vd[:4])[0]
    named = {}
    p = 4
    for _ in range(nv):
        vi = struct.unpack('>I', vd[p:p + 4])[0]
        nl = vd[p + 4]
        nm = vd[p + 5:p + 5 + nl].decode()
        named[nm] = vi
        p += 5 + nl

    rend_idx = named['RENDITIONS']
    tree_off, tree_ln = blocks[rend_idx]
    tree_hdr = data[tree_off:tree_off + tree_ln]
    child = struct.unpack('>I', tree_hdr[8:12])[0]

    results = {}

    def collect(node_idx):
        off, ln = blocks[node_idx]
        node = data[off:off + ln]
        is_leaf = struct.unpack('>H', node[:2])[0]
        count = struct.unpack('>H', node[2:4])[0]
        if is_leaf:
            pos = 12
            for _ in range(count):
                vi = struct.unpack('>I', node[pos:pos + 4])[0]
                pos += 8
                voff, vln = blocks[vi]
                val = data[voff:voff + vln]
                name = val[40:168].split(b'\x00')[0].decode('ascii', errors='replace')
                layout = struct.unpack('<H', val[36:38])[0]
                results[name] = layout
        else:
            pos = 12
            c0 = struct.unpack('>I', node[pos:pos + 4])[0]
            collect(c0)
            pos += 4
            for _ in range(count):
                pos += 4
                c = struct.unpack('>I', node[pos:pos + 4])[0]
                pos += 4
                collect(c)

    collect(child)
    return results


def parse_car_info(car_path):
    """Parse key structural info from a CAR file."""
    with open(car_path, 'rb') as f:
        data = f.read()

    info = {"file_size": len(data)}

    idx_off = struct.unpack('>I', data[16:20])[0]
    idx_data = data[idx_off:]
    n = struct.unpack('>I', idx_data[:4])[0]
    blocks = []
    for i in range(n):
        off, ln = struct.unpack('>II', idx_data[4 + i * 8:12 + i * 8])
        blocks.append((off, ln))

    info["num_blocks"] = struct.unpack('>I', data[12:16])[0]
    info["table_count"] = n

    vars_off, vars_ln = struct.unpack('>II', data[24:32])
    vd = data[vars_off:vars_off + vars_ln]
    nv = struct.unpack('>I', vd[:4])[0]
    named = {}
    p = 4
    for _ in range(nv):
        vi = struct.unpack('>I', vd[p:p + 4])[0]
        nl = vd[p + 4]
        nm = vd[p + 5:p + 5 + nl].decode()
        named[nm] = vi
        p += 5 + nl
    info["named_blocks"] = sorted(named.keys())

    # KEYFORMAT
    if "KEYFORMAT" in named:
        kf_off, kf_ln = blocks[named["KEYFORMAT"]]
        kf = data[kf_off:kf_off + kf_ln]
        kf_count = struct.unpack('<I', kf[8:12])[0]
        info["keyformat_count"] = kf_count

    # Rendition count
    if "RENDITIONS" in named:
        r_off, r_ln = blocks[named["RENDITIONS"]]
        r_hdr = data[r_off:r_off + r_ln]
        if r_hdr[:4] == b'tree':
            info["rendition_count"] = struct.unpack('>I', r_hdr[16:20])[0]

    # BITMAPKEYS count
    if "BITMAPKEYS" in named:
        bk_off, bk_ln = blocks[named["BITMAPKEYS"]]
        bk_hdr = data[bk_off:bk_off + bk_ln]
        if bk_hdr[:4] == b'tree':
            info["bitmapkeys_count"] = struct.unpack('>I', bk_hdr[16:20])[0]

    # Layout counts (from tree entries, not deduplicated names)
    if "RENDITIONS" in named:
        r_idx = named["RENDITIONS"]
        r_off, r_ln = blocks[r_idx]
        r_hdr = data[r_off:r_off + r_ln]
        if r_hdr[:4] == b'tree':
            r_child = struct.unpack('>I', r_hdr[8:12])[0]
            all_layouts = []

            def _count_layouts(ni):
                no, nl = blocks[ni]
                nd = data[no:no + nl]
                il = struct.unpack('>H', nd[:2])[0]
                nc = struct.unpack('>H', nd[2:4])[0]
                if il:
                    p = 12
                    for _ in range(nc):
                        vi = struct.unpack('>I', nd[p:p + 4])[0]
                        p += 8
                        vo, vl = blocks[vi]
                        v = data[vo:vo + vl]
                        all_layouts.append(struct.unpack('<H', v[36:38])[0])
                else:
                    p = 12
                    c0 = struct.unpack('>I', nd[p:p + 4])[0]
                    _count_layouts(c0)
                    p += 4
                    for _ in range(nc):
                        p += 4
                        c = struct.unpack('>I', nd[p:p + 4])[0]
                        p += 4
                        _count_layouts(c)

            _count_layouts(r_child)
            layout_counts = {}
            for l in all_layouts:
                layout_counts[l] = layout_counts.get(l, 0) + 1
            info["layout_counts"] = layout_counts

    return info


def run_assetutil(car_path):
    """Run assetutil -I and return parsed JSON, or None if unavailable."""
    if not has_assetutil():
        return None
    import subprocess
    # assetutil requires relative paths (sandboxed filesystem)
    rel_path = os.path.relpath(car_path)
    result = subprocess.run(
        [ASSETUTIL, "-I", rel_path],
        capture_output=True, text=True, timeout=30)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        import json
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


ASSETUTIL_TMPDIR = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                 "_test_output")


def get_test_outdir(name):
    """Get a clean test output directory that assetutil can read."""
    outdir = os.path.join(ASSETUTIL_TMPDIR, name)
    if os.path.exists(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir)
    return outdir


def cleanup_test_outputs():
    """Remove all test output directories."""
    if os.path.exists(ASSETUTIL_TMPDIR):
        shutil.rmtree(ASSETUTIL_TMPDIR)


VALIDATE_CAR = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "tools", "validate_car")
EXTRACT_PIXELS = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                               "tools", "extract_pixels")


def has_validate_car():
    return os.path.isfile(VALIDATE_CAR) and os.access(VALIDATE_CAR, os.X_OK)


def has_extract_pixels():
    return os.path.isfile(EXTRACT_PIXELS) and os.access(EXTRACT_PIXELS, os.X_OK)


def extract_car_image(car_path, image_name, output_dir):
    """Extract pixel data for a named image from a CAR file.

    Returns dict of {scale: (width, height, rgba_bytes)} or empty if tool
    unavailable.
    """
    import subprocess
    if not has_extract_pixels():
        return {}
    subprocess.run([EXTRACT_PIXELS, car_path, image_name, output_dir],
                   capture_output=True, timeout=10)
    results = {}
    for fname in os.listdir(output_dir):
        if fname.startswith(image_name + "_") and fname.endswith("x.rgba"):
            scale = int(fname.split("_")[-1].replace("x.rgba", ""))
            path = os.path.join(output_dir, fname)
            with open(path, 'rb') as f:
                w = struct.unpack('<I', f.read(4))[0]
                h = struct.unpack('<I', f.read(4))[0]
                pixels = f.read()
            results[scale] = (w, h, pixels)
    return results


def validate_car_rendering(car_path):
    """Run validate_car to test actual pixel rendering of all images.

    Returns (successes, failures, details) where details is a list of
    (status, name) tuples. Status is 'OK', 'FAIL', or 'CRASH'.
    """
    import subprocess
    result = subprocess.run(
        [VALIDATE_CAR, car_path],
        capture_output=True, text=True, timeout=30)
    details = []
    successes = 0
    failures = 0
    for line in result.stdout.splitlines():
        if line.startswith("OK   "):
            details.append(("OK", line[5:].split(" (")[0]))
            successes += 1
        elif line.startswith("FAIL "):
            details.append(("FAIL", line[5:].split(" (")[0]))
            failures += 1
        elif line.startswith("CRASH "):
            details.append(("CRASH", line[6:].split(" (")[0]))
            failures += 1
    return successes, failures, details


SYSTEM_ACTOOL = "/usr/bin/actool"


def has_system_actool():
    return os.path.isfile(SYSTEM_ACTOOL) and os.access(SYSTEM_ACTOOL, os.X_OK)


def compile_with_system_actool(xcassets_path, outdir, app_icon=None,
                               min_deploy="11.0"):
    """Compile an xcassets catalog using the system actool.

    Returns True on success, False on failure.
    """
    import subprocess
    os.makedirs(outdir, exist_ok=True)
    cmd = [
        SYSTEM_ACTOOL, "--compile", outdir,
        "--platform", "macosx",
        "--minimum-deployment-target", min_deploy,
    ]
    if app_icon:
        cmd += ["--app-icon", app_icon,
                "--output-partial-info-plist",
                os.path.join(outdir, "AppIcon.Info.plist")]
    cmd.append(xcassets_path)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return result.returncode == 0


def parse_car_inlk_entries(car_path):
    """Parse all INLK (internal link) entries from a CAR file.

    Returns a list of dicts with:
      name: rendition name from CSI header
      scale: scale factor (1 or 2)
      inlk_attrs: dict of {token_id: value} from trailing attributes
      padding: the single uint16 padding value (should be 0)
      f1: first header field (should be 12)
      f2: second header field (byte count of attr data)
    """
    with open(car_path, 'rb') as f:
        data = f.read()

    idx_off = struct.unpack('>I', data[16:20])[0]
    idx_data = data[idx_off:idx_off + struct.unpack('>I', data[20:24])[0]]
    n = struct.unpack('>I', idx_data[:4])[0]
    blocks = {}
    for i in range(n):
        addr = struct.unpack('>I', idx_data[4 + i * 8:8 + i * 8])[0]
        length = struct.unpack('>I', idx_data[8 + i * 8:12 + i * 8])[0]
        if addr != 0 or length != 0:
            blocks[i] = (addr, length)

    def read_block(idx):
        if idx not in blocks:
            return b''
        return data[blocks[idx][0]:blocks[idx][0] + blocks[idx][1]]

    vars_off, vars_ln = struct.unpack('>II', data[24:32])
    vd = data[vars_off:vars_off + vars_ln]
    nv = struct.unpack('>I', vd[:4])[0]
    named = {}
    p = 4
    for _ in range(nv):
        bi = struct.unpack('>I', vd[p:p + 4])[0]
        nl = vd[p + 4]
        nm = vd[p + 5:p + 5 + nl].decode('ascii')
        p += 5 + nl
        named[nm] = bi

    rend_tree = read_block(named['RENDITIONS'])
    rt_root = struct.unpack('>I', rend_tree[8:12])[0]

    renditions = []

    def collect(block_idx):
        nd = read_block(block_idx)
        is_leaf = struct.unpack('>H', nd[:2])[0]
        cnt = struct.unpack('>H', nd[2:4])[0]
        if is_leaf:
            pos = 12
            for _ in range(cnt):
                vi = struct.unpack('>I', nd[pos:pos + 4])[0]
                ki = struct.unpack('>I', nd[pos + 4:pos + 8])[0]
                pos += 8
                renditions.append((read_block(ki), read_block(vi)))
        else:
            pos = 12
            c = struct.unpack('>I', nd[pos:pos + 4])[0]
            pos += 4
            collect(c)
            for _ in range(cnt):
                pos += 4
                c2 = struct.unpack('>I', nd[pos:pos + 4])[0]
                pos += 4
                collect(c2)

    collect(rt_root)

    results = []
    for kd, vd_block in renditions:
        if len(vd_block) < 184 or b'KLNI' not in vd_block:
            continue
        name = vd_block[40:168].rstrip(b'\x00').decode('ascii', errors='replace')
        sf = struct.unpack('<I', vd_block[20:24])[0]

        tlv_len = struct.unpack('<I', vd_block[168:172])[0]
        pos = 184
        end = 184 + tlv_len
        while pos + 8 <= end:
            tag = struct.unpack('<I', vd_block[pos:pos + 4])[0]
            tlen = struct.unpack('<I', vd_block[pos + 4:pos + 8])[0]
            if tag == 0x03F2:
                inlk = vd_block[pos + 8:pos + 8 + tlen]
                f1 = struct.unpack('<H', inlk[24:26])[0]
                f2 = struct.unpack('<H', inlk[26:28])[0]
                padding = struct.unpack('<H', inlk[28:30])[0]
                ap = 30
                attrs = {}
                while ap + 4 <= 28 + f2:
                    a_id = struct.unpack('<H', inlk[ap:ap + 2])[0]
                    a_val = struct.unpack('<H', inlk[ap + 2:ap + 4])[0]
                    if a_id == 0:
                        break
                    attrs[a_id] = a_val
                    ap += 4
                results.append({
                    'name': name,
                    'scale': sf // 100,
                    'inlk_attrs': attrs,
                    'padding': padding,
                    'f1': f1,
                    'f2': f2,
                })
            pos += 8 + tlen

    return results


def parse_car_atlas_keys(car_path):
    """Parse atlas rendition keys from a CAR file.

    Returns a set of (dim1, scale) tuples for atlas entries (layout 1004).
    """
    with open(car_path, 'rb') as f:
        data = f.read()

    idx_off = struct.unpack('>I', data[16:20])[0]
    idx_data = data[idx_off:idx_off + struct.unpack('>I', data[20:24])[0]]
    n = struct.unpack('>I', idx_data[:4])[0]
    blocks = {}
    for i in range(n):
        addr = struct.unpack('>I', idx_data[4 + i * 8:8 + i * 8])[0]
        length = struct.unpack('>I', idx_data[8 + i * 8:12 + i * 8])[0]
        if addr != 0 or length != 0:
            blocks[i] = (addr, length)

    def read_block(idx):
        if idx not in blocks:
            return b''
        return data[blocks[idx][0]:blocks[idx][0] + blocks[idx][1]]

    vars_off, vars_ln = struct.unpack('>II', data[24:32])
    vd = data[vars_off:vars_off + vars_ln]
    nv = struct.unpack('>I', vd[:4])[0]
    named = {}
    p = 4
    for _ in range(nv):
        bi = struct.unpack('>I', vd[p:p + 4])[0]
        nl = vd[p + 4]
        nm = vd[p + 5:p + 5 + nl].decode('ascii')
        p += 5 + nl
        named[nm] = bi

    kf = read_block(named['KEYFORMAT'])
    kf_count = struct.unpack('<I', kf[8:12])[0]
    tokens = [struct.unpack('<I', kf[12 + i * 4:16 + i * 4])[0] for i in range(kf_count)]

    rend_tree = read_block(named['RENDITIONS'])
    rt_root = struct.unpack('>I', rend_tree[8:12])[0]

    renditions = []

    def collect(block_idx):
        nd = read_block(block_idx)
        is_leaf = struct.unpack('>H', nd[:2])[0]
        cnt = struct.unpack('>H', nd[2:4])[0]
        if is_leaf:
            pos = 12
            for _ in range(cnt):
                vi = struct.unpack('>I', nd[pos:pos + 4])[0]
                ki = struct.unpack('>I', nd[pos + 4:pos + 8])[0]
                pos += 8
                renditions.append((read_block(ki), read_block(vi)))
        else:
            pos = 12
            c = struct.unpack('>I', nd[pos:pos + 4])[0]
            pos += 4
            collect(c)
            for _ in range(cnt):
                pos += 4
                c2 = struct.unpack('>I', nd[pos:pos + 4])[0]
                pos += 4
                collect(c2)

    collect(rt_root)

    atlas_keys = set()
    for kd, vd_block in renditions:
        if len(vd_block) < 40:
            continue
        layout = struct.unpack('<H', vd_block[36:38])[0]
        if layout == 1004:
            vals = struct.unpack(f'<{len(kd) // 2}H', kd)
            tok_map = {tokens[i]: vals[i]
                       for i in range(min(len(tokens), len(vals)))}
            atlas_keys.add((tok_map.get(8, 0), tok_map.get(12, 0)))

    return atlas_keys
