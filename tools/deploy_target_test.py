"""Test system actool with deployment targets from 10.13 to 26.0."""

import os
import shutil
import struct
import subprocess
import sys


def run_actool(doc, outdir, deploy, extra_args=None):
    os.makedirs(outdir, exist_ok=True)
    cmd = ["/usr/bin/actool", doc, "--compile", outdir,
           "--platform", "macosx", "--minimum-deployment-target", deploy]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True)


def analyze_car(path):
    with open(path, 'rb') as f:
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

    # CARHEADER
    ch_off, ch_ln = blocks[named['CARHEADER']]
    ch = data[ch_off:ch_off + ch_ln]
    coreui_ver = struct.unpack('<I', ch[4:8])[0]
    storage_ver = struct.unpack('<I', ch[8:12])[0]
    rend_count = struct.unpack('<I', ch[16:20])[0]

    # RENDITIONS count
    rend_idx = named['RENDITIONS']
    tree_off, tree_ln = blocks[rend_idx]
    tree_hdr = data[tree_off:tree_off + tree_ln]
    path_count = struct.unpack('>I', tree_hdr[16:20])[0]

    # Compression types
    child = struct.unpack('>I', tree_hdr[8:12])[0]
    comp_types = set()

    def collect_leaves(node_idx):
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
                layout = struct.unpack('<H', val[36:38])[0]
                if layout in (12, 1004):  # Inline or PackedAsset
                    rend_len = struct.unpack('<I', val[180:184])[0]
                    if rend_len > 0:
                        tvl_len = struct.unpack('<I', val[168:172])[0]
                        rs = 184 + tvl_len
                        if rs + 12 <= len(val):
                            comp = struct.unpack('<I', val[rs + 8:rs + 12])[0]
                            comp_types.add(comp)
        else:
            pos = 12
            c0 = struct.unpack('>I', node[pos:pos + 4])[0]
            collect_leaves(c0)
            pos += 4
            for _ in range(count):
                pos += 4
                c = struct.unpack('>I', node[pos:pos + 4])[0]
                pos += 4
                collect_leaves(c)

    collect_leaves(child)

    return {
        'size': len(data),
        'rend_count': path_count,
        'coreui_ver': coreui_ver,
        'storage_ver': storage_ver,
        'named_blocks': sorted(named.keys()),
        'comp_types': sorted(comp_types),
    }


def analyze_icns(path):
    with open(path, 'rb') as f:
        data = f.read()
    pos = 8
    types = []
    while pos < len(data):
        tc = data[pos:pos + 4].decode()
        el = struct.unpack('>I', data[pos + 4:pos + 8])[0]
        types.append(tc)
        pos += el
    return {'size': len(data), 'types': types}


VERSIONS = [
    "10.13", "10.14", "10.15",
    "11.0", "12.0", "13.0", "14.0", "15.0",
    "16.0", "17.0", "18.0", "19.0", "20.0",
    "21.0", "22.0", "23.0", "24.0", "25.0", "26.0",
]

# Test 1: xcassets with app icon
print("=" * 80)
print("TEST: test/Images.xcassets with --app-icon AppIcon")
print("=" * 80)

base = "deploy_test"
shutil.rmtree(base, ignore_errors=True)
prev = None

for ver in VERSIONS:
    outdir = f"{base}/xcassets_{ver}"
    result = run_actool("test/Images.xcassets", outdir, ver,
                        ["--app-icon", "AppIcon",
                         "--output-partial-info-plist", f"{outdir}/Info.plist"])
    car_path = f"{outdir}/Assets.car"
    icns_path = f"{outdir}/AppIcon.icns"

    if not os.path.exists(car_path):
        print(f"  {ver:6s}: NO OUTPUT (stderr: {result.stderr[:80]})")
        continue

    info = analyze_car(car_path)
    icns_info = analyze_icns(icns_path) if os.path.exists(icns_path) else None

    changed = ""
    if prev:
        diffs = []
        for k in ['size', 'rend_count', 'coreui_ver', 'storage_ver',
                   'comp_types']:
            if info.get(k) != prev.get(k):
                diffs.append(f"{k}: {prev[k]}→{info[k]}")
        if icns_info and prev_icns:
            if icns_info['types'] != prev_icns['types']:
                diffs.append(f"icns_types: {prev_icns['types']}→{icns_info['types']}")
            if icns_info['size'] != prev_icns['size']:
                diffs.append(f"icns_size: {prev_icns['size']}→{icns_info['size']}")
        if diffs:
            changed = "  CHANGED: " + ", ".join(diffs)

    print(f"  {ver:6s}: car={info['size']:7d}b rend={info['rend_count']:2d} "
          f"comp={info['comp_types']} "
          f"icns={'%db %s' % (icns_info['size'], icns_info['types']) if icns_info else 'none'}"
          f"{changed}")

    prev = info
    prev_icns = icns_info

# Test 2: .icon bundle
print()
print("=" * 80)
print("TEST: test_element/Icon.icon with --app-icon Icon")
print("=" * 80)

prev = None
prev_icns = None

for ver in VERSIONS:
    outdir = f"{base}/icon_{ver}"
    result = run_actool("test_element/Icon.icon", outdir, ver,
                        ["--app-icon", "Icon",
                         "--output-partial-info-plist", f"{outdir}/Info.plist"])
    car_path = f"{outdir}/Assets.car"
    icns_path = f"{outdir}/Icon.icns"

    if not os.path.exists(car_path):
        print(f"  {ver:6s}: NO OUTPUT (stderr: {result.stderr[:80]})")
        continue

    info = analyze_car(car_path)
    icns_info = analyze_icns(icns_path) if os.path.exists(icns_path) else None

    changed = ""
    if prev:
        diffs = []
        for k in ['size', 'rend_count', 'coreui_ver', 'storage_ver',
                   'comp_types']:
            if info.get(k) != prev.get(k):
                diffs.append(f"{k}: {prev[k]}→{info[k]}")
        if icns_info and prev_icns:
            if icns_info['types'] != prev_icns['types']:
                diffs.append(f"icns_types: {prev_icns['types']}→{icns_info['types']}")
            if icns_info['size'] != prev_icns['size']:
                diffs.append(f"icns_size: {prev_icns['size']}→{icns_info['size']}")
        elif icns_info and not prev_icns:
            diffs.append(f"icns: none→{icns_info['types']}")
        elif not icns_info and prev_icns:
            diffs.append(f"icns: {prev_icns['types']}→none")
        if diffs:
            changed = "  CHANGED: " + ", ".join(diffs)

    print(f"  {ver:6s}: car={info['size']:7d}b rend={info['rend_count']:2d} "
          f"comp={info['comp_types']} "
          f"icns={'%db %s' % (icns_info['size'], icns_info['types']) if icns_info else 'none'}"
          f"{changed}")

    prev = info
    prev_icns = icns_info
