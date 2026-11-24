#!/usr/bin/env python3
"""
sc2rip - batch-rip StarCraft II unit & building models to COLLADA (.dae) + PNG textures.

Pipeline
  1. read the game's CASC storage (CascLib) or an already-extracted copy of the files
  2. pick every .m3/.m3a under Assets/Units and Assets/Buildings of the selected mods/campaigns
     (melee = standard multiplayer data, campaign = WoL/HotS/LotV/NCO campaigns), skipping
     campaign copies that are byte-identical to the standard ones
  3. extract them plus every texture they reference, convert DDS -> PNG
  4. run Blender 4.5 LTS headless with the M3Studio add-on to convert each model to .dae
     (skeleton, skin weights, materials and all animations)
  5. write one folder per model group, plus a CSV/JSON report

Run `python sc2rip.py --help` for options, and see README.md.
"""
import argparse
import concurrent.futures as cf
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import casc  # noqa: E402

CONVERTER_VERSION = '5'  # bump when output changes, so cached folders are redone
M3STUDIO_COMMIT = 'bef1b08136d73d6b530d07068dbb510aeba4c6db'  # version sc2rip was tested with
M3STUDIO_URL = 'https://github.com/Solstice245/m3studio/archive/{ref}.zip'

# --------------------------------------------------------------------------------------
# Classification of game containers (mods / campaigns)
# --------------------------------------------------------------------------------------

# (regex on the lower-cased container path, source group, label, melee priority)
# Melee priority mirrors SC2's own override order: later mods replace earlier ones' files.
CONTAINER_RULES = [
    (r'mods/core\.sc2mod', 'melee', 'Core', 0),
    (r'mods/liberty\.sc2mod', 'melee', 'Wings of Liberty', 1),
    (r'mods/swarm\.sc2mod', 'melee', 'Heart of the Swarm', 2),
    (r'mods/void\.sc2mod', 'melee', 'Legacy of the Void', 3),
    (r'mods/libertymulti\.sc2mod', 'melee', 'Wings of Liberty Multiplayer', 4),
    (r'mods/swarmmulti\.sc2mod', 'melee', 'Heart of the Swarm Multiplayer', 5),
    (r'mods/voidmulti\.sc2mod', 'melee', 'Legacy of the Void Multiplayer', 6),
    (r'campaigns/liberty\.sc2campaign', 'campaign', 'Wings of Liberty', None),
    (r'campaigns/libertystory\.sc2campaign', 'campaign', 'Wings of Liberty (Story)', None),
    (r'campaigns/swarm\.sc2campaign', 'campaign', 'Heart of the Swarm', None),
    (r'campaigns/swarmstory\.sc2campaign', 'campaign', 'Heart of the Swarm (Story)', None),
    (r'campaigns/voidprologue\.sc2campaign', 'campaign', 'Whispers of Oblivion', None),
    (r'campaigns/voidprologuestory\.sc2campaign', 'campaign', 'Whispers of Oblivion (Story)', None),
    (r'campaigns/void\.sc2campaign', 'campaign', 'Legacy of the Void', None),
    (r'campaigns/voidstory\.sc2campaign', 'campaign', 'Legacy of the Void (Story)', None),
    (r'campaigns/voidepilogue\.sc2campaign', 'campaign', 'Legacy of the Void Epilogue', None),
    (r'mods/novacampaign\.sc2mod', 'campaign', 'Nova Covert Ops', None),
    (r'mods/novastory\.sc2mod', 'campaign', 'Nova Covert Ops (Story)', None),
    (r'campaigns/[^/]+', 'campaign', None, None),
    (r'mods/(alliedcommanders|coop[^/]*|[^/]*commander[^/]*)\.sc2mod', 'coop', 'Co-op', None),
]
GROUP_ORDER = ['melee', 'campaign', 'coop', 'other']

ASSET_RE = re.compile(
    r'^(?P<container>.+?)/(?:(?P<pkg>[^/]+\.sc2assets)/)?assets/(?P<cat>units|buildings)/(?P<rel>.+)$')
TEXREF_RE = re.compile(rb'([A-Za-z0-9_\-\\/ .()]{3,260}?\.(?:dds|tga|png|jpg))\x00', re.I)


def classify_container(container_lc):
    for rx, group, label, prio in CONTAINER_RULES:
        if re.fullmatch(rx, container_lc):
            if label is None:
                stem = container_lc.rsplit('/', 1)[-1].split('.')[0]
                label = stem.title()
            return group, label, prio
    stem = container_lc.rsplit('/', 1)[-1].split('.')[0]
    return 'other', stem.title(), None


def pretty(component, *hints):
    """Pick a nicely-cased version of a path component (CascLib may lowercase directories)."""
    if component != component.lower():
        return component
    for h in hints:
        if h and h.lower() == component.lower() and h != h.lower():
            return h
    return component[:1].upper() + component[1:]


def safe(s):
    return re.sub(r'[<>:"|?*\x00-\x1f]', '_', s).strip().rstrip('.') or '_'


# --------------------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------------------

class ModelFile:
    __slots__ = ('path', 'lc', 'container', 'group', 'label', 'prio', 'cat', 'rel', 'ext')

    def __init__(self, path, m):
        self.path = path
        self.lc = path.lower()
        self.container = m.group('container')
        self.group, self.label, self.prio = classify_container(self.container.lower())
        self.cat = m.group('cat').lower()
        self.rel = path[m.start('rel'):]          # original case
        self.ext = self.lc.rsplit('.', 1)[-1]


def scan(storage, categories):
    models, textures = [], {}
    for p in storage.files():
        lc = p.lower()
        if lc.endswith(('.m3', '.m3a')):
            m = ASSET_RE.match(lc)
            # localized packages (zhcn.sc2assets, ...) hold regional variants of base models with the
            # same file names, and usually aren't downloaded - only use the base package
            if m and m.group('cat') in categories and m.group('pkg') in (None, 'base.sc2assets'):
                models.append(ModelFile(p, m))
        elif lc.endswith(('.dds', '.tga', '.png', '.jpg')):
            i = lc.find('assets/')
            if i >= 0:
                textures.setdefault(lc[i:], []).append(p)
            textures.setdefault('::base::' + lc.rsplit('/', 1)[-1], []).append(p)
    return models, textures


def container_of(path_lc):
    m = re.match(r'^(.+?\.(?:sc2mod|sc2campaign))/', path_lc)
    return m.group(1) if m else ''


def resolve_texture(ref, textures, container, melee_order):
    """Find the storage path for a texture reference like 'Assets\\Textures\\Foo.dds'."""
    key = ref.replace('\\', '/').lower().lstrip('/')
    cands = textures.get(key)
    if not cands and not key.startswith('assets/'):
        cands = textures.get('assets/textures/' + key.rsplit('/', 1)[-1])
    if not cands:
        cands = textures.get('::base::' + key.rsplit('/', 1)[-1])
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    container = container.lower()
    rank = {c: i for i, c in enumerate(melee_order[::-1])}  # newest melee mod first

    def score(p):
        lp = p.lower()
        localized = int('.sc2assets/' in lp and '/base.sc2assets/' not in lp)   # prefer base packages
        c = container_of(lp)
        if c == container:
            return (localized, 0, 0)
        if c in rank:
            return (localized, 1, rank[c])
        return (localized, 2, 0)
    return sorted(cands, key=score)[0]


def build_plan(models, args, log):
    """Group model files into output folders and drop duplicates."""
    groups = set(args.sources)
    if args.containers:
        wanted = [c.lower().replace('\\', '/').strip('/') for c in args.containers]
        models = [m for m in models if m.container.lower() in wanted]
    else:
        models = [m for m in models if m.group in groups]

    # melee: apply SC2's override order so each relative path resolves to one file
    melee_order = sorted({(m.prio, m.container.lower()) for m in models if m.group == 'melee'})
    melee_order = [c for _, c in melee_order]
    effective = {}
    for m in sorted([m for m in models if m.group == 'melee'], key=lambda m: m.prio):
        effective[(m.cat, m.rel.lower())] = m
    melee_models = list(effective.values())
    others = [m for m in models if m.group != 'melee']
    others.sort(key=lambda m: (GROUP_ORDER.index(m.group), m.container.lower(), m.rel.lower()))
    return melee_models + others, melee_order


def folder_for(m):
    parts = m.rel.split('/')
    race = pretty(parts[0]) if len(parts) >= 2 else 'Misc'
    stem = os.path.splitext(parts[-1])[0]
    unit = pretty(parts[1], stem) if len(parts) >= 3 else pretty(stem)
    cat = 'Units' if m.cat == 'units' else 'Buildings'
    if m.group == 'melee':
        grp = 'Melee'
    elif m.group == 'campaign':
        grp = f'Campaign - {m.label}'
    elif m.group == 'coop':
        grp = f'Co-op - {m.label}' if m.label != 'Co-op' else 'Co-op'
    else:
        grp = f'Other - {m.label}'
    return os.path.join(cat, safe(grp), safe(race), safe(unit))


# --------------------------------------------------------------------------------------
# Textures
# --------------------------------------------------------------------------------------

def dds_is_cubemap(data):
    import struct
    if len(data) < 128 or data[:4] != b'DDS ':
        return False
    caps2 = struct.unpack_from('<I', data, 4 + 108)[0]
    return bool(caps2 & 0x200)


def dds_cube_faces(data):
    """Split a cubemap DDS into its 6 faces (+X,-X,+Y,-Y,+Z,-Z) as PIL images."""
    import struct
    from PIL import Image
    h, w = struct.unpack_from('<II', data, 12)
    mips = max(1, struct.unpack_from('<I', data, 28)[0])
    pf_flags, fourcc = struct.unpack_from('<I4s', data, 80)
    rgb_bits = struct.unpack_from('<I', data, 88)[0]
    hdr = 128
    if fourcc == b'DX10':
        hdr += 20
        dxgi = struct.unpack_from('<I', data, 128)[0]
        block = 8 if dxgi in (70, 71, 72, 79, 80, 81) else 16   # BC1/BC4 vs others
        bc = dxgi in range(70, 100)
        bpp = None if bc else 32
    elif pf_flags & 0x4:   # FourCC compressed
        bc = True
        block = 8 if fourcc in (b'DXT1', b'ATI1', b'BC4U', b'BC4S') else 16
    else:
        bc = False
        bpp = rgb_bits or 32

    def level_size(lw, lh):
        if bc:
            return max(1, (lw + 3) // 4) * max(1, (lh + 3) // 4) * block
        return lw * lh * bpp // 8

    face_stride = sum(level_size(max(1, w >> i), max(1, h >> i)) for i in range(mips))
    top = level_size(w, h)
    header = bytearray(data[:hdr])
    struct.pack_into('<I', header, 28, 1)            # one mip level
    struct.pack_into('<I', header, 4 + 108, 0)       # not a cubemap any more
    caps = struct.unpack_from('<I', header, 4 + 104)[0] & ~0x400008
    struct.pack_into('<I', header, 4 + 104, caps | 0x1000)
    if fourcc == b'DX10':
        struct.pack_into('<I', header, 128 + 12, 1)  # arraySize
        misc = struct.unpack_from('<I', header, 128 + 8)[0] & ~0x4
        struct.pack_into('<I', header, 128 + 8, misc)
    faces = []
    for f in range(6):
        start = hdr + f * face_stride
        im = Image.open(io.BytesIO(bytes(header) + data[start:start + top]))
        im.load()
        faces.append(im.convert('RGBA'))
    return faces


def cube_to_equirect(faces, width=1024):
    """Cube faces -> equirectangular panorama in Blender's convention (Z up). The cube is looked up
    Direct3D-style with the game's Z-up vector mapped to D3D's Y-up (x, z, y)."""
    import numpy as np
    from PIL import Image
    H, W = width // 2, width
    u = (np.arange(W) + 0.5) / W
    v = (np.arange(H) + 0.5) / H
    uu, vv = np.meshgrid(u, v)
    phi = (0.5 - uu) * 2 * np.pi              # Blender: u = -atan2(dy, dx) / 2pi + 0.5 (+X at centre)
    lat = (0.5 - vv) * np.pi                  # image row 0 = top
    dx, dy, dz = np.cos(lat) * np.cos(phi), np.cos(lat) * np.sin(phi), np.sin(lat)
    x, y, z = dx, dz, dy                      # world (Z up) -> D3D cube space (Y up)
    ax, ay, az = np.abs(x), np.abs(y), np.abs(z)
    out = np.zeros((H, W, 4), np.uint8)
    arrs = [np.asarray(f) for f in faces]
    size = arrs[0].shape[0]
    conds = [(ax >= ay) & (ax >= az) & (x > 0), (ax >= ay) & (ax >= az) & (x <= 0),
             (ay > ax) & (ay >= az) & (y > 0), (ay > ax) & (ay >= az) & (y <= 0),
             (az > ax) & (az > ay) & (z > 0), (az > ax) & (az > ay) & (z <= 0)]
    sc_tc_ma = [(-z, -y, ax), (z, -y, ax), (x, z, ay), (x, -z, ay), (x, -y, az), (-x, -y, az)]
    for i, (cond, (sc, tc, ma)) in enumerate(zip(conds, sc_tc_ma)):
        fu = ((sc / np.maximum(ma, 1e-9) + 1) / 2 * size).astype(int).clip(0, size - 1)
        fv = ((tc / np.maximum(ma, 1e-9) + 1) / 2 * size).astype(int).clip(0, size - 1)
        out[cond] = arrs[i][fv[cond], fu[cond]]
    return Image.fromarray(out, 'RGBA')


def dds_to_png(data, out_path, normal_mode='raw', is_normal=False):
    """Returns 'equirect' when the DDS was a cubemap (converted to a panorama), else None."""
    from PIL import Image
    if dds_is_cubemap(data):
        cube_to_equirect(dds_cube_faces(data)).save(out_path, 'PNG')
        return 'equirect'
    im = Image.open(io.BytesIO(data))
    im.load()
    if im.mode not in ('RGBA', 'RGB', 'L', 'LA'):
        im = im.convert('RGBA')
    if is_normal and normal_mode == 'rgb' and im.mode == 'RGBA':
        try:
            import numpy as np
            a = np.asarray(im).astype('float32') / 255.0
            x = a[..., 3] * 2 - 1          # SC2 stores X in alpha ("DXT5nm")
            y = 1 - a[..., 1] * 2          # ... and Y DirectX-style: flip to OpenGL / Blender
            z = np.sqrt(np.clip(1 - x * x - y * y, 0, 1))
            rgb = np.stack([(x + 1) / 2, (y + 1) / 2, (z + 1) / 2], -1)
            im = Image.fromarray((rgb * 255 + 0.5).astype('uint8'), 'RGB')
        except ImportError:
            pass
    im.save(out_path, 'PNG', optimize=False)
    return None


# --------------------------------------------------------------------------------------
# Tools: Blender & M3Studio
# --------------------------------------------------------------------------------------

def default_blender_candidates():
    c = []
    env = os.environ.get('SC2RIP_BLENDER')
    if env:
        c.append(env)
    if sys.platform.startswith('win'):
        pf = os.environ.get('ProgramFiles', r'C:\Program Files')
        for v in ('4.5', '4.4', '4.3', '4.2'):
            c.append(os.path.join(pf, 'Blender Foundation', f'Blender {v}', 'blender.exe'))
        c.append(os.path.join(HERE, 'tools', 'blender', 'blender.exe'))
    elif sys.platform == 'darwin':
        c.append('/Applications/Blender.app/Contents/MacOS/Blender')
    else:
        c.append(os.path.join(HERE, 'tools', 'blender', 'blender'))
    w = shutil.which('blender')
    if w:
        c.append(w)
    return c


def converter_command(args):
    """Return the argv prefix that runs blender_convert.py, after checking the Blender version."""
    script = os.path.join(HERE, 'blender_convert.py')
    probe = ("import bpy,sys;print('SC2RIP_PROBE', bpy.app.version_string, "
             "hasattr(bpy.ops.wm,'collada_export'))")
    if args.bpy_python:
        prefix = [args.bpy_python, script]
        cmd = [args.bpy_python, '-c', probe]
    else:
        exe = args.blender
        if not exe:
            exe = next((p for p in default_blender_candidates() if p and os.path.isfile(p)), None)
        if not exe or not os.path.isfile(exe):
            raise SystemExit('Blender 4.5 LTS not found. Pass --blender PATH (see README).')
        prefix = [exe, '-b', '--factory-startup', '--python', script, '--']
        cmd = [exe, '-b', '--factory-startup', '--python-expr', probe]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300).stdout
    except Exception as e:
        raise SystemExit(f'Could not run Blender ({cmd[0]}): {e}')
    m = re.search(r'SC2RIP_PROBE (\S+).*?(True|False)', out)
    if not m:
        raise SystemExit(f'Could not query Blender version from {cmd[0]}. Output:\n{out[-2000:]}')
    version, has_dae = m.group(1), m.group(2) == 'True'
    if not has_dae:
        raise SystemExit(
            f'Blender {version} has no COLLADA exporter (it was removed in Blender 5.0).\n'
            f'Install the portable Blender 4.5 LTS build next to your normal Blender and pass it '
            f'with --blender (see README).')
    return prefix, version


def ensure_m3studio(args, log):
    if args.m3studio:
        p = os.path.abspath(args.m3studio)
        if not os.path.isfile(os.path.join(p, '__init__.py')):
            raise SystemExit(f'{p} does not look like the M3Studio add-on folder (no __init__.py)')
        return p
    dest = os.path.join(HERE, 'tools', 'm3studio')
    if os.path.isfile(os.path.join(dest, '__init__.py')):
        return dest
    ref = args.m3studio_ref or M3STUDIO_COMMIT
    url = M3STUDIO_URL.format(ref=ref)
    log(f'Downloading M3Studio ({ref[:12]}) from GitHub ...')
    try:
        data = urllib.request.urlopen(url, timeout=120).read()
    except Exception as e:
        raise SystemExit(f'Could not download M3Studio from {url}: {e}\n'
                         f'Download it manually and pass --m3studio PATH.')
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        top = z.namelist()[0].split('/')[0]
        tmp = os.path.join(HERE, 'tools', '_m3studio_tmp')
        shutil.rmtree(tmp, ignore_errors=True)
        z.extractall(tmp)
        shutil.rmtree(dest, ignore_errors=True)
        shutil.move(os.path.join(tmp, top), dest)
        shutil.rmtree(tmp, ignore_errors=True)
    return dest


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_argument_group('input')
    src.add_argument('--game', help='StarCraft II install folder (contains SC2Data and .build.info)')
    src.add_argument('--from-extracted', metavar='DIR',
                     help='use files already extracted from the game (e.g. with CascView) instead of CASC')
    src.add_argument('--casclib', help='path to the CascLib shared library (default: tools/casclib/)')
    sel = ap.add_argument_group('selection')
    sel.add_argument('--categories', default='units,buildings',
                     help='comma list: units,buildings (default: both)')
    sel.add_argument('--sources', default='melee,campaign',
                     help='comma list of melee,campaign,coop,other (default: melee,campaign)')
    sel.add_argument('--containers', nargs='*',
                     help='explicit container list, overrides --sources (e.g. mods/liberty.sc2mod)')
    sel.add_argument('--filter', help='regex; only output folders whose path matches (case-insensitive)')
    sel.add_argument('--exclude', help='regex; skip model files whose storage path matches')
    sel.add_argument('--no-dedupe', action='store_true',
                     help='keep campaign/co-op models that are byte-identical to an already-ripped one')
    sel.add_argument('--limit', type=int, default=0, help='only process the first N folders (testing)')
    out = ap.add_argument_group('output')
    out.add_argument('--out', required=True, help='output folder')
    out.add_argument('--work', help='scratch folder (default: <out>/_work)')
    out.add_argument('--anim-mode', default='timeline', choices=['timeline', 'separate', 'both', 'none'],
                     help='timeline: one .dae per model with every animation on one timeline + clip '
                          'list (default); separate: also one .dae per animation; none: static only')
    out.add_argument('--anim-sampling', type=int, default=1, help='sample every N frames (30 fps base)')
    out.add_argument('--normal-maps', default='raw', choices=['raw', 'rgb'],
                     help="raw: keep SC2's DXT5nm layout; rgb: rebuild a standard RGB normal map")
    out.add_argument('--save-blend', action='store_true',
                     help='also save a ready-to-view .blend next to each .dae')
    out.add_argument('--no-obj', action='store_true', help="don't write .obj/.mtl files next to the .dae files")
    out.add_argument('--keep-empty', action='store_true', help='export models that have no geometry')
    tools = ap.add_argument_group('tools')
    tools.add_argument('--blender', help='Blender 4.5 LTS executable (5.x has no COLLADA)')
    tools.add_argument('--bpy-python', help='alternative to --blender: a Python 3.11 with "bpy==4.5.*" installed')
    tools.add_argument('--m3studio', help='M3Studio add-on folder (default: auto-download to tools/m3studio)')
    tools.add_argument('--m3studio-ref', help='git ref to download instead of the tested commit, e.g. master')
    run = ap.add_argument_group('run')
    run.add_argument('--jobs', type=int, default=max(1, min(4, (os.cpu_count() or 2) // 2)),
                     help='parallel Blender processes')
    run.add_argument('--timeout', type=int, default=1800, help='seconds per folder')
    run.add_argument('--list-only', action='store_true',
                     help='only scan and write <out>/_plan.tsv + container summary (no extraction)')
    run.add_argument('--force', action='store_true', help='redo folders that already finished')
    args = ap.parse_args()

    args.categories = [c.strip().lower() for c in args.categories.split(',') if c.strip()]
    args.sources = [s.strip().lower() for s in args.sources.split(',') if s.strip()]
    # absolute paths everywhere: Blender stores texture paths in .blend files and must be able to
    # make them relative to the .blend, not to the folder sc2rip was started from
    args.out = os.path.abspath(args.out)
    os.makedirs(args.out, exist_ok=True)
    work = os.path.abspath(args.work or os.path.join(args.out, '_work'))
    os.makedirs(work, exist_ok=True)
    log_f = open(os.path.join(args.out, '_log.txt'), 'a', encoding='utf-8')

    def log(msg):
        line = f'[{time.strftime("%H:%M:%S")}] {msg}'
        print(line, flush=True)
        log_f.write(line + '\n')
        log_f.flush()

    # ---- 1. storage
    try:
        storage = casc.open_storage(args.game, args.from_extracted, args.casclib)
    except casc.StorageError as e:
        raise SystemExit(f'ERROR: {e}')
    log(f'Reading {storage.describe()}')
    t0 = time.time()
    models, textures = scan(storage, args.categories)
    log(f'{len(storage.files())} files in storage, {len(models)} unit/building model files '
        f'({time.time() - t0:.1f}s)')

    # container summary (always useful to sanity-check the classification)
    summary = {}
    for m in models:
        k = (m.container, m.group, m.label)
        summary.setdefault(k, {'units': 0, 'buildings': 0})[m.cat] += 1
    with open(os.path.join(args.out, '_containers.tsv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f, delimiter='\t', lineterminator='\n')
        w.writerow(['container', 'group', 'label', 'unit_files', 'building_files'])
        for (c, g, l), v in sorted(summary.items(), key=lambda kv: (GROUP_ORDER.index(kv[0][1]), kv[0][0])):
            w.writerow([c, g, l, v['units'], v['buildings']])
            log(f'  {g:8s} {c:45s} units={v["units"]:5d} buildings={v["buildings"]:5d}')

    planned, melee_order = build_plan(models, args, log)
    if args.exclude:
        rx = re.compile(args.exclude, re.I)
        planned = [m for m in planned if not rx.search(m.path)]

    # group into output folders (preserving order: melee first, then campaign, ...)
    # group case-insensitively (CascLib may lowercase directories) and keep the best-cased name
    folders, display = {}, {}
    for m in planned:
        name = folder_for(m)
        key = name.lower()
        folders.setdefault(key, []).append(m)
        if key not in display or sum(c.isupper() for c in name) > sum(c.isupper() for c in display[key]):
            display[key] = name
    folders = {display[k]: v for k, v in folders.items()}
    if args.filter:
        rx = re.compile(args.filter, re.I)
        folders = {k: v for k, v in folders.items() if rx.search(k.replace('\\', '/'))}

    with open(os.path.join(args.out, '_plan.tsv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f, delimiter='\t', lineterminator='\n')
        w.writerow(['output_folder', 'storage_path', 'group', 'container'])
        for k, ms in folders.items():
            for m in ms:
                w.writerow([k, m.path, m.group, m.container])
    n_m3 = sum(1 for ms in folders.values() for m in ms if m.ext == 'm3')
    log(f'Plan: {len(folders)} output folders, {n_m3} .m3 models (see _plan.tsv)')
    if args.list_only:
        return

    # ---- 2. tools
    try:
        import PIL  # noqa: F401
    except ImportError:
        raise SystemExit('Pillow is required for DDS -> PNG conversion: python3 -m pip install pillow')
    m3studio = ensure_m3studio(args, log)
    prefix, bl_version = converter_command(args)
    log(f'Using Blender {bl_version}, M3Studio at {m3studio}')

    # ---- 3. extract models, dedupe, collect textures
    raw_dir = os.path.join(work, 'raw')
    png_cache = os.path.join(work, 'png')
    job_dir = os.path.join(work, 'jobs')
    for d in (raw_dir, png_cache, job_dir):
        os.makedirs(d, exist_ok=True)

    seen_hashes = {}
    report_rows = []
    jobs = []
    folder_items = list(folders.items())
    if args.limit:
        folder_items = folder_items[:args.limit]

    tex_cache = {}  # storage path -> png in cache (or None on failure)
    equirect_pngs = set()  # cached pngs that are cubemaps converted to panoramas
    equirect_list = os.path.join(png_cache, '_equirect.txt')
    if os.path.isfile(equirect_list):
        equirect_pngs.update(open(equirect_list, encoding='utf-8').read().split('\n'))
    tex_fallback = []  # (dds, png) pairs Pillow couldn't decode

    def get_png(spath):
        if spath in tex_cache:
            return tex_cache[spath]
        base = os.path.splitext(spath.split('/')[-1])[0]
        is_normal = bool(re.search(r'(_normal|_nrm|_norm)\b', base.lower()))
        variant = '|rgb' if (is_normal and args.normal_maps == 'rgb') else ''
        h = hashlib.sha1((spath.lower() + variant).encode()).hexdigest()[:10]
        dst = os.path.join(png_cache, f'{base}__{h}.png')
        if not os.path.isfile(dst):
            try:
                data = storage.read(spath)
            except Exception as e:
                log(f'    ! cannot read texture {spath}: {e}')
                tex_cache[spath] = None
                return None
            try:
                if dds_to_png(data, dst, args.normal_maps, is_normal) == 'equirect':
                    equirect_pngs.add(dst)
                    with open(equirect_list, 'a', encoding='utf-8') as f:
                        f.write(dst + '\n')
            except Exception as e:
                raw = os.path.join(work, 'tex_raw', f'{base}__{h}' + os.path.splitext(spath)[1])
                os.makedirs(os.path.dirname(raw), exist_ok=True)
                with open(raw, 'wb') as f:
                    f.write(data)
                tex_fallback.append((raw, dst))
                log(f'    Pillow could not decode {base} ({e}); will retry with Blender')
        tex_cache[spath] = dst
        return dst

    log('Extracting models and textures ...')
    for fi, (folder, ms) in enumerate(folder_items):
        out_folder = os.path.join(args.out, folder)
        m3s = [m for m in ms if m.ext == 'm3']
        m3as = [m for m in ms if m.ext == 'm3a']
        job_models, tex_map = [], {}
        used_png_names = {}
        m3a_owner = {}
        for a in m3as:
            astem = os.path.splitext(a.rel.split('/')[-1])[0].lower()
            owners = [m for m in m3s if astem.startswith(os.path.splitext(m.rel.split('/')[-1])[0].lower())]
            if owners:
                m3a_owner[a.path] = max(owners, key=lambda m: len(m.rel.split('/')[-1]))
        for m in m3s:
            try:
                data = storage.read(m.path)
            except Exception as e:
                report_rows.append(dict(folder=folder, model=m.rel, status='read-error', error=str(e)))
                continue
            if data[:4] not in (b'43DM', b'33DM'):  # MD34 / MD33 headers (stored reversed)
                report_rows.append(dict(folder=folder, model=m.rel, status='not-m3',
                                        error=f'unknown header {data[:4]!r}'))
                continue
            digest = hashlib.sha1(data).hexdigest()
            if not args.no_dedupe and digest in seen_hashes:
                report_rows.append(dict(folder=folder, model=m.rel, status='duplicate',
                                        error=f'identical to {seen_hashes[digest]}'))
                continue
            seen_hashes[digest] = m.path
            raw = os.path.join(raw_dir, *safe(m.path).split('/'))
            os.makedirs(os.path.dirname(raw), exist_ok=True)
            with open(raw, 'wb') as f:
                f.write(data)

            # textures referenced by this model
            refs = sorted({r.decode('latin-1') for r in TEXREF_RE.findall(data)})
            for ref in refs:
                key = ref.replace('/', '\\').strip().lower()
                if key in tex_map:
                    continue
                spath = resolve_texture(ref, textures, m.container, melee_order)
                if not spath:
                    continue
                png = get_png(spath)
                if not png:
                    continue
                name = os.path.splitext(spath.split('/')[-1])[0]
                name = pretty(name, os.path.splitext(ref.replace('\\', '/').split('/')[-1])[0])
                fname = name + '.png'
                if used_png_names.get(fname.lower(), spath) != spath:
                    fname = f'{name}_{hashlib.sha1(spath.encode()).hexdigest()[:6]}.png'
                used_png_names[fname.lower()] = spath
                tex_map[key] = (png, os.path.join(out_folder, fname))

            stem = os.path.splitext(m.rel.split('/')[-1])[0]
            # .m3a animation files in this folder belong to the model with the longest matching name
            extra = [a for a in m3as if m3a_owner.get(a.path) is m]
            m3a_paths = []
            for a in extra:
                araw = os.path.join(raw_dir, *safe(a.path).split('/'))
                if not os.path.isfile(araw):
                    try:
                        adata = storage.read(a.path)
                    except Exception:
                        continue
                    os.makedirs(os.path.dirname(araw), exist_ok=True)
                    with open(araw, 'wb') as f:
                        f.write(adata)
                m3a_paths.append(araw)
            job_models.append({'m3': raw, 'm3a': m3a_paths, 'dae': os.path.join(out_folder, safe(stem) + '.dae'),
                               'storage_path': m.path, 'rel': m.rel})
        if not job_models:
            continue
        jobs.append({'folder': folder, 'out_folder': out_folder, 'models': job_models, 'tex_map': tex_map})
        if (fi + 1) % 50 == 0:
            log(f'  {fi + 1}/{len(folder_items)} folders prepared')

    # Blender-based fallback for textures Pillow couldn't read
    if tex_fallback:
        log(f'Converting {len(tex_fallback)} textures with Blender ...')
        fb_job = os.path.join(job_dir, '_textures.json')
        with open(fb_job, 'w', encoding='utf-8') as f:
            json.dump({'m3studio': m3studio, 'texture_fallback': tex_fallback, 'models': [],
                       'result': fb_job + '.result'}, f)
        subprocess.run(prefix + ['--job', fb_job], capture_output=True, timeout=args.timeout)

    # ---- 4. convert
    options = {'anim_mode': args.anim_mode, 'sampling_rate': args.anim_sampling,
               'save_blend': args.save_blend, 'skip_empty': not args.keep_empty, 'animations': True,
               'obj': not args.no_obj, 'normal_maps': args.normal_maps,
               'converter_version': CONVERTER_VERSION, 'blender_version': bl_version}

    def expected_outputs(m):
        base = os.path.splitext(m['dae'])[0]
        outs = [m['dae']]
        if options['obj']:
            outs += [base + '.obj', base + '.mtl']
        if options['save_blend']:
            outs.append(base + '.blend')
        return outs

    def run_job(job):
        out_folder = job['out_folder']
        os.makedirs(out_folder, exist_ok=True)
        textures_out = {}
        for key, (png, dst) in job['tex_map'].items():
            if os.path.isfile(png):
                # (re)copy when missing or when the cached conversion is different / newer
                if not os.path.isfile(dst) or os.path.getsize(dst) != os.path.getsize(png) or \
                        os.path.getmtime(dst) < os.path.getmtime(png):
                    shutil.copyfile(png, dst)
                textures_out[key] = dst
        jid = hashlib.sha1(job['folder'].encode()).hexdigest()[:12]
        jpath = os.path.join(job_dir, jid + '.json')
        rpath = os.path.join(job_dir, jid + '.result.json')
        equirect = sorted(k for k, (png, _) in job['tex_map'].items() if png in equirect_pngs)
        job_data = {'m3studio': m3studio, 'models': job['models'], 'textures': textures_out,
                    'equirect': equirect, 'options': options, 'result': rpath}
        # reuse a previous result only if it was produced from exactly the same job (same models,
        # textures, options and converter version) and every expected output file still exists
        if not args.force and os.path.isfile(rpath) and os.path.isfile(jpath):
            try:
                with open(jpath, encoding='utf-8') as f:
                    prev_job = json.load(f)
                with open(rpath, encoding='utf-8') as f:
                    prev = json.load(f)
                same = json.dumps(prev_job, sort_keys=True) == json.dumps(job_data, sort_keys=True)
                complete = len(prev['models']) == len(job['models']) and all(
                    r.get('skipped') or (r.get('ok') and all(os.path.isfile(p) for p in expected_outputs(m)))
                    for m, r in zip(job['models'], prev['models']))
                if same and complete:
                    return job, prev, 'cached'
            except Exception:
                pass
        if os.path.isfile(rpath):
            os.remove(rpath)
        with open(jpath, 'w', encoding='utf-8') as f:
            json.dump(job_data, f, indent=1)
        logpath = os.path.join(job_dir, jid + '.log')
        try:
            with open(logpath, 'w', encoding='utf-8', errors='replace') as lf:
                subprocess.run(prefix + ['--job', jpath], stdout=lf, stderr=subprocess.STDOUT,
                               timeout=args.timeout)
        except subprocess.TimeoutExpired:
            return job, None, f'timeout (see {logpath})'
        if not os.path.isfile(rpath):
            return job, None, f'Blender crashed (see {logpath})'
        with open(rpath, encoding='utf-8') as f:
            return job, json.load(f), 'done'

    log(f'Converting {len(jobs)} folders with {args.jobs} parallel Blender process(es) ...')
    done = 0
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futures = [ex.submit(run_job, j) for j in jobs]
        for fut in cf.as_completed(futures):
            job, res, how = fut.result()
            done += 1
            if res is None:
                log(f'  [{done}/{len(jobs)}] FAILED {job["folder"]}: {how}')
                for m in job['models']:
                    report_rows.append(dict(folder=job['folder'], model=m['rel'], status='failed', error=how))
                continue
            oks = 0
            for m, r in zip(job['models'], res['models']):
                status = 'skipped' if r.get('skipped') else ('ok' if r.get('ok') else 'error')
                oks += r.get('ok', False)
                err = r.get('error', '') or r.get('skipped', '')
                report_rows.append(dict(
                    folder=job['folder'], model=m['rel'], status=status,
                    dae=os.path.relpath(m['dae'], args.out) if status == 'ok' else '',
                    animations=len(r.get('animations', [])), meshes=r.get('meshes', ''),
                    bones=r.get('bones', ''), missing_textures=';'.join(r.get('missing_textures', [])),
                    warnings=len(r.get('warnings', [])), error=err.strip().splitlines()[-1] if err else ''))
            log(f'  [{done}/{len(jobs)}] {job["folder"]}: {oks}/{len(job["models"])} ok ({how})')

    # ---- 5. report
    fields = ['folder', 'model', 'status', 'dae', 'animations', 'meshes', 'bones',
              'missing_textures', 'warnings', 'error']
    with open(os.path.join(args.out, '_report.tsv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter='\t', lineterminator='\n')
        w.writeheader()
        for r in report_rows:
            w.writerow({k: str(r.get(k, '')).replace('\t', ' ').replace('\n', ' ') for k in fields})
    counts = {}
    for r in report_rows:
        counts[r['status']] = counts.get(r['status'], 0) + 1
    log('Finished: ' + ', '.join(f'{k}={v}' for k, v in sorted(counts.items())) +
        f'. Report: {os.path.join(args.out, "_report.tsv")}')


if __name__ == '__main__':
    main()
