#!/usr/bin/env python3
"""
sc2rip - batch-rip StarCraft II unit & building models to COLLADA (.dae) + PNG textures.

Pipeline
  1. read the game's CASC storage (CascLib) or an already-extracted copy of the files
  2. pick every .m3/.m3a under Assets/Units, Assets/Buildings and Assets/Portraits of the selected mods/campaigns
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
import struct
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import casc  # noqa: E402
import game_data  # noqa: E402
import team_colors  # noqa: E402

CONVERTER_VERSION = '9'  # bump when output changes, so cached folders are redone
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
    (r'mods/balancemulti[^/]*\.sc2mod', 'melee', 'Multiplayer balance', 7),
    (r'campaigns/liberty\.sc2campaign', 'campaign', 'Wings of Liberty', None),
    (r'campaigns/libertystory\.sc2campaign', 'campaign', 'Wings of Liberty (Story)', None),
    (r'campaigns/swarm\.sc2campaign', 'campaign', 'Heart of the Swarm', None),
    (r'campaigns/swarmstory[^/]*\.(sc2campaign|sc2mod)', 'campaign', 'Heart of the Swarm (Story)', None),
    (r'(campaigns|mods)/voidprologue\.(sc2campaign|sc2mod)', 'campaign', 'Whispers of Oblivion', None),
    (r'campaigns/voidprologuestory\.sc2campaign', 'campaign', 'Whispers of Oblivion (Story)', None),
    (r'campaigns/void\.sc2campaign', 'campaign', 'Legacy of the Void', None),
    (r'campaigns/voidstory\.sc2campaign', 'campaign', 'Legacy of the Void (Story)', None),
    (r'campaigns/voidepilogue\.sc2campaign', 'campaign', 'Legacy of the Void Epilogue', None),
    (r'mods/novacampaign\.sc2mod', 'campaign', 'Nova Covert Ops', None),
    (r'mods/novastory[^/]*\.sc2mod', 'campaign', 'Nova Covert Ops (Story)', None),
    (r'mods/nova[^/]*\.sc2mod', 'campaign', 'Nova Covert Ops', None),
    (r'campaigns/[^/]+', 'campaign', None, None),
    # co-op missions: mods/starcoop/starcoop.sc2mod, mods/starcoop/commanders/<commander>.sc2mod, ...
    (r'mods/starcoop/.+', 'coop', 'Co-op', None),
    (r'mods/(alliedcommanders|coop[^/]*|[^/]*commander[^/]*)\.sc2mod', 'coop', 'Co-op', None),
    # Blizzard's Warcraft III mods for the arcade (assets, data, and the Warcraft co-op/classic modes)
    (r'mods/(war3|war3data)\.sc2mod|mods/warcoop/.+', 'other', 'Warcraft III', None),
]
GROUP_ORDER = ['melee', 'campaign', 'coop', 'other', 'unused']

ASSET_RE = re.compile(
    r'^(?P<container>.+?)/(?:(?P<pkg>[^/]+\.sc2assets)/)?assets/(?P<cat>units|buildings|portraits)/(?P<rel>.+)$')
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
    __slots__ = ('path', 'lc', 'container', 'group', 'label', 'prio', 'cat', 'rel', 'ext',
                 'out_group', 'out_label', 'refs', 'ref_how', 'nest')

    def __init__(self, path, m):
        self.path = path
        self.lc = path.lower()
        self.container = m.group('container')
        self.group, self.label, self.prio = classify_container(self.container.lower())
        self.cat = m.group('cat').lower()
        self.rel = path[m.start('rel'):]          # original case
        self.ext = self.lc.rsplit('.', 1)[-1]
        # where the model is filed in the output: by default where it is stored, see classify_by_data()
        self.out_group, self.out_label = self.group, self.label
        self.refs, self.ref_how = None, ''
        self.nest = None    # (unit folder, subfolder or None, unit category): see nest_unit_folders()


TERRAIN_DATA_RE = re.compile(r'^(?P<container>.+?)/base\.sc2data/gamedata/terraintexdata\.xml$')


def scan(storage, categories):
    """-> (unit/building model files, {texture key: [storage paths]}, [TerrainTexData.xml paths],
    [every game data .xml path])"""
    models, textures, terrain_data, game_data = [], {}, [], []
    for p in storage.files():
        lc = p.lower()
        if lc.endswith('.xml') and '.sc2data/gamedata/' in lc:
            game_data.append(p)
            if lc.endswith('terraintexdata.xml') and TERRAIN_DATA_RE.match(lc):
                terrain_data.append(p)
        elif lc.endswith(('.m3', '.m3a')):
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
    return models, textures, terrain_data, game_data


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


# --------------------------------------------------------------------------------------
# Sorting models by the game data that uses them
# --------------------------------------------------------------------------------------
# Since patch 4.0 nearly every model file is stored in mods/liberty.sc2mod, whatever uses it, so
# where a file is stored says nothing about melee vs campaign. The game data does: each mod and
# campaign declares the models it uses (CModel entries in its GameData .xml files, also in the
# data of individual campaign maps). A model is filed as:
#   Melee     used by a melee mod (core, liberty, swarm, void and their *multi mods)
#   Campaign  otherwise, used by a campaign (the first one in CONTAINER_RULES order)
#   Co-op     otherwise, used by a co-op mod
#   Other     otherwise, used by another mod
#   Unused    not referenced by any game data (cut content, or models only used by name in scripts)
# A unit's folder is never split: the whole asset folder goes where its "best" model goes.
#
# Most CModel entries don't spell out their file: they inherit a template path such as
# Assets\Units\##race##\##id##\##id##.m3 from a parent entry (see game_data.py). A model counts as
# used by a mod when, in that mod's data:
#   path     a CModel entry (or any other field) names the file, templates expanded
#   variant  it names the file without its variation / part suffix: SC2 picks x_00.m3, x_01.m3 ...
#            for a model x.m3 with variations, and x_prtmdl_*.m3 are the parts of x.m3's physics
#   id       a CModel entry's id is the file's name (in case a template wasn't understood)
#   folder   (only if none of the above) another file in the same folder is used

MODELREF_RE = re.compile(rb'["\'>]\s*([^"\'<>\r\n]{1,260}?\.m3)\s*["\'<]', re.I)
MODEL_REFS_VERSION = 2


def data_owner(path_lc):
    """The mod / campaign a game data file belongs to (campaign maps count as their campaign)."""
    c = container_of(path_lc)
    if c:
        return c
    return path_lc.split('/', 1)[0]


def base_data_containers():
    """Containers whose data every other mod builds on: the melee mods, in load order."""
    rules = sorted((prio, rx) for rx, group, _label, prio in CONTAINER_RULES
                   if group == 'melee' and prio is not None and re.fullmatch(r'[\w/]+\\\.sc2mod', rx))
    return [rx.replace('\\.', '.') for _, rx in rules]


def _ref_key(value):
    k = value.replace('\\', '/').strip().lower()
    j = k.find('assets/')
    return k[j:] if j >= 0 and k.endswith('.m3') else None


def load_model_refs(storage, xml_paths, cache_path, log):
    """-> {'paths': {'assets/units/.../x.m3': {data containers}}, 'ids': {cmodel id: {containers}}}
    from every game data .xml (see above)."""
    sig = hashlib.sha1(('v%d\n' % MODEL_REFS_VERSION + '\n'.join(sorted(p.lower() for p in xml_paths)))
                       .encode()).hexdigest()
    try:
        with open(cache_path, encoding='utf-8') as f:
            cached = json.load(f)
        if cached.get('sig') == sig:
            return {k: {kk: set(vv) for kk, vv in cached[k].items()} for k in ('paths', 'ids')}
    except (OSError, ValueError, KeyError, AttributeError):
        pass
    paths, ids, entries, failed, bad_xml = {}, {}, [], 0, 0
    t0 = time.time()
    for i, p in enumerate(sorted(xml_paths)):
        if i and i % 500 == 0:
            log(f'  ... {i}/{len(xml_paths)} game data files')
        try:
            data = storage.read(p)
        except Exception:
            failed += 1
            continue
        owner = data_owner(p.lower())
        # explicit paths anywhere (actors, effects ...)
        for r in MODELREF_RE.findall(data):
            k = _ref_key(r.decode('latin-1'))
            if k:
                paths.setdefault(k, set()).add(owner)
        # CModel entries, with their templates
        if b'<CModel' in data:
            try:
                entries += game_data.parse_catalog(data, owner, ('CModel',),
                                                   lambda v: '.m3' in v.lower() or '##' in v)
            except ET.ParseError:
                bad_xml += 1
    cat = game_data.Catalog(entries, base_data_containers())
    n_tpl = 0
    for tag, eid, owner in cat.concrete():
        ids.setdefault(eid.lower(), set()).add(owner)
        for _field, v in cat.values(tag, eid, owner):
            k = _ref_key(v)
            if k:
                if owner not in paths.get(k, ()):
                    n_tpl += 1
                paths.setdefault(k, set()).add(owner)
    log(f'  read {len(xml_paths) - failed} game data files in {time.time() - t0:.0f}s'
        + (f' ({failed} unreadable)' if failed else '') + (f' ({bad_xml} not valid XML)' if bad_xml else '')
        + f': {len(paths)} model paths referenced ({n_tpl} through templates), {len(ids)} CModel ids')
    res = {'paths': paths, 'ids': ids}
    try:
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump({'sig': sig, **{k: {kk: sorted(vv) for kk, vv in v.items()} for k, v in res.items()}}, f)
    except OSError:
        pass
    return res


def _rule_index(container_lc):
    for i, (rx, *_rest) in enumerate(CONTAINER_RULES):
        if re.fullmatch(rx, container_lc):
            return i
    return len(CONTAINER_RULES)


def _data_rank(c):
    return GROUP_ORDER.index(classify_container(c)[0]), _rule_index(c), c


def data_class(containers):
    """(group, label, rank) for a model used by these data containers."""
    if not containers:
        return 'unused', 'Unused', (GROUP_ORDER.index('unused'), 0, '')
    best = min(containers, key=_data_rank)
    group, label, _ = classify_container(best)
    return group, label, _data_rank(best)


VARIANT_RE = re.compile(r'(?:_prtmdl.*|_\d+)$')


def model_base_names(stem):
    """'thor_silver_death_00_prtmdl_rib1' -> ['thor_silver_death_00', 'thor_silver_death']"""
    out = []
    while True:
        s = VARIANT_RE.sub('', stem)
        if not s or s == stem:
            return out
        out.append(s)
        stem = s


def model_usage(m, refs, dir_refs):
    """-> (data containers using model m, how that was found)"""
    paths, ids = refs['paths'], refs['ids']
    folder, name = m.rel.lower().rsplit('/', 1) if '/' in m.rel else ('', m.rel.lower())
    stem = os.path.splitext(name)[0]
    prefix = f'assets/{m.cat}/' + (folder + '/' if folder else '')
    used, how = set(), []
    if prefix + name in paths:
        used |= paths[prefix + name]
        how.append('path')
    for base in model_base_names(stem):
        if prefix + base + '.m3' in paths:
            used |= paths[prefix + base + '.m3']
            how.append('variant')
            break
    for s in [stem] + model_base_names(stem):
        if s in ids:
            new = ids[s] - used
            if new:
                used |= new
                how.append('id')
            break
    if not used and folder and prefix in dir_refs:
        used = set(dir_refs[prefix])
        how.append('folder')
    return used, '+'.join(how)


def classify_by_data(models, refs, log):
    """Set out_group / out_label / refs / ref_how of every model from the game data that uses it.
    Unused models stored outside the melee mods (e.g. the Warcraft III asset mod) stay filed where
    they are stored."""
    dir_refs = {}
    for k, owners in refs['paths'].items():
        dir_refs.setdefault(k.rsplit('/', 1)[0] + '/', set()).update(owners)
    rank = {}
    for m in models:
        used, m.ref_how = model_usage(m, refs, dir_refs)
        m.refs = sorted(used)
        m.out_group, m.out_label, rank[id(m)] = data_class(used)
        if not used and m.group not in ('melee', 'campaign'):
            m.out_group, m.out_label = m.group, m.label
            rank[id(m)] = (GROUP_ORDER.index(m.group), _rule_index(m.container.lower()), m.container.lower())
    # keep each asset folder together: all its files go where its best .m3 goes
    by_folder = {}
    for m in models:
        by_folder.setdefault(asset_folder_key(m), []).append(m)
    for ms in by_folder.values():
        best = min((m for m in ms if m.ext == 'm3'), default=ms[0], key=lambda m: rank[id(m)])
        for m in ms:
            m.out_group, m.out_label = best.out_group, best.out_label
            if not m.ref_how and best.ref_how:
                m.ref_how = 'unit' if m.nest else 'folder'
    counts, hows = {}, {}
    for m in models:
        if m.ext == 'm3':
            counts[(m.out_group, m.out_label)] = counts.get((m.out_group, m.out_label), 0) + 1
            h = m.ref_how or 'none'
            hows[h] = hows.get(h, 0) + 1
    for (g, l), n in sorted(counts.items(), key=lambda kv: (GROUP_ORDER.index(kv[0][0]), kv[0][1])):
        log(f'  {g:8s} {l:40s} {n:5d} .m3 files')
    log('  found by: ' + ', '.join(f'{h} {n}' for h, n in sorted(hows.items(), key=lambda kv: -kv[1])))


def asset_folder_key(m):
    """The unit a model file belongs to: (category, race, unit folder). Death models nested under
    a unit (see nest_unit_folders) belong to that unit."""
    parts = m.rel.lower().split('/')
    if m.nest:
        return (m.nest[2], parts[0], m.nest[0].lower())
    return (m.cat, parts[0], parts[1] if len(parts) >= 3 else os.path.splitext(parts[-1])[0])


# Death models live in folders of their own, named after their unit: MarineDeathAcid,
# Marine_Silver_DeathRagdoll, BarracksFlyDeath, Hatchery_Mecha_Hive_Death... They are filed in a
# subfolder of that unit, named without the unit's name: Units/Melee/Terran/Marine/deathacid.
# The unit is the longest other folder of the same race whose name starts the death folder's name,
# with at most one of these unit states in between (BarracksFlyDeath -> Barracks/flydeath).
#
# Extra animation libraries (.m3a files with no model: Marine_SwarmAnims, ArchonOptionalAnims,
# Marine_BlackOps_DLC2Anims...) go into the folder of the unit they animate (the longest folder of
# the same race with a model whose name starts theirs), and are added to its model's animations.
DEATH_STATES = ('', 'empty', 'fly', 'morph', 'unpowered', 'detached', 'open', 'lowered', 'sieged',
                'unburrowed', 'burrow', 'burrowed', 'phasing', 'hive', 'lair', 'yamato', 'warpgate',
                'acid', 'fire', 'walk', 'physics', 'construction')


PORTRAIT_RE = re.compile(r'_?portrait', re.I)


def nest_unit_folders(models, deaths=True, anims=True, portraits=True):
    """Set m.nest = (unit folder, subfolder or None, unit category) for the files of death folders,
    animation library folders and portrait folders that belong to a unit folder.
    -> (death folders, animation folders, portrait folders)"""
    folders, exts = {}, {}      # (cat, race) -> {lowercase folder: original case}; folder -> {ext}
    for m in models:
        parts = m.rel.split('/')
        if len(parts) >= 3:
            key = (m.cat, parts[0].lower())
            folders.setdefault(key, {}).setdefault(parts[1].lower(), parts[1])
            exts.setdefault(key + (parts[1].lower(),), set()).add(m.ext)
    nested, n_death, n_anim, n_portrait = {}, 0, 0, 0
    for (cat, race), names in folders.items():
        for name in names:
            i = name.find('death')
            if portraits and 'portrait' in name or cat == 'portraits':
                # portraits: Assets/Portraits/<race>/<name>, or a *Portrait folder among the units
                # -> the unit (or building) folder of the same race named like it without "portrait"
                if not portraits or 'm3' not in exts[(cat, race, name)]:
                    continue
                unit = PORTRAIT_RE.sub('', name).strip('_')
                for ucat in ('units', 'buildings'):
                    unames = folders.get((ucat, race), {})
                    if unit in unames and (ucat, race, unit) != (cat, race, name) \
                            and 'm3' in exts[(ucat, race, unit)]:
                        sub = name[len(unit):].strip('_') if name.startswith(unit) else ''
                        if 'portrait' not in sub:
                            sub = 'portrait' + ('_' + sub if sub else '')
                        nested[(cat, race, name)] = (unames[unit], sub, ucat)
                        n_portrait += 1
                        break
            elif deaths and i > 0:
                head = name[:i]
                cands = [p for p in names if p != name and len(p) >= 3 and 'death' not in p
                         and head.startswith(p) and head[len(p):].strip('_') in DEATH_STATES]
                if cands:
                    parent = max(cands, key=len)
                    nested[(cat, race, name)] = (names[parent], name[len(parent):].strip('_'), cat)
                    n_death += 1
            elif anims and exts[(cat, race, name)] == {'m3a'}:
                cands = [p for p in names if p != name and len(p) >= 3 and name.startswith(p)
                         and 'm3' in exts[(cat, race, p)]]
                if cands:
                    nested[(cat, race, name)] = (names[max(cands, key=len)], None, cat)
                    n_anim += 1
    for m in models:
        parts = m.rel.split('/')
        if len(parts) >= 3:
            m.nest = nested.get((m.cat, parts[0].lower(), parts[1].lower()))
    return n_death, n_anim, n_portrait


# --------------------------------------------------------------------------------------
# Terrain materials
# --------------------------------------------------------------------------------------
# Models such as mineral fields have a "terrain" material on the ground around them: in game it
# shows whatever terrain texture the map has there, so the model has no texture of its own.
# A stand-in terrain texture from the game's terrain texture catalogue is used instead.

def m3_has_terrain_material(data):
    """True when an .m3 file contains a terrain material (a 'TER_' section in its index)."""
    try:
        _, ioff, count = struct.unpack_from('<4sII', data, 0)
        for i in range(count):
            if data[ioff + 16 * i: ioff + 16 * i + 4] == b'_RET':   # tags are stored reversed
                return True
        return False
    except struct.error:
        return b'_RET' in data


def load_terrain_catalogue(storage, paths, melee_order, log):
    """{id: {'id', 'diffuse', 'normal', 'container'}} from every TerrainTexData.xml, templates
    expanded (see game_data.py). An id defined by several mods is taken from a melee mod if it can
    be (the newest one), otherwise from the first other mod."""
    entries = []
    for path in paths:
        try:
            entries += game_data.parse_catalog(storage.read(path), container_of(path.lower()), ('CTerrainTex',))
        except Exception as e:
            log(f'  ! cannot read {path}: {e}')
    cat = game_data.Catalog(entries, base_data_containers())
    rank = {c: i for i, c in enumerate(melee_order)}
    best = {}
    for tag, eid, container in cat.concrete():
        key = (container not in rank, -rank.get(container, 0), container)
        if eid not in best or key < best[eid][0]:
            best[eid] = (key, container)
    out = {}
    for eid, (_key, container) in best.items():
        vals = cat.values('CTerrainTex', eid, container)
        e = {'id': eid, 'container': container}
        for field, v in vals:
            if field == 'texture' and 'diffuse' not in e:
                e['diffuse'] = v
            elif field == 'normalmap' and 'normal' not in e:
                e['normal'] = v
        if e.get('diffuse'):
            out[eid] = e
    return out


TERRAIN_KEYWORDS = ('dirt', 'soil', 'sand', 'ground', 'gravel', 'ash', 'rock')
TERRAIN_AVOID = ('creep', 'lava', 'water', 'snow', 'ice', 'space', 'metal', 'tile', 'grate', 'plate',
                 'road', 'grass', 'moss', 'blood', 'acid', 'glow', 'crystal', 'test', 'black', 'white')
TERRAIN_TILESETS = ('belshir', 'agria', 'marsara', 'char', 'redstone', 'tarsonis', 'avernus')


def pick_terrain_texture(catalogue):
    """A plain earth texture: ids with 'dirt' first (then soil, sand...), favouring common melee
    tilesets, then the shortest name."""
    best = None
    for tid, e in catalogue.items():
        t = tid.lower()
        kw = next((i for i, k in enumerate(TERRAIN_KEYWORDS) if k in t), None)
        if kw is None or any(a in t for a in TERRAIN_AVOID):
            continue
        ts = next((i for i, k in enumerate(TERRAIN_TILESETS) if t.startswith(k)), len(TERRAIN_TILESETS))
        key = (kw, ts, len(t), t)
        if best is None or key < best[0]:
            best = (key, e)
    return best[1] if best else None


def build_plan(models, args, log):
    """Group model files into output folders and drop duplicates."""
    groups = set(args.sources)
    if args.containers:
        wanted = [c.lower().replace('\\', '/').strip('/') for c in args.containers]
        models = [m for m in models if m.container.lower() in wanted]
    else:
        models = [m for m in models if m.out_group in groups]

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
    if m.nest:
        unit = safe(pretty(m.nest[0], stem[:len(m.nest[0])]))
        if m.nest[1]:
            unit = os.path.join(unit, safe(m.nest[1].lower()))
    cat = {'units': 'Units', 'buildings': 'Buildings', 'portraits': 'Portraits'}[m.nest[2] if m.nest else m.cat]
    group, label = m.out_group, m.out_label
    if group == 'melee':
        grp = 'Melee'
    elif group == 'campaign':
        grp = f'Campaign - {label}'
    elif group == 'coop':
        grp = f'Co-op - {label}' if label != 'Co-op' else 'Co-op'
    elif group == 'unused':
        grp = 'Unused'
    else:
        grp = f'Other - {label}'
    return os.path.join(cat, safe(grp), safe(race), unit if m.nest else safe(unit))


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
    if is_normal and normal_mode == 'rgb':
        try:
            import numpy as np
            a = np.asarray(im.convert('RGBA')).astype('float32') / 255.0
            # SC2 uses packed "DXT5nm" maps (X in alpha, blue unused) and plain RGB maps (blue = Z)
            plain = a[..., 2].mean() > 0.6 and a[..., 3].std() < 0.02
            x = (a[..., 0] if plain else a[..., 3]) * 2 - 1
            y = 1 - a[..., 1] * 2          # SC2 is DirectX-style (green down): flip to OpenGL / Blender
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

def find_files(args):
    """--find: list matching storage paths with their container classification."""
    try:
        storage = casc.open_storage(args.game, args.from_extracted, args.casclib)
    except casc.StorageError as e:
        raise SystemExit(f'ERROR: {e}')
    rx = re.compile(args.find, re.I)
    rows = []
    for p in sorted(storage.files(), key=str.lower):
        if rx.search(p):
            c = container_of(p.lower())
            group, label, _ = classify_container(c)
            m = ASSET_RE.match(p.lower())
            note = ''
            if p.lower().endswith(('.m3', '.m3a')):
                if not m:
                    note = 'not under Assets/Units, Assets/Buildings or Assets/Portraits'
                elif m.group('pkg') not in (None, 'base.sc2assets'):
                    note = f'localized package {m.group("pkg")} (ignored)'
            rows.append((p, c, group, note))
    for p, c, group, note in rows:
        print(f'{group:8s}  {p}' + (f'   [{note}]' if note else ''))
    print(f'{len(rows)} matching files out of {len(storage.files())}', file=sys.stderr)
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, '_find.tsv'), 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f, delimiter='\t', lineterminator='\n')
            w.writerow(['storage_path', 'container', 'group', 'note'])
            w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_argument_group('input')
    src.add_argument('--game', help='StarCraft II install folder (contains SC2Data and .build.info)')
    src.add_argument('--from-extracted', metavar='DIR',
                     help='use files already extracted from the game (e.g. with CascView) instead of CASC')
    src.add_argument('--casclib', help='path to the CascLib shared library (default: tools/casclib/)')
    sel = ap.add_argument_group('selection')
    sel.add_argument('--categories', default='units,buildings,portraits',
                     help='comma list: units,buildings,portraits (default: all three)')
    sel.add_argument('--sources', default='melee,campaign,unused',
                     help='comma list of melee,campaign,coop,other,unused: which models to rip, by what uses '
                          'them (see --classify; default: melee,campaign,unused)')
    sel.add_argument('--classify', choices=('data', 'location'), default='data',
                     help='sort models as melee/campaign/co-op/... by the game data that uses them (data, '
                          'default) or by the mod/campaign they are stored in (location: since patch 4.0 '
                          'nearly everything is stored in liberty.sc2mod)')
    sel.add_argument('--containers', nargs='*',
                     help='explicit container list, overrides --sources (e.g. mods/liberty.sc2mod)')
    sel.add_argument('--filter', help='regex; only output folders whose path matches (case-insensitive)')
    sel.add_argument('--exclude', help='regex; skip model files whose storage path matches')
    sel.add_argument('--flat-deaths', action='store_true',
                     help="keep death models in folders of their own (default: in a subfolder of their unit, "
                          "e.g. Units/Melee/Terran/Marine/deathacid)")
    sel.add_argument('--flat-portraits', action='store_true',
                     help='keep portrait models in Portraits/... folders of their own (default: in a subfolder '
                          'of their unit, e.g. Units/Melee/Terran/Marine/portrait)')
    sel.add_argument('--no-extra-anims', action='store_true',
                     help="don't add the animation libraries of other folders (Marine_SwarmAnims, "
                          "Marine_BlackOps_DLC2Anims...) to their unit's model")
    sel.add_argument('--no-dedupe', action='store_true',
                     help='keep campaign/co-op models that are byte-identical to an already-ripped one')
    sel.add_argument('--limit', type=int, default=0, help='only process the first N folders (testing)')
    sel.add_argument('--find', metavar='REGEX',
                     help='only list the files in the game storage whose path matches (case-insensitive), '
                          'e.g. --find mothershipcore, and exit')
    out = ap.add_argument_group('output')
    out.add_argument('--out', help='output folder (required, except with --find)')
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
    out.add_argument('--no-glb', action='store_true', help="don't write .glb (glTF binary) files next to the .dae files")
    out.add_argument('--team-colors', default='red', metavar='LIST',
                     help='.glb team colours: the first is baked into the materials, the others become '
                          'material variants (KHR_materials_variants). Comma-separated SC2 player colour names '
                          '(red, blue, teal, purple, yellow, orange, green, "light pink", violet, "light grey", '
                          '"dark green", brown, "light green", "dark grey", pink), #rrggbb, none (plain '
                          'texture) or all. Default: red')
    out.add_argument('--glb-texture-anims', choices=('safe', 'all', 'none'), default='safe',
                     help='texture (UV) animations in the .glb: safe = only those Blender\'s glTF importer '
                          'can read (diffuse textures of lit materials); all = also glows, emission, normal and '
                          'specular maps (crashes Blender\'s importer: for other viewers); none. Default: safe')
    out.add_argument('--keep-empty', action='store_true', help='export models that have no geometry')
    out.add_argument('--terrain-texture', default='auto', metavar='ID|PATH|auto|none',
                     help='stand-in for the map terrain shown by terrain materials (the ground around '
                          'mineral fields...): auto (a plain dirt texture from the game), a terrain texture '
                          'id from _terrain_textures.tsv, a texture path in the game, or none (plain colour)')
    out.add_argument('--terrain-tile', type=float, default=4.0,
                     help='game units covered by one repeat of the terrain texture (default 4)')
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
    if args.find:
        find_files(args)
        return
    if not args.out:
        ap.error('the following arguments are required: --out')
    try:
        team_colors.parse(args.team_colors)
    except ValueError as e:
        ap.error(f'--team-colors: {e}')

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
    models, textures, terrain_data, game_data = scan(storage, args.categories)
    log(f'{len(storage.files())} files in storage, {len(models)} unit/building/portrait model files '
        f'({time.time() - t0:.1f}s)')

    # container summary (always useful to sanity-check the classification)
    summary = {}
    for m in models:
        k = (m.container, m.group, m.label)
        summary.setdefault(k, {'units': 0, 'buildings': 0, 'portraits': 0})[m.cat] += 1
    with open(os.path.join(args.out, '_containers.tsv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f, delimiter='\t', lineterminator='\n')
        w.writerow(['container', 'group', 'label', 'unit_files', 'building_files', 'portrait_files'])
        for (c, g, l), v in sorted(summary.items(), key=lambda kv: (GROUP_ORDER.index(kv[0][1]), kv[0][0])):
            w.writerow([c, g, l, v['units'], v['buildings'], v['portraits']])
            log(f'  {g:8s} {c:45s} units={v["units"]:5d} buildings={v["buildings"]:5d} '
                f'portraits={v["portraits"]:5d}')

    n_death, n_anim, n_portrait = nest_unit_folders(models, deaths=not args.flat_deaths,
                                                    anims=not args.no_extra_anims,
                                                    portraits=not args.flat_portraits)
    if n_death:
        log(f'Death models: {n_death} folders go in a subfolder of their unit')
    if n_anim:
        log(f'Animation libraries: {n_anim} folders of .m3a files go with the unit they animate')
    n_pf = len({(m.cat, m.rel.lower().rsplit('/', 1)[0]) for m in models
                if m.cat == 'portraits' or 'portrait' in m.rel.lower()})
    if n_pf:
        log(f'Portraits: {n_portrait} of {n_pf} portrait folders go in a subfolder of their unit')
    if args.classify == 'data':
        log('Sorting models by the game data that uses them ...')
        os.makedirs(os.path.join(args.out, '_work'), exist_ok=True)
        refs = load_model_refs(storage, game_data, os.path.join(args.out, '_work', 'model_refs.json'), log)
        if refs['paths'] or refs['ids']:
            classify_by_data(models, refs, log)
        else:
            log('  ! no model references found in the game data: sorting by storage location instead')
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
    # a death subfolder's unit folder is spelled as the unit's own output folder is
    for k in sorted(display, key=len):
        head, sep, tail = k.rpartition(os.sep)
        if sep and head in display:
            display[k] = display[head] + sep + display[k].rpartition(os.sep)[2]
    folders = {display[k]: v for k, v in folders.items()}
    if args.filter:
        rx = re.compile(args.filter, re.I)
        folders = {k: v for k, v in folders.items() if rx.search(k.replace('\\', '/'))}

    with open(os.path.join(args.out, '_plan.tsv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f, delimiter='\t', lineterminator='\n')
        w.writerow(['output_folder', 'storage_path', 'filed_as', 'stored_in', 'used_by', 'found_by'])
        for k, ms in folders.items():
            for m in ms:
                w.writerow([k, m.path, m.out_group, m.container,
                            ' '.join(m.refs) if m.refs is not None else '(not checked)', m.ref_how])
    n_m3 = sum(1 for ms in folders.values() for m in ms if m.ext == 'm3')
    log(f'Plan: {len(folders)} output folders, {n_m3} .m3 models (see _plan.tsv)')

    # stand-in texture for terrain materials (see "Terrain materials" above)
    catalogue = load_terrain_catalogue(storage, terrain_data, melee_order, log)
    with open(os.path.join(args.out, '_terrain_textures.tsv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f, delimiter='\t', lineterminator='\n')
        w.writerow(['id', 'texture', 'normal_map', 'container'])
        for tid in sorted(catalogue, key=str.lower):
            e = catalogue[tid]
            w.writerow([tid, e['diffuse'], e.get('normal', ''), e['container']])
    choice = args.terrain_texture.strip()
    terrain = None
    if choice.lower() == 'auto':
        terrain = pick_terrain_texture(catalogue)
    elif choice.lower() != 'none':
        terrain = next((e for k, e in catalogue.items() if k.lower() == choice.lower()), None)
        if terrain is None:
            if re.search(r'[\\/]|\.(dds|tga|png)$', choice, re.I):
                terrain = {'id': os.path.splitext(re.split(r'[\\/]', choice)[-1])[0], 'diffuse': choice}
            else:
                log(f'  ! --terrain-texture: no terrain texture "{choice}" (see _terrain_textures.tsv)')
    if terrain is not None and not resolve_texture(terrain['diffuse'], textures, '', melee_order):
        log(f'  ! terrain texture {terrain["diffuse"]} not found in the game files')
        terrain = None
    if terrain is not None:
        log(f'Terrain materials: stand-in texture "{terrain["id"]}" ({terrain["diffuse"]}), '
            f'{len(catalogue)} available (see _terrain_textures.tsv)')
    else:
        log('Terrain materials: plain earth colour (no stand-in texture)')
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

    def add_texture(ref, container, tex_map, used_png_names, out_folder, no_alpha=False):
        """Convert the texture a model references (if not done yet) and add it to the folder's
        texture map. Returns its output file name, or None if it can't be found / read."""
        key = ref.replace('/', '\\').strip().lower()
        if key in tex_map:
            return os.path.basename(tex_map[key][1])
        spath = resolve_texture(ref, textures, container, melee_order)
        if not spath:
            return None
        png = get_png(spath)
        if not png:
            return None
        if no_alpha:
            png = png_without_alpha(png)
        name = os.path.splitext(spath.split('/')[-1])[0]
        name = pretty(name, os.path.splitext(ref.replace('\\', '/').split('/')[-1])[0])
        fname = name + '.png'
        if used_png_names.get(fname.lower(), spath) != spath:
            fname = f'{name}_{hashlib.sha1(spath.encode()).hexdigest()[:6]}.png'
        used_png_names[fname.lower()] = spath
        tex_map[key] = (png, os.path.join(out_folder, fname))
        return fname

    def png_without_alpha(png):
        """Terrain textures keep a lighting mask in their alpha channel, not transparency."""
        dst = png[:-4] + '__rgb.png'
        if not os.path.isfile(dst) and os.path.isfile(png):
            from PIL import Image
            with Image.open(png) as im:
                im.convert('RGB').save(dst)
        return dst if os.path.isfile(dst) else png

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
            elif a.nest and m3s:    # a library from another folder: the unit's main model
                main = [m for m in m3s if not m.nest and os.path.splitext(m.rel.split('/')[-1])[0].lower()
                        == a.nest[0].lower()]
                m3a_owner[a.path] = (main or sorted(m3s, key=lambda m: len(m.rel)))[0]
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
                add_texture(ref, m.container, tex_map, used_png_names, out_folder)

            terrain_job = None
            if m3_has_terrain_material(data):
                terrain_job = {'name': 'colour', 'tile': args.terrain_tile}
                if terrain is not None:
                    terrain_job['name'] = terrain['id']
                    for role in ('diffuse', 'normal'):
                        ref = terrain.get(role)
                        dst_name = add_texture(ref, m.container, tex_map, used_png_names, out_folder,
                                               no_alpha=(role == 'diffuse')) if ref else None
                        if dst_name:
                            terrain_job[role] = ref
                    if 'diffuse' not in terrain_job:
                        terrain_job['name'] = 'colour'

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
            if terrain_job is not None:
                job_models[-1]['terrain'] = terrain_job
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
               'obj': not args.no_obj, 'glb': not args.no_glb, 'team_colors': args.team_colors,
               'glb_texture_anims': args.glb_texture_anims,
               'normal_maps': args.normal_maps,
               'converter_version': CONVERTER_VERSION, 'blender_version': bl_version}

    def expected_outputs(m):
        base = os.path.splitext(m['dae'])[0]
        outs = [m['dae']]
        if options['obj']:
            outs += [base + '.obj', base + '.mtl']
        if options['glb']:
            outs.append(base + '.glb')
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
                    warnings=len(r.get('warnings', [])), terrain=r.get('terrain', ''),
                    error=err.strip().splitlines()[-1] if err else ''))
            log(f'  [{done}/{len(jobs)}] {job["folder"]}: {oks}/{len(job["models"])} ok ({how})')

    # ---- 5. report
    fields = ['folder', 'model', 'status', 'dae', 'animations', 'meshes', 'bones',
              'missing_textures', 'warnings', 'terrain', 'error']
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
