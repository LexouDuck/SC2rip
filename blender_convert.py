"""
Blender-side worker: converts StarCraft II .m3 models to COLLADA (.dae).

Runs INSIDE Blender 4.5 LTS (the last Blender with built-in COLLADA export):

    blender -b --factory-startup --python blender_convert.py -- --job job.json

job.json:
{
  "m3studio": "/path/to/m3studio",          # folder containing M3Studio's __init__.py
  "models": [
     {"m3": "/work/raw/.../Marine.m3",
      "m3a": ["/work/raw/.../Marine_Anims.m3a"],   # optional extra animation files
      "dae": "/out/Units/Terran/Marine/Marine.dae"}
  ],
  "textures": {"assets\\textures\\marine_diffuse.dds": "/out/.../Marine_Diffuse.png"},
  "options": {...},
  "result": "/work/jobs/xxx.result.json"
}

Animation is not exported through Blender's COLLADA animation exporter (multi-action export is
broken in 4.4+/4.5 and it takes minutes on large rigs); instead the static file is exported by
Blender and the joint matrices are sampled and injected here (see inject_animations()).
"""
import os
import sys
import json
import re
import types
import time
import traceback
import importlib
import xml.etree.ElementTree as ET

import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sc2_material as sm  # noqa: E402


# --------------------------------------------------------------------------------------
# M3Studio loading
# --------------------------------------------------------------------------------------

def load_m3studio(addon_dir):
    """Import and register M3Studio from `addon_dir` so it works in background mode.

    M3Studio's viewport-drawing module creates GPU shaders at import time, which raises
    "GPU functions for drawing are not available in background mode" under `blender -b`.
    We replace that one module with a no-op stub before importing the package.
    """
    addon_dir = os.path.abspath(addon_dir)
    parent, pkg = os.path.split(addon_dir.rstrip('/\\'))
    if pkg in sys.modules and hasattr(sys.modules[pkg], 'io_m3_import'):
        return sys.modules[pkg]
    if parent not in sys.path:
        sys.path.insert(0, parent)

    stub = types.ModuleType(pkg + '.bl_graphics_draw')
    stub.draw = lambda *a, **k: None
    sys.modules[pkg + '.bl_graphics_draw'] = stub

    mod = importlib.import_module(pkg)
    try:
        mod.register()
    except ValueError as e:  # already registered
        if 'already registered' not in str(e):
            raise
    importlib.import_module(pkg + '.io_m3_import')
    return mod


# --------------------------------------------------------------------------------------
# Scene helpers
# --------------------------------------------------------------------------------------

def clear_scene():
    """Remove everything from the current file, keeping registered add-on classes."""
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for coll_name in ('objects', 'meshes', 'armatures', 'actions', 'materials', 'images',
                      'cameras', 'lights', 'curves', 'node_groups', 'textures', 'collections'):
        coll = getattr(bpy.data, coll_name, None)
        if coll is None:
            continue
        for item in list(coll):
            try:
                coll.remove(item)
            except Exception:
                pass
    scene = bpy.context.scene
    scene.render.fps = 30
    scene.frame_start, scene.frame_end, scene.frame_current = 0, 1, 0


def norm_tex_key(path):
    """Normalise an M3 bitmap reference to the key format used by the job's texture map."""
    return path.replace('/', '\\').strip().lower()


# --------------------------------------------------------------------------------------
# Materials
# --------------------------------------------------------------------------------------

# --------------------------------------------------------------------------------------
# Animations
# --------------------------------------------------------------------------------------

BONE_CHANNELS = (('location', 3), ('rotation_quaternion', 4), ('scale', 3))
CONSTANT = 0  # KeyframePoint.interpolation enum value ('CONSTANT'=0, 'LINEAR'=1, 'BEZIER'=2)
TRANSFORM_SUFFIXES = ('.location', '.rotation_quaternion', '.scale', '.rotation_euler')


def fcurves_of(action):
    """F-curves of an action (Blender 4.4+ slotted actions + legacy API)."""
    try:
        return list(action.fcurves)
    except Exception:
        out = []
        for layer in getattr(action, 'layers', []):
            for strip in layer.strips:
                for bag in getattr(strip, 'channelbags', []):
                    out.extend(bag.fcurves)
        return out


def is_layer_channel(path):
    """Animated material properties the .blend materials can reproduce."""
    if path.startswith('m3_materiallayers['):
        return path.rsplit('.', 1)[-1] in sm.ANIMATED_LAYER_PROPS
    return path.startswith('m3_materials_') and path.rsplit('.', 1)[-1] in sm.ANIMATED_MATERIAL_PROPS


def is_bone_transform(path):
    return path.startswith('pose.bones[') and path.endswith(TRANSFORM_SUFFIXES)


def collect_sequences(ob):
    """Flatten M3Studio's animation data into one complete key-set per M3 sequence.

    M3Studio imports each sequence ("animation group", e.g. "Stand", "Attack 01") as several
    actions - one per sub-animation (STC), some running concurrently and keying only part of the
    skeleton. A COLLADA clip needs a fully defined skeleton, so per sequence we combine:
        bone defaults (DEFAULTS action)  <  sub-animations in ascending priority order.
    Material-layer channels (scrolling UVs, brightness...) are collected separately in
    'mat_channels' - COLLADA can't carry them, but the .blend can. Particle etc. are dropped.

    Per-bone render flags ("batching": whether meshes bound to the bone are drawn) are collected as
    'vis_channels'; vis_defaults holds their default values.

    Returns (defaults, sequences, layer_defaults, vis_defaults) where
        defaults       = {(data_path, index): value}            bone channels
        layer_defaults = {(data_path, index): value}            material-layer channels
        sequences = [{'name', 'length', 'looping', 'source_frames',
                      'channels': {key: [(f, v, interp)]}, 'mat_channels': {...}}]
    with frames relative to the start of the sequence.
    """
    defaults, layer_defaults = {}, {}
    vis_defaults = {pb.name: bool(getattr(pb, 'm3_batching', True)) for pb in ob.pose.bones}
    if ob.m3_animations_default:
        for fc in fcurves_of(ob.m3_animations_default):
            if not len(fc.keyframe_points):
                continue
            if fc.data_path.endswith('.m3_batching') and fc.data_path.startswith('pose.bones['):
                vis_defaults[fc.data_path.split('"')[1]] = fc.keyframe_points[0].co[1] > 0.5
            elif is_bone_transform(fc.data_path):
                defaults[(fc.data_path, fc.array_index)] = fc.keyframe_points[0].co[1]
            elif is_layer_channel(fc.data_path):
                layer_defaults[(fc.data_path, fc.array_index)] = fc.keyframe_points[0].co[1]
    for pb in ob.pose.bones:  # anything missing from DEFAULTS falls back to the current pose
        for prop, n in BONE_CHANNELS:
            path = pb.path_from_id(prop)
            for i in range(n):
                defaults.setdefault((path, i), getattr(pb, prop)[i])

    sequences, used = [], set()
    for g in ob.m3_animation_groups:
        f0, f1 = int(g.frame_start), int(g.frame_end)
        if f1 <= f0:
            f1 = f0 + 1
        channels, mat_channels, vis_channels = {}, {}, {}
        for a in sorted([a for a in g.animations if a.action], key=lambda a: a.priority):
            for fc in fcurves_of(a.action):
                if fc.data_path.endswith('.m3_batching') and fc.data_path.startswith('pose.bones['):
                    pts = [(kp.co[0] - f0, kp.co[1] > 0.5) for kp in fc.keyframe_points
                           if f0 - 0.001 <= kp.co[0] <= f1 + 0.001]
                    if pts:
                        vis_channels[fc.data_path.split('"')[1]] = pts
                    continue
                if is_bone_transform(fc.data_path):
                    target = channels
                elif is_layer_channel(fc.data_path):
                    target = mat_channels
                else:
                    continue
                n = len(fc.keyframe_points)
                co = [0.0] * (2 * n)
                ip = [0] * n
                fc.keyframe_points.foreach_get('co', co)
                fc.keyframe_points.foreach_get('interpolation', ip)
                pts = [(co[2 * i] - f0, co[2 * i + 1], ip[i]) for i in range(n)
                       if f0 - 0.001 <= co[2 * i] <= f1 + 0.001]
                if pts:
                    target[(fc.data_path, fc.array_index)] = pts  # higher priority overrides
        name = (g.name or 'Anim').strip()
        base, k = name, 2
        while name in used:
            name, k = f'{base}_{k}', k + 1
        used.add(name)
        span = f1 - f0
        looping = not g.not_looping
        # M3 sequence ends are exclusive (M3Studio plays start .. end-1). The key sitting exactly on
        # the end is usually a loop-wrap copy of the first pose, which the game never shows.
        #  - looping: keep it (so the last frames blend smoothly into the loop) but stop before it
        #  - non-looping (deaths...): if the end keys merely repeat the opening pose, drop them and
        #    hold the final pose; if they hold a real final pose, keep and show that frame
        play = span - 1
        if not looping:
            if _end_keys_are_wrap(channels, span):
                for d in (channels, mat_channels, vis_channels):
                    for key in list(d):
                        d[key] = [p for p in d[key] if p[0] < span - 0.001] or d[key][:1]
            elif any(abs(pts[-1][0] - span) < 0.001 for pts in channels.values()):
                play = span
        sequences.append({'name': name, 'length': span, 'play_length': max(play, 1), 'looping': looping,
                          'source_frames': [f0, f1], 'channels': channels, 'mat_channels': mat_channels,
                          'vis_channels': vis_channels})
    return defaults, sequences, layer_defaults, vis_defaults


def _end_keys_are_wrap(channels, span, tol=1e-3):
    """True if the keys on the sequence's last frame just repeat its first frame (a loop-wrap key)
    while the pose right before them is different."""
    at_end, differs = 0, False
    for pts in channels.values():
        if len(pts) < 3 or abs(pts[-1][0] - span) > 0.001:
            continue
        at_end += 1
        if abs(pts[-1][1] - pts[0][1]) > tol:
            return False            # a real final pose
        if abs(pts[-2][1] - pts[-1][1]) > tol:
            differs = True
    return at_end > 0 and differs


def build_action(name, defaults, placed):
    """Create one action from sequences placed at frame offsets: placed = [(sequence, offset)]."""
    act = bpy.data.actions.new(name)
    act.use_fake_user = True
    fcs = act.fcurves
    for key, default in defaults.items():
        path, idx = key
        group = path.split('"')[1] if '"' in path else ''
        pts = []
        for seq, off in placed:
            seq_pts = seq['channels'].get(key)
            if seq_pts:
                # make sure each segment is pinned at both ends so neighbours don't bleed in
                if seq_pts[0][0] > 0:
                    pts.append((off, seq_pts[0][1], CONSTANT))
                pts.extend((f + off, v, i) for f, v, i in seq_pts)
                if seq_pts[-1][0] < seq['length']:
                    pts.append((off + seq['length'], seq_pts[-1][1], CONSTANT))
            else:
                pts.append((off, default, CONSTANT))
                pts.append((off + seq['length'], default, CONSTANT))
        # dedupe identical frames (keep last)
        dedup = {}
        for f, v, i in pts:
            dedup[round(f, 4)] = (f, v, i)
        pts = [dedup[k] for k in sorted(dedup)]
        fc = fcs.new(path, index=idx, action_group=group)
        fc.keyframe_points.add(len(pts))
        fc.keyframe_points.foreach_set('co', [x for f, v, _ in pts for x in (f, v)])
        fc.keyframe_points.foreach_set('interpolation', [i for _, _, i in pts])
        fc.update()
    return act


def drop_m3studio_actions(ob, keep=()):
    if ob.animation_data:
        ob.animation_data.action = None
    for a in list(bpy.data.actions):
        if a not in keep:
            bpy.data.actions.remove(a)


def assign_action(ob, act, frame_start, frame_end):
    ob.animation_data_create()
    ob.animation_data.action = act
    try:  # Blender 4.4+: make sure a slot is bound so the exporter actually evaluates it
        if ob.animation_data.action_slot is None and len(act.slots):
            ob.animation_data.action_slot = act.slots[0]
    except AttributeError:
        pass
    sc = bpy.context.scene
    sc.frame_start, sc.frame_end = int(frame_start), int(frame_end)
    sc.frame_set(int(frame_start))


# --------------------------------------------------------------------------------------
# Mesh visibility ("batching")
# --------------------------------------------------------------------------------------

class Visibility:
    """SC2 shows/hides geometry per animation: every mesh batch is bound to a bone, and that bone's
    (animated) render flag decides whether the batch is drawn. Death models use it to swap debris
    sets between death variants.

    .blend: the mesh objects get animated hide_viewport / hide_render.
    COLLADA has no visibility, so there a hidden mesh is collapsed by shrinking the bones that only
    hidden meshes use (and only when that doesn't disturb any visible mesh)."""

    def __init__(self, ob, meshes, batch_bones, vis_defaults):
        self.meshes = meshes
        self.batch_bones = {m.name: [b for b in batch_bones.get(m, []) if b] for m in meshes}
        self.defaults = vis_defaults
        self.used_by = {}
        self.bones_of = {}
        for m in meshes:
            names = {g.index: g.name for g in m.vertex_groups}
            used = set()
            for v in m.data.vertices:
                for g in v.groups:
                    if g.weight > 1e-4 and g.group in names:
                        used.add(names[g.group])
            self.bones_of[m.name] = used
            for b in used:
                self.used_by.setdefault(b, set()).add(m.name)
        self.descendants = {b.name: {c.name for c in b.children_recursive} for b in ob.data.bones}
        self._cache = {}

    def bone_visible(self, seq, bone, rel):
        pts = seq.get('vis_channels', {}).get(bone)
        if not pts:
            return self.defaults.get(bone, True)
        val = pts[0][1]
        for f, v in pts:
            if f <= rel + 1e-6:
                val = v
            else:
                break
        return val

    def mesh_visible(self, seq, mesh_name, rel):
        bones = self.batch_bones.get(mesh_name)
        return True if not bones else any(self.bone_visible(seq, b, rel) for b in bones)

    def is_animated(self, sequences):
        bound = {b for bs in self.batch_bones.values() for b in bs}
        return any(not self.defaults.get(b, True) for b in bound) or \
            any(b in seq.get('vis_channels', {}) for seq in sequences for b in bound)

    def hidden(self, seq, rel):
        return frozenset(m.name for m in self.meshes if not self.mesh_visible(seq, m.name, rel))

    def collapse_set(self, hidden):
        """Bones that can be shrunk to hide `hidden` meshes without touching visible ones."""
        if hidden in self._cache:
            return self._cache[hidden]
        h = set(hidden)
        while True:
            visible = {m.name for m in self.meshes} - h
            ok = {b for b, users in self.used_by.items()
                  if users <= h and not any(self.used_by.get(d, set()) & visible for d in self.descendants.get(b, ()))}
            h2 = {m for m in h if self.bones_of.get(m) and self.bones_of[m] <= ok}
            if h2 == h:
                break
            h = h2
        result = frozenset(b for b in ok if self.used_by[b] <= h)
        self._cache[hidden] = result
        return result

    def collapse_fn(self, placed):
        """frame -> set of bones to collapse, for sequences placed at offsets on one timeline."""
        def fn(frame):
            seq, off = placed[0]
            for s_, o in placed:
                if frame >= o - 1e-6:
                    seq, off = s_, o
            return self.collapse_set(self.hidden(seq, min(frame - off, seq['length'])))
        return fn

    def _hide_keys(self, m, placed):
        keys = []
        for seq, off in placed:
            events = {0.0}
            for b in self.batch_bones[m.name]:
                events.update(f for f, _ in seq.get('vis_channels', {}).get(b, []) if 0 <= f <= seq['length'])
            for rel in sorted(events):
                keys.append((off + rel, 0.0 if self.mesh_visible(seq, m.name, rel) else 1.0))
        return keys

    def keyframe_objects(self, placed, all_placed=None, out=None, name='{mesh}_Visibility'):
        """Animated hide_viewport / hide_render on the mesh objects (for the .blend). all_placed decides
        which meshes are animated (default: placed); out receives (mesh object, action)."""
        for m in self.meshes:
            if not self.batch_bones.get(m.name):
                continue
            keys = self._hide_keys(m, placed)
            if all(v == 0.0 for _, v in (keys if all_placed is None else self._hide_keys(m, all_placed))):
                continue
            act = bpy.data.actions.new(name.format(mesh=m.name))
            if out is not None:
                out.append((m, act))
            for prop in ('hide_viewport', 'hide_render'):
                fc = act.fcurves.new(prop)
                fc.keyframe_points.add(len(keys))
                fc.keyframe_points.foreach_set('co', [x for k in keys for x in k])
                fc.keyframe_points.foreach_set('interpolation', [0] * len(keys))
                fc.update()
            m.animation_data_create()
            m.animation_data.action = act
            try:
                if m.animation_data.action_slot is None and len(act.slots):
                    m.animation_data.action_slot = act.slots[0]
            except AttributeError:
                pass


# --------------------------------------------------------------------------------------
# COLLADA export
# --------------------------------------------------------------------------------------

def export_collada(filepath, opts, has_anims):
    kwargs = dict(
        filepath=filepath,
        check_existing=False,
        apply_modifiers=False,
        selected=True,
        include_children=False,
        include_armatures=True,
        include_shapekeys=False,
        deform_bones_only=False,
        include_animations=has_anims,
        include_all_actions=False,  # broken in 4.4+/4.5 (only the active action is written)
        export_animation_type_selection='sample' if opts.get('anim_sample', True) else 'keys',
        sampling_rate=int(opts.get('sampling_rate', 1)),
        keep_smooth_curves=False,
        keep_keyframes=False,
        active_uv_only=False,
        use_texture_copies=False,
        triangulate=True,
        use_object_instantiation=False,
        use_blender_profile=True,
        sort_by_name=False,
        keep_bind_info=False,
        limit_precision=False,
    )
    # drop kwargs this Blender build doesn't know about
    props = bpy.ops.wm.collada_export.get_rna_type().properties.keys()
    kwargs = {k: v for k, v in kwargs.items() if k in props}
    res = bpy.ops.wm.collada_export(**kwargs)
    if 'FINISHED' not in res:
        raise RuntimeError(f'collada_export returned {res}')


COLLADA_NS = 'http://www.collada.org/2005/11/COLLADASchema'


def sample_joint_matrices(ob, frames, collapse_fn=None):
    """Per-frame joint transforms, as COLLADA wants them: each bone's pose matrix relative to its
    parent bone (root bones: relative to the armature). Same convention as Blender's exporter.

    Blender 4.5's own animation sampler needs minutes for a big SC2 rig; evaluating the pose here
    takes about a millisecond per frame."""
    sc = bpy.context.scene
    bones = list(ob.pose.bones)
    # don't re-skin the meshes on every frame while sampling
    mods = [m for o in bpy.data.objects for m in o.modifiers if m.type == 'ARMATURE' and m.show_viewport]
    for m in mods:
        m.show_viewport = False
    out = {pb.name: [] for pb in bones}
    try:
        for f in frames:
            fi = int(f)
            sc.frame_set(fi, subframe=f - fi)
            collapse = collapse_fn(f) if collapse_fn else ()
            for pb in bones:
                m = pb.matrix if pb.parent is None else pb.parent.matrix.inverted_safe() @ pb.matrix
                if pb.name in collapse:   # hidden: shrink the bone (and what hangs off it) to a point
                    out[pb.name].append((0, 0, 0, m[0][3], 0, 0, 0, m[1][3], 0, 0, 0, m[2][3], 0, 0, 0, 1))
                    continue
                out[pb.name].append((m[0][0], m[0][1], m[0][2], m[0][3], m[1][0], m[1][1], m[1][2], m[1][3],
                                     m[2][0], m[2][1], m[2][2], m[2][3], m[3][0], m[3][1], m[3][2], m[3][3]))
    finally:
        for m in mods:
            m.show_viewport = True
    return out


def _fmt(values):
    return ' '.join('%.7g' % v for v in values)


def inject_animations(dae_path, ob, frame_start, frame_end, fps, step=1, collapse_fn=None):
    """Write a <library_animations> with one matrix animation per moving joint into a .dae that
    Blender exported without animation (the static pose must be the pose at `frame_start`)."""
    frames = list(range(int(frame_start), int(frame_end) + 1, max(1, int(step))))
    if frames[-1] != int(frame_end):
        frames.append(int(frame_end))
    mats = sample_joint_matrices(ob, frames, collapse_fn)

    tree = load_dae(dae_path)
    root = tree.getroot()
    ns = {'c': COLLADA_NS}
    q = lambda tag: f'{{{COLLADA_NS}}}{tag}'
    joint_ids = {n.get('name'): n.get('id') for n in root.iterfind('.//c:node[@type="JOINT"]', ns)}

    old = root.find('c:library_animations', ns)
    if old is not None:
        root.remove(old)
    lib = ET.Element(q('library_animations'))
    container = ET.SubElement(lib, q('animation'), {'id': f'action_container-{ob.name}', 'name': ob.name})
    times = _fmt((f - frames[0]) / fps for f in frames)
    n = len(frames)
    written = 0
    for bone, ms in mats.items():
        nid = joint_ids.get(bone) or joint_ids.get(_XML_INVALID.sub('_', bone))
        if nid is None:
            continue
        first = ms[0]
        if all(max(abs(a - b) for a, b in zip(first, m)) < 1e-6 for m in ms):
            continue  # constant: the static node matrix already has this value
        aid = f'{nid}_pose_matrix'
        an = ET.SubElement(container, q('animation'), {'id': aid, 'name': ob.name})

        def source(suffix, array_tag, text, count, stride, pname, ptype):
            src = ET.SubElement(an, q('source'), {'id': f'{aid}-{suffix}'})
            arr = ET.SubElement(src, q(array_tag), {'id': f'{aid}-{suffix}-array', 'count': str(count)})
            arr.text = text
            tc = ET.SubElement(src, q('technique_common'))
            acc = ET.SubElement(tc, q('accessor'), {'source': f'#{aid}-{suffix}-array',
                                                     'count': str(n), 'stride': str(stride)})
            ET.SubElement(acc, q('param'), {'name': pname, 'type': ptype})

        source('input', 'float_array', times, n, 1, 'TIME', 'float')
        source('output', 'float_array', ' '.join(_fmt(m) for m in ms), n * 16, 16, 'TRANSFORM', 'float4x4')
        source('interpolation', 'Name_array', ' '.join(['LINEAR'] * n), n, 1, 'INTERPOLATION', 'name')
        smp = ET.SubElement(an, q('sampler'), {'id': f'{aid}-sampler'})
        for sem, suf in (('INPUT', 'input'), ('OUTPUT', 'output'), ('INTERPOLATION', 'interpolation')):
            ET.SubElement(smp, q('input'), {'semantic': sem, 'source': f'#{aid}-{suf}'})
        ET.SubElement(an, q('channel'), {'source': f'#{aid}-sampler', 'target': f'{nid}/transform'})
        written += 1

    if written:
        kids = list(root)
        vs = root.find('c:library_visual_scenes', ns)
        root.insert(kids.index(vs) if vs is not None else len(kids), lib)
    tree.write(dae_path, xml_declaration=True, encoding='utf-8')
    return written


_XML_INVALID = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]')
_NCNAME_BAD = re.compile(r'[^A-Za-z0-9._-]')


def load_dae(path):
    """Parse a .dae, tolerating characters that aren't legal in XML (some SC2 names contain control
    characters or non-UTF-8 bytes, which Blender writes out verbatim). They are replaced the same
    way everywhere, so id/sid references stay consistent."""
    ET.register_namespace('', COLLADA_NS)
    with open(path, 'rb') as f:
        text = f.read().decode('utf-8', errors='replace')
    text = _XML_INVALID.sub('_', text).replace('�', '_')
    text = re.sub(r'^<\?xml[^>]*\?>', '', text.lstrip())
    return ET.ElementTree(ET.fromstring(text))


def ncname(value):
    """COLLADA 1.4.1 requires most 'name' attributes to be XML NCNames (no spaces, parentheses...)."""
    v = _NCNAME_BAD.sub('_', value) or '_'
    return v if (v[0].isalpha() or v[0] == '_') else '_' + v


def postprocess_dae(dae_path, clips=None, fps=30, material_info=None):
    """Clean up Blender's .dae so it is valid COLLADA 1.4.1 and complete:
      * texture paths relative to the .dae
      * transparency / emission textures Blender's exporter can't write (material_info:
        {material name: {'alpha': bool, 'glow': bool}})
      * <library_animation_clips> describing each sequence (clips on the timeline, frames at fps)
      * NCName-safe 'name' attributes, no empty libraries
    Returns a list of problems that could not be fixed (e.g. duplicate ids)."""
    tree = load_dae(dae_path)
    root = tree.getroot()
    ns = {'c': COLLADA_NS}
    q = lambda tag: f'{{{COLLADA_NS}}}{tag}'
    dae_dir = os.path.dirname(os.path.abspath(dae_path))
    problems = []

    for init in root.iterfind('.//c:library_images/c:image/c:init_from', ns):
        if not init.text:
            continue
        p = init.text
        if p.startswith('file://'):
            p = p[7:]
        base = os.path.basename(p.replace('\\', '/'))
        for rel in (base, '../' + base):
            if os.path.isfile(os.path.join(dae_dir, rel)):
                init.text = rel
                break

    # --- materials: transparency + emission from the diffuse texture
    if material_info:
        effects = {e.get('id'): e for e in root.iterfind('.//c:library_effects/c:effect', ns)}
        for mat in root.iterfind('.//c:library_materials/c:material', ns):
            info = material_info.get(mat.get('name'))
            inst = mat.find('c:instance_effect', ns)
            if not info or inst is None:
                continue
            eff = effects.get(inst.get('url', '#')[1:])
            if eff is None:
                continue
            tech = eff.find('.//c:technique', ns)
            shading = next((c for c in tech if c.tag in (q('lambert'), q('phong'), q('blinn'), q('constant'))), None) \
                if tech is not None else None
            if shading is None:
                continue
            diff_tex = shading.find('c:diffuse/c:texture', ns)
            if diff_tex is None:
                continue
            attrs = {'texture': diff_tex.get('texture'), 'texcoord': diff_tex.get('texcoord', 'UVMap')}
            emis_attrs = attrs
            if info.get('emission_file'):
                # a separate glow texture: declare image + surface + sampler for it
                img_id = ncname(os.path.splitext(info['emission_file'])[0]) + '_emission_png'
                lib_images = root.find('c:library_images', ns)
                if lib_images is None:
                    lib_images = ET.Element(q('library_images'))
                    root.insert(list(root).index(root.find('c:library_effects', ns)), lib_images)
                if lib_images.find(f"c:image[@id='{img_id}']", ns) is None:
                    im = ET.SubElement(lib_images, q('image'), {'id': img_id, 'name': img_id})
                    ET.SubElement(im, q('init_from')).text = info['emission_file']
                prof = eff.find('c:profile_COMMON', ns)
                first_tech = prof.find('c:technique', ns)
                pos = list(prof).index(first_tech)
                surf = ET.Element(q('newparam'), {'sid': img_id + '-surface'})
                se = ET.SubElement(surf, q('surface'), {'type': '2D'})
                ET.SubElement(se, q('init_from')).text = img_id
                samp = ET.Element(q('newparam'), {'sid': img_id + '-sampler'})
                sa = ET.SubElement(samp, q('sampler2D'))
                ET.SubElement(sa, q('source')).text = img_id + '-surface'
                prof.insert(pos, surf)
                prof.insert(pos + 1, samp)
                emis_attrs = {'texture': img_id + '-sampler', 'texcoord': attrs['texcoord']}
            if info.get('glow'):
                em = shading.find('c:emission', ns)
                if em is None:
                    em = ET.Element(q('emission'))
                    shading.insert(0, em)
                for c in list(em):
                    em.remove(c)
                ET.SubElement(em, q('texture'), emis_attrs)
            if info.get('alpha'):
                for tag in ('transparent', 'transparency'):
                    old_el = shading.find(f'c:{tag}', ns)
                    if old_el is not None:
                        shading.remove(old_el)
                tr = ET.Element(q('transparent'), {'opaque': 'A_ONE'})
                ET.SubElement(tr, q('texture'), attrs)
                trv = ET.Element(q('transparency'))
                ET.SubElement(trv, q('float'), {'sid': 'transparency'}).text = '1'
                # schema order: emission ambient diffuse specular shininess reflective reflectivity
                #               transparent transparency index_of_refraction
                order = ['emission', 'ambient', 'diffuse', 'specular', 'shininess', 'reflective', 'reflectivity']
                kids = list(shading)
                pos = max([i + 1 for i, c in enumerate(kids) if c.tag in [q(t) for t in order]] or [0])
                shading.insert(pos, tr)
                shading.insert(pos + 1, trv)

    # --- animation clips
    lib_anims = root.find('c:library_animations', ns)
    if clips and lib_anims is not None:
        top_ids = [a.get('id') for a in lib_anims.findall('c:animation', ns) if a.get('id')]
        old = root.find('c:library_animation_clips', ns)
        if old is not None:
            root.remove(old)
        lib_clips = ET.Element(q('library_animation_clips'))
        used = set()
        for c in clips:
            cid = ncname(c['name'])
            base, k = cid, 2
            while cid in used:
                cid, k = f'{base}_{k}', k + 1
            used.add(cid)
            el = ET.SubElement(lib_clips, q('animation_clip'), {
                'id': 'clip_' + cid, 'name': cid,
                'start': f"{c['frame_start'] / fps:.6f}", 'end': f"{c['frame_end'] / fps:.6f}"})
            for aid in top_ids:
                ET.SubElement(el, q('instance_animation'), {'url': '#' + aid})
        kids = list(root)
        root.insert(kids.index(lib_anims) + 1, lib_clips)

    # --- schema hygiene
    for el in root.iter():
        if 'name' in el.attrib:
            el.set('name', ncname(el.get('name')))
    for lib in list(root):
        if lib.tag.startswith(q('library_')) and len(lib) == 0:
            root.remove(lib)
    ids = {}
    for el in root.iter():
        i = el.get('id')
        if i:
            ids[i] = ids.get(i, 0) + 1
    dups = sorted(i for i, n in ids.items() if n > 1)
    if dups:
        problems.append('duplicate ids in .dae: ' + ', '.join(dups[:5]))

    ET.indent(tree, space='  ')
    tree.write(dae_path, xml_declaration=True, encoding='utf-8')
    return problems


def safe_name(s):
    return ''.join(ch if ch.isalnum() or ch in ' _-.()' else '_' for ch in s).strip() or 'unnamed'


def write_anim_list(path, model_name, clips, fps):
    lines = [f'# Animations of {model_name} (COLLADA timeline, {fps} fps)',
             '# name\tstart_frame\tend_frame\tstart_sec\tend_sec\tlooping\toriginal_m3_frames']
    for c in clips:
        lines.append(f"{c['name']}\t{c['frame_start']}\t{c['frame_end']}\t"
                     f"{c['frame_start'] / fps:.3f}\t{c['frame_end'] / fps:.3f}\t"
                     f"{'yes' if c['looping'] else 'no'}\t{c['source_frames'][0]}-{c['source_frames'][1]}")
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


TERRAIN_TILE = 4.0   # game units covered by one repeat of a terrain texture


def project_terrain_uvs(mob, tile):
    """Replace the first UV set with a top-down projection (model X/Y / tile), the way terrain
    textures are laid on the ground in game. Terrain materials ignore the mesh's own UVs."""
    me = mob.data
    uv0 = me.uv_layers[0] if me.uv_layers else me.uv_layers.new(name='uv0')
    mw = mob.matrix_world
    coords = [mw @ v.co for v in me.vertices]
    uvs = []
    for loop in me.loops:
        co = coords[loop.vertex_index]
        uvs += (co.x / tile, co.y / tile)
    uv0.data.foreach_set('uv', uvs)
    me.update()


def bake_export_uvs(mob, layer):
    """Overwrite the first UV set with the layer's UVs (its own UV set + rest transform).
    Returns the original coordinates for restore_uvs()."""
    me = mob.data
    if not me.uv_layers:
        return None
    uv0 = me.uv_layers[0]
    n = len(uv0.data)
    orig = [0.0] * (2 * n)
    uv0.data.foreach_get('uv', orig)
    src_layer = me.uv_layers.get(sm.static_uv(layer)[0] or 'uv0') or uv0
    src = [0.0] * (2 * n)
    src_layer.data.foreach_get('uv', src)
    uv0.data.foreach_set('uv', sm.transform_uvs(src, layer))
    me.update()
    return orig


def restore_uvs(backup):
    for mob, orig in backup.items():
        mob.data.uv_layers[0].data.foreach_set('uv', orig)
        mob.data.update()
    backup.clear()


def build_blend_materials(mesh_materials, tex_map, equirect, report, normal_mode, animated_paths,
                          layer_defaults, placed):
    """Swap the export (Principled) materials for SC2-style ones and animate their layers."""
    built, targets_by_mat = {}, {}
    for mob, name, m3mat, layers, mtype in mesh_materials:
        if name not in built:
            if m3mat is not None:
                old = bpy.data.materials.get(name)
                if old is not None:
                    old.name = name + '_export'
                mat, targets = sm.build_sc2_material(name, m3mat, layers, tex_map, equirect, report,
                                                     normal_mode, animated_paths, mtype)
                targets_by_mat[mat] = targets
                built[name] = mat
            else:
                built[name] = bpy.data.materials.get(name)
        mob.data.materials.clear()
        mob.data.materials.append(built[name])
    for m in list(bpy.data.materials):
        if m.name.endswith('_export') and m.users == 0:
            bpy.data.materials.remove(m)
    if placed:
        report['animated_materials'] = sm.build_material_actions(targets_by_mat, layer_defaults, placed,
                                                                 build_action)
    return targets_by_mat


def export_obj(path, meshes):
    for o in bpy.data.objects:
        o.select_set(False)
    for m in meshes:
        m.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    kwargs = dict(filepath=os.path.abspath(path), check_existing=False, export_selected_objects=True,
                  apply_modifiers=False,          # ignore the armature: bind pose
                  export_animation=False, export_uv=True, export_normals=True, export_colors=False,
                  export_materials=True, export_pbr_extensions=False, path_mode='RELATIVE',
                  export_triangulated_mesh=False, export_object_groups=False, export_material_groups=False,
                  export_vertex_groups=False, forward_axis='NEGATIVE_Z', up_axis='Y')
    props = bpy.ops.wm.obj_export.get_rna_type().properties.keys()
    res = bpy.ops.wm.obj_export(**{k: v for k, v in kwargs.items() if k in props})
    if 'FINISHED' not in res:
        raise RuntimeError(f'obj_export returned {res}')
    # Blender names the currently open .blend (an unrelated earlier model) in the MTL header
    mtl = os.path.splitext(os.path.abspath(path))[0] + '.mtl'
    if os.path.isfile(mtl):
        with open(mtl, 'r', encoding='utf-8') as f:
            lines = f.read().split('\n')
        if lines and lines[0].startswith('# Blender'):
            lines[0] = f'# Blender {bpy.app.version_string} MTL File'
        with open(mtl, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))


# --------------------------------------------------------------------------------------
# .blend animations: one action per sequence
# --------------------------------------------------------------------------------------

ALL_ANIMATIONS = 'All Animations'

# Saved in the .blend as a text block that runs when the file opens (Blender asks first, unless
# Preferences > Save & Load > Auto Run Python Scripts is on). Kept free of SC2rip imports.
ANIM_SWITCHER = r"""# SC2rip: animation switcher
#
# Adds an "SC2 Animations" panel to the sidebar (N) of the 3D View, Dope Sheet and NLA editor, tab "SC2".
# Clicking an animation switches every animated part of the model to it (skeleton, material layers,
# visibility of meshes) and sets the scene frame range to it, so playback loops it.
#
# Each animation is also a plain action ("Stand", "Walk", ... and "All Animations", every animation
# end to end as in the .dae): pick one in the Action Editor to see its keys. The action only changes
# the object it is picked for, though; the panel switches them all.
#
# This runs when the file opens if Blender allows it; else run it here: Text > Run Script.
import bpy


def sc2_targets():
    for ob in bpy.data.objects:
        yield 'OB' + ob.name, ob
    for mat in bpy.data.materials:
        if mat.node_tree is not None:
            yield 'NT' + mat.name, mat.node_tree


def sc2_apply_animation(name, scene=None):
    act = bpy.data.actions.get(name)
    if act is None:
        return False
    slots = {s.identifier: s for s in act.slots}
    for ident, idb in sc2_targets():
        slot = slots.get(ident)
        ad = idb.animation_data
        if slot is None:
            if ad is not None and ad.action is not None and ad.action.get('sc2_animation'):
                ad.action = None
            continue
        if ad is None:
            ad = idb.animation_data_create()
        ad.action = act
        ad.action_slot = slot
    sc = scene or bpy.context.scene
    start, end = (int(round(f)) for f in act.frame_range)
    sc.frame_start, sc.frame_end = start, max(start, end)
    sc.frame_set(start)
    sc['sc2_animation'] = name
    sc.timeline_markers.clear()
    for m in act.pose_markers:
        sc.timeline_markers.new(m.name, frame=m.frame)
    return True


class SC2_OT_play_animation(bpy.types.Operator):
    bl_idname = 'sc2.play_animation'
    bl_description = 'Switch the model to this animation and set the frame range to it'
    bl_label = 'SC2 Animation'
    bl_options = {'REGISTER', 'UNDO'}
    name: bpy.props.StringProperty()

    def execute(self, context):
        return {'FINISHED'} if sc2_apply_animation(self.name, context.scene) else {'CANCELLED'}


def sc2_draw(self, context):
    sc = context.scene
    current = sc.get('sc2_animation', '')
    col = self.layout.column(align=True)
    for name in sc.get('sc2_animations', []):
        act = bpy.data.actions.get(name)
        if act is None:
            continue
        length = int(round(act.frame_range[1] - act.frame_range[0]))
        op = col.operator('sc2.play_animation', text=f'{name}  ({length})', depress=(name == current),
                          icon='PLAY' if name == current else ('FILE_REFRESH' if act.use_cyclic else 'ACTION'))
        op.name = name


def sc2_panel(space):
    return type('SC2_PT_animations_' + space.lower(), (bpy.types.Panel,), {
        'bl_label': 'SC2 Animations', 'bl_space_type': space, 'bl_region_type': 'UI',
        'bl_category': 'SC2', 'draw': sc2_draw})


for cls in (SC2_OT_play_animation, sc2_panel('VIEW_3D'), sc2_panel('DOPESHEET_EDITOR'), sc2_panel('NLA_EDITOR')):
    old = getattr(bpy.types, cls.__name__, None)
    if old is not None:
        try:
            bpy.utils.unregister_class(old)
        except RuntimeError:
            pass
    bpy.utils.register_class(cls)
"""


def _copy_fcurves(src, bag):
    groups = {}
    for fc in fcurves_of(src):
        nf = bag.fcurves.new(fc.data_path, index=fc.array_index)
        if fc.group is not None:
            if fc.group.name not in groups:
                groups[fc.group.name] = bag.groups.new(fc.group.name)
            nf.group = groups[fc.group.name]
        n = len(fc.keyframe_points)
        nf.keyframe_points.add(n)
        for attr, size, zero in (('co', 2, 0.0), ('handle_left', 2, 0.0), ('handle_right', 2, 0.0),
                                 ('interpolation', 1, 0), ('handle_left_type', 1, 0), ('handle_right_type', 1, 0)):
            buf = [zero] * (n * size)
            fc.keyframe_points.foreach_get(attr, buf)
            nf.keyframe_points.foreach_set(attr, buf)
        nf.extrapolation = fc.extrapolation
        nf.update()


def merge_slots(name, parts):
    """One action with a slot per animated ID (Blender 4.4+ slotted actions).
    parts = [(id type, slot name, source action)]; the source actions are removed."""
    act = bpy.data.actions.new(name)
    act.use_fake_user = True
    strip = act.layers.new('Layer').strips.new(type='KEYFRAME')
    for id_type, slot_name, src in parts:
        slot = act.slots.new(id_type=id_type, name=slot_name)
        _copy_fcurves(src, strip.channelbag(slot, ensure=True))
        bpy.data.actions.remove(src)
    act['sc2_animation'] = True
    return act


def build_blend_actions(ob, sequences, defaults, layer_defaults, targets_by_mat, vis, placed, clips, report):
    """The .blend's animations as actions: one per sequence plus "All Animations" (every sequence end
    to end, as on the .dae's timeline, with a pose marker per clip). Each action has a slot for every
    animated ID: the armature (bones), the material node trees (layer animations) and the mesh
    objects (visibility). -> action names, in order; None if this Blender has no slotted actions."""
    if not hasattr(bpy.types, 'ActionSlot'):
        return None
    all_placed = [(s_, 0) for s_ in sequences]
    vis_on = vis is not None

    def parts_for(bones_act, placed_, tag):
        parts = [('OBJECT', ob.name, bones_act)]
        mats, meshes = [], []
        if targets_by_mat:
            n = sm.build_material_actions(targets_by_mat, layer_defaults, placed_, build_action,
                                          all_placed=all_placed, out=mats, name='{mat}_' + tag)
            if placed_ is placed:
                report['animated_materials'] = n
        if vis_on:
            vis.keyframe_objects(placed_, all_placed=all_placed, out=meshes, name='{mesh}_' + tag)
        parts += [('NODETREE', mat.name, a) for mat, a in mats]
        parts += [('OBJECT', mob.name, a) for mob, a in meshes]
        return parts

    names = []
    for seq in sequences:
        tag = seq['name']
        act = merge_slots(tag, parts_for(build_action(tag + '_bones', defaults, [(seq, 0)]), [(seq, 0)], tag))
        act.use_frame_range = True
        act.frame_start, act.frame_end = 0, max(seq['play_length'], 0)
        act.use_cyclic = bool(seq.get('looping'))
        names.append(act.name)
    if placed:
        act = merge_slots(ALL_ANIMATIONS, parts_for(build_action('_timeline_bones', defaults, placed), placed,
                                                    'timeline'))
        act.use_frame_range = True
        act.frame_start, act.frame_end = 0, int(clips[-1]['frame_end'])
        for c in clips:
            act.pose_markers.new(c['name']).frame = int(c['frame_start'])
        names.append(act.name)
    return names


def apply_blend_animation(names):
    """Store the switcher script + animation list in the .blend and switch to its default animation
    (Stand if there is one)."""
    ns = {}
    exec(compile(ANIM_SWITCHER, 'sc2_animations.py', 'exec'), ns)
    txt = bpy.data.texts.new('sc2_animations.py')
    txt.write(ANIM_SWITCHER)
    txt.use_module = True
    sc = bpy.context.scene
    sc['sc2_animations'] = names
    default = next((n for n in names if n.lower() == 'stand'), names[0])
    ns['sc2_apply_animation'](default, sc)


def setup_viewer(ob, meshes, clips, timeline_act):
    """Make the saved .blend open ready to look at: Material Preview shading (HDRI lighting, so
    normal/specular maps show), view framed on the model, a sun + world for Rendered mode, and the
    animation timeline with one named marker per clip."""
    from mathutils import Vector, Quaternion
    sc = bpy.context.scene

    # animation: the full timeline action + markers at each clip start
    if timeline_act is not None:
        ob.animation_data_create()
        ob.animation_data.action = timeline_act
        try:
            if ob.animation_data.action_slot is None and len(timeline_act.slots):
                ob.animation_data.action_slot = timeline_act.slots[0]
        except AttributeError:
            pass
        sc.timeline_markers.clear()
        for c in clips:
            sc.timeline_markers.new(c['name'], frame=int(c['frame_start']))
        sc.frame_start, sc.frame_end = 0, int(clips[-1]['frame_end'])
    sc.frame_set(0)

    # bounds of the model (evaluated at frame 0)
    dg = bpy.context.evaluated_depsgraph_get()
    pts = []
    for m in meshes:
        ev = m.evaluated_get(dg)
        pts += [ev.matrix_world @ Vector(c) for c in ev.bound_box]
    if pts:
        lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
        hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    else:
        lo, hi = Vector((-1, -1, 0)), Vector((1, 1, 2))
    center, radius = (lo + hi) / 2, max((hi - lo).length / 2, 0.1)

    # team colour + global strengths: custom properties on the scene, read by every material
    # (Scene Properties tab > Custom Properties)

    # lighting for Rendered mode (Material Preview uses its own studio HDRI)
    sun = bpy.data.lights.new('Light', 'SUN')
    sun.energy = 3.0
    sun_ob = bpy.data.objects.new('Light', sun)
    sc.collection.objects.link(sun_ob)
    sun_ob.rotation_euler = sm.SUN_EULER
    bpy.context.view_layer.update()

    # team colour, strengths and sun direction: custom properties on the scene, read by every
    # material (Scene Properties tab > Custom Properties)
    for key, default, ui in sm.SCENE_SETTINGS:
        if key == 'sun_direction':
            default = tuple(sun_ob.matrix_world.col[2][:3])     # the sun lamp shines along its -Z
        sc[key] = default
        sc.id_properties_ui(key).update(**ui)
    # keep sun_direction in step with the lamp: rotating "Light" moves the highlights
    for i in range(3):
        fc = sc.driver_add('["sun_direction"]', i)
        drv = fc.driver
        drv.type = 'AVERAGE'
        var = drv.variables.new()
        var.name = 'v'
        var.type = 'SINGLE_PROP'
        var.targets[0].id_type = 'OBJECT'
        var.targets[0].id = sun_ob
        var.targets[0].data_path = f'matrix_world[{i}][2]'
    world = sc.world or bpy.data.worlds.new('World')
    sc.world = world
    world.use_nodes = True
    bg = next((n for n in world.node_tree.nodes if n.type == 'BACKGROUND'), None)
    if bg is not None:
        bg.inputs['Color'].default_value = (0.4, 0.4, 0.42, 1.0)   # SC2 scenes have strong ambient light
        bg.inputs['Strength'].default_value = 1.0
    try:
        sc.view_settings.view_transform = 'Standard'   # true-to-texture colours
    except TypeError:
        pass

    # every 3D viewport: Material Preview, framed on the model from a 3/4 angle
    view_rot = Quaternion((0.0, 0.0, 1.0), 0.6) @ Quaternion((1.0, 0.0, 0.0), 1.15)
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for space in area.spaces:
                if space.type != 'VIEW_3D':
                    continue
                space.shading.type = 'MATERIAL'
                space.clip_end = max(space.clip_end, radius * 50)
                r3d = space.region_3d
                if r3d is not None:
                    r3d.view_location = center
                    r3d.view_distance = radius * 3.0
                    r3d.view_rotation = view_rot
                    r3d.view_perspective = 'PERSP'


# --------------------------------------------------------------------------------------
# Per-model conversion
# --------------------------------------------------------------------------------------

def _get_prop(ob, path, idx):
    try:
        v = ob.path_resolve(path)
    except ValueError:
        return None
    try:
        return float(v[idx])
    except TypeError:
        return float(v)


def _set_prop(ob, path, idx, value):
    owner_path, prop = path.rsplit('.', 1)
    owner = ob.path_resolve(owner_path)
    cur = getattr(owner, prop)
    try:
        cur[idx] = value
    except TypeError:
        setattr(owner, prop, value)


def export_model_glb(model, ob, meshes, mesh_materials, export_info, sequences, defaults, layer_defaults,
                     placed, vis, fps, opts, report):
    """Write <model>.glb. Samples every sequence (bones and material layers) on one timeline
    action, then puts the armature's animation and the layers back the way they were."""
    import glb_export
    import glb_materials
    stem = os.path.splitext(os.path.basename(model['dae']))[0]
    ad = ob.animation_data
    prev = (ad.action if ad else None, getattr(ad, 'action_slot', None) if ad else None)
    sc = bpy.context.scene
    prev_range = (sc.frame_start, sc.frame_end, sc.frame_current)
    tmp_actions = []
    gap = int(opts.get('anim_gap', 1))
    if sequences and not placed:
        off = 0
        for seq in sequences:
            placed.append((seq, off))
            off += seq['length'] + gap
    clips = [{'name': seq['name'], 'seq': seq, 'offset': off, 'frame_start': off,
              'frame_end': off + seq['play_length']} for seq, off in placed]
    # bone + material layer channels on one action (the .dae's timeline only has the bones)
    layer_keys = sorted({k for seq, _ in placed for k in seq['mat_channels']} | set(layer_defaults))
    rest_layers = {k: _get_prop(ob, *k) for k in layer_keys}
    rest_layers = {k: v for k, v in rest_layers.items() if v is not None}
    all_defaults = dict(defaults)
    all_defaults.update({k: layer_defaults.get(k, v) for k, v in rest_layers.items()})
    act = None
    if clips:
        act = build_action(stem + '_glTF', all_defaults,
                           [(dict(seq, channels={**seq['channels'], **seq['mat_channels']}), off) for seq, off in placed])
        tmp_actions.append(act)
    animated_layers = {}
    for seq, _ in placed:
        for path, _ in seq['mat_channels']:
            owner, prop = path.rsplit('.', 1)
            animated_layers.setdefault(owner, set()).add(prop)
    try:
        team = glb_materials.parse_team_colors(opts.get('team_colors', 'red'))
    except ValueError as e:
        report['warnings'].append(f'glTF: {e}')
        team = glb_materials.parse_team_colors('red')
    default_act = None
    if sequences:
        default_act = build_action(stem + '_glTFDefault', defaults,
                                   [(dict(sequences[0], channels={}, length=1, play_length=1), 0)])
        tmp_actions.append(default_act)

    def default_pose():
        if default_act is not None:
            assign_action(ob, default_act, 0, 1)

    def clip_pose():
        assign_action(ob, act, 0, clips[-1]['frame_end'])

    try:
        glb_export.export_glb(os.path.splitext(model['dae'])[0] + '.glb', ob, meshes,
                              {m[0]: m[1] for m in mesh_materials}, export_info, clips, vis, fps,
                              max(1, int(opts.get('sampling_rate', 1))), opts.get('normal_maps', 'raw'),
                              default_pose, clip_pose, report, team, animated_layers,
                              opts.get('glb_texture_anims', 'safe'))
    finally:
        for (path, idx), v in rest_layers.items():
            try:
                _set_prop(ob, path, idx, v)
            except (ValueError, AttributeError, TypeError):
                pass
        if prev[0] is not None:
            assign_action(ob, prev[0], prev_range[0], prev_range[1])
            try:
                if prev[1] is not None:
                    ob.animation_data.action_slot = prev[1]
            except (AttributeError, TypeError):
                pass
        elif ob.animation_data:
            ob.animation_data.action = None
        sc.frame_start, sc.frame_end = prev_range[0], prev_range[1]
        sc.frame_set(prev_range[2])
        for a in tmp_actions:
            bpy.data.actions.remove(a)


def convert_model(m3s, model, tex_map, opts):
    shared = m3s.shared
    io_imp = sys.modules[m3s.__name__ + '.io_m3_import']
    report = {'m3': model['m3'], 'dae': model['dae'], 'ok': False, 'warnings': [],
              'missing_textures': set(), 'animations': [], 'meshes': 0, 'bones': 0}
    t0 = time.time()
    clear_scene()

    importer = io_imp.Importer()
    get_anims = bool(opts.get('animations', True))
    # opts order used by M3Studio: (rig, anims, mesh, effects)
    importer.m3_import(model['m3'], None, opts=(True, get_anims, True, False))
    report['warnings'].extend(importer.warn_strings)
    ob = importer.ob

    model_stem = os.path.splitext(os.path.basename(model['m3']))[0].lower()
    for m3a in model.get('m3a', []) if get_anims else []:
        try:
            before = len(ob.m3_animation_groups)
            existing = {(g.name or '').strip().lower() for g in ob.m3_animation_groups}
            imp2 = io_imp.Importer()
            imp2.m3a_import(m3a, ob)
            report['warnings'].extend(imp2.warn_strings)
            report.setdefault('m3a_imported', []).append(os.path.basename(m3a))
            # an animation library may redefine sequences the model has ("Stand", "Walk"...): name
            # its versions after the library, e.g. "Stand (swarmanims)"
            tag = os.path.splitext(os.path.basename(m3a))[0]
            if tag.lower().startswith(model_stem):
                tag = tag[len(model_stem):].strip('_ ') or tag
            try:   # M3Studio makes clashing names unique ("Stand 01"): use the file's own names
                names = [imp2.m3[s.name].content_to_string() for s in imp2.m3[imp2.m3_model.sequences]]
            except Exception:
                names = []
            added = list(ob.m3_animation_groups)[before:]
            for i, g in enumerate(added):
                orig = (names[i] if len(names) == len(added) else g.name or '').strip()
                if orig.lower() in existing:
                    g.name = f'{orig} ({tag})'
        except Exception as e:
            report['warnings'].append(f'm3a import failed for {os.path.basename(m3a)}: {e!r}')

    # region meshes are the only render geometry; everything else (hit tests, physics shapes,
    # attachment volumes...) is helper geometry that should not end up in the .dae
    region_meshes = []
    for d in importer.m3_bl_ref.values():
        if isinstance(d, dict):
            region_meshes.extend(o for o in d.values() if getattr(o, 'type', None) == 'MESH')
    region_set = set(region_meshes)
    for o in list(bpy.data.objects):
        if o is not ob and o not in region_set:
            bpy.data.objects.remove(o, do_unlink=True)

    defaults, sequences, layer_defaults, vis_defaults = collect_sequences(ob) if get_anims else ({}, [], {}, {})
    animated_paths = {k[0] for seq in sequences for k in seq['mat_channels']}
    anim_peaks = {}      # animated scalar material property -> strongest value it reaches
    for seq in sequences:
        for (path, idx), pts in seq['mat_channels'].items():
            if idx == 0 and pts:
                anim_peaks[path] = max(anim_peaks.get(path, float('-inf')), max(v for _, v, _ in pts))
    # a sequence that doesn't animate the property shows its default value (e.g. a glow that is
    # faded to 0 by every other animation, and simply left on in "Stand Work")
    for (path, idx), value in layer_defaults.items():
        if idx == 0 and path in anim_peaks and any((path, idx) not in seq['mat_channels'] for seq in sequences):
            anim_peaks[path] = max(anim_peaks[path], value)
    normal_mode = opts.get('normal_maps', 'raw')

    model_stem = os.path.splitext(os.path.basename(model['dae']))[0]
    ob.name = model_stem
    ob.data.name = model_stem + '_Armature'

    # materials + mesh clean-up
    mat_cache = {}
    mesh_materials = []   # (mesh, material name, m3 material, layers) for the .blend materials
    export_info = {}      # material name -> what postprocess_dae must add (alpha / glow textures)
    batch_bones = {}      # mesh -> bones whose render flag controls its visibility
    uv_backup = {}        # mesh -> original first-UV-set coordinates
    out_dir = os.path.dirname(os.path.abspath(model['dae']))
    os.makedirs(out_dir, exist_ok=True)
    # Blender's COLLADA exporter names joint nodes "<armature>_<bone>"; mesh object names must not
    # collide with those (some SC2 rigs have bones called "Mesh01"...), or the .dae gets duplicate IDs
    reserved = {f'{model_stem}_{b.name}'.lower() for b in ob.data.bones}
    n = len(region_meshes)
    base = next((b for b in ('Mesh', 'Geometry', 'Geo', 'Surface', 'Part')
                 if not any(f'{model_stem}_{b}{i:02d}'.lower() in reserved for i in range(n))), 'Mesh_')
    for i, mob in enumerate(region_meshes):
        cand = f'{model_stem}_{base}{i:02d}'
        mob.name = cand
        mob.data.name = mob.name
        # apply the hard-edge EdgeSplit modifier so sharp edges survive export
        for mod in list(mob.modifiers):
            if mod.type == 'EDGE_SPLIT':
                with bpy.context.temp_override(object=mob, active_object=mob, selected_objects=[mob]):
                    try:
                        bpy.ops.object.modifier_apply(modifier=mod.name)
                    except RuntimeError as e:
                        report['warnings'].append(f'EdgeSplit apply failed on {mob.name}: {e}')
                        mob.modifiers.remove(mod)
        # first UV layer active for export
        if mob.data.uv_layers:
            mob.data.uv_layers.active_index = 0
            mob.data.uv_layers[0].active_render = True

        matref = None
        for batch in mob.m3_mesh_batches:
            matref = shared.m3_pointer_get(ob.m3_materialrefs, batch.material)
            if matref is not None:
                break
        bound = []
        for batch in mob.m3_mesh_batches:
            pb = shared.m3_pointer_get(ob.pose.bones, batch.bone.handle) if batch.bone.handle else None
            bound.append(pb.name if pb is not None else None)
        # a batch without a bone is always drawn
        batch_bones[mob] = [] if (not bound or None in bound) else bound
        m3mat, mtype = sm.m3_material_from_ref(ob, matref, shared) if matref else (None, None)
        mat_name = matref.name if matref else f'{model_stem}_NoMaterial'
        layers = sm.collect_layers(ob, m3mat, shared)
        if mtype == 'm3_materials_terrain' and not any(sm.layer_bitmap(l) for l in layers.values()):
            # shows the map's terrain in game: use a stand-in terrain texture, projected from above
            terrain = model.get('terrain') or {}
            layers = sm.terrain_standin_layers(terrain)
            project_terrain_uvs(mob, float(terrain.get('tile') or TERRAIN_TILE))
            report['terrain'] = terrain.get('name') or 'colour'
        plan = sm.export_plan(layers, m3mat, mtype)
        if mat_name not in mat_cache:
            mat_cache[mat_name], export_info[mat_name] = sm.build_export_material(
                mat_name, plan, tex_map, report, out_dir, layers.get('normal'), normal_mode, anim_peaks)
        mob.data.materials.clear()
        mob.data.materials.append(mat_cache[mat_name])
        mesh_materials.append((mob, mat_name, m3mat, layers, mtype))
        # COLLADA/OBJ textures always read the first UV set with no transform: bake the main
        # texture's UV set / tiling / offset / rotation into it for the export (restored afterwards)
        if plan['main'] is not None and not sm.mapping_is_identity(plan['main']):
            backup = bake_export_uvs(mob, plan['main'])
            if backup is not None:
                uv_backup[mob] = backup

    report['meshes'] = len(region_meshes)
    report['bones'] = len(ob.data.bones)

    drop_m3studio_actions(ob)
    fps = bpy.context.scene.render.fps
    vis = Visibility(ob, region_meshes, batch_bones, vis_defaults)
    vis_on = bool(sequences) and vis.is_animated(sequences)
    report['visibility_animated'] = vis_on

    if not region_meshes:
        report['warnings'].append('model has no mesh geometry (effect/dummy model)')
        if opts.get('skip_empty', True):
            report['skipped'] = 'no geometry'
            report['ok'] = True
            return report

    for o in bpy.data.objects:
        o.select_set(False)
    ob.select_set(True)
    for mob in region_meshes:
        mob.select_set(True)
    bpy.context.view_layer.objects.active = ob
    os.makedirs(os.path.dirname(model['dae']), exist_ok=True)

    anim_mode = opts.get('anim_mode', 'timeline')   # timeline | separate | both | none
    if anim_mode == 'none':
        sequences = []
    gap = int(opts.get('anim_gap', 1))

    # 1) main file: all sequences laid out end to end on one timeline
    clips = []
    timeline_act = None
    placed = []
    if sequences and anim_mode in ('timeline', 'both'):
        placed, off = [], 0
        for seq in sequences:
            placed.append((seq, off))
            clips.append({'name': seq['name'], 'frame_start': off, 'frame_end': off + seq['play_length'],
                          'looping': seq['looping'], 'source_frames': seq['source_frames']})
            off += seq['length'] + gap
        act = build_action(model_stem + '_AllAnimations', defaults, placed)
        timeline_act = act
        assign_action(ob, act, 0, clips[-1]['frame_end'])
        export_collada(model['dae'], opts, False)
        inject_animations(model['dae'], ob, 0, clips[-1]['frame_end'], fps, opts.get('sampling_rate', 1),
                          vis.collapse_fn(placed) if vis_on else None)
        report['warnings'] += postprocess_dae(model['dae'], clips, fps, export_info)
        write_anim_list(os.path.splitext(model['dae'])[0] + '_animations.txt', model_stem, clips, fps)
    else:
        # static export in the default pose
        if sequences:
            act = build_action(model_stem + '_Default', defaults,
                               [(dict(sequences[0], channels={}, length=1, play_length=1), 0)])
            assign_action(ob, act, 0, 1)
        export_collada(model['dae'], opts, False)
        report['warnings'] += postprocess_dae(model['dae'], None, fps, export_info)

    # 2) optional: one file per sequence
    if sequences and anim_mode in ('separate', 'both'):
        anim_dir = os.path.join(os.path.dirname(model['dae']), model_stem + '_anims')
        os.makedirs(anim_dir, exist_ok=True)
        for seq in sequences:
            act = build_action(f"{model_stem}_{seq['name']}", defaults, [(seq, 0)])
            assign_action(ob, act, 0, seq['play_length'])
            path = os.path.join(anim_dir, f"{model_stem}_{safe_name(seq['name'])}.dae")
            export_collada(path, opts, False)
            inject_animations(path, ob, 0, seq['play_length'], fps, opts.get('sampling_rate', 1),
                              vis.collapse_fn([(seq, 0)]) if vis_on else None)
            postprocess_dae(path, [{'name': seq['name'], 'frame_start': 0, 'frame_end': seq['play_length']}], fps,
                            export_info)
            bpy.data.actions.remove(act)
        if not clips:
            clips = [{'name': s_['name'], 'frame_start': 0, 'frame_end': s_['play_length'],
                      'looping': s_['looping'], 'source_frames': s_['source_frames']} for s_ in sequences]

    report['animations'] = clips

    # 3) Wavefront OBJ + MTL (static mesh in its bind pose; OBJ has no skeleton/animation)
    if opts.get('obj', True):
        for name, info in export_info.items():
            sm.set_export_emission(mat_cache[name], info, True)
        export_obj(os.path.splitext(model['dae'])[0] + '.obj', region_meshes)

    # 4) glTF binary: one animation per sequence, mesh visibility, normal maps, embedded textures
    if opts.get('glb', True):
        export_model_glb(model, ob, region_meshes, mesh_materials, export_info, sequences, defaults,
                         layer_defaults, list(placed) if timeline_act else [], vis if vis_on else None, fps,
                         opts, report)
    restore_uvs(uv_backup)

    if opts.get('save_blend'):
        anim_names = None
        if sequences and hasattr(bpy.types, 'ActionSlot'):
            # one action per animation (+ "All Animations"), switched with the saved panel script
            targets_by_mat = build_blend_materials(mesh_materials, tex_map, set(opts.get('equirect', [])), report,
                                                   normal_mode, animated_paths, layer_defaults, [])
            if timeline_act is not None:
                ob.animation_data.action = None
                bpy.data.actions.remove(timeline_act)
                timeline_act = None
            anim_names = build_blend_actions(ob, sequences, defaults, layer_defaults, targets_by_mat,
                                             vis if vis_on else None, placed, clips, report)
        else:
            build_blend_materials(mesh_materials, tex_map, set(opts.get('equirect', [])), report, normal_mode,
                                  animated_paths, layer_defaults, placed if timeline_act else [])
            if vis_on and timeline_act is not None:
                vis.keyframe_objects(placed)
        setup_viewer(ob, region_meshes, clips if timeline_act else [], timeline_act)
        if anim_names:
            apply_blend_animation(anim_names)
        blend_path = os.path.abspath(os.path.splitext(model['dae'])[0] + '.blend')
        # store texture paths relative to the .blend ("//Foo.png") so the folder can be moved/shared
        for img in bpy.data.images:
            if img.filepath and not img.filepath.startswith('//'):
                img.filepath = '//' + os.path.relpath(os.path.abspath(img.filepath),
                                                      os.path.dirname(blend_path)).replace(os.sep, '/')
        bpy.context.preferences.filepaths.save_version = 0      # no .blend1 backup files
        if os.path.isfile(blend_path):
            os.remove(blend_path)
        bpy.ops.wm.save_as_mainfile(filepath=blend_path, check_existing=False, compress=True,
                                    relative_remap=True)

    report['ok'] = True
    report['seconds'] = round(time.time() - t0, 2)
    return report


def convert_textures_with_blender(pairs):
    """Fallback DDS->PNG conversion through Blender's image loader (for formats Pillow lacks)."""
    done = {}
    for dds, png in pairs:
        try:
            src = bpy.data.images.load(dds, check_existing=False)
            w, h = src.size
            if not w or not h:
                raise RuntimeError('Blender could not decode the image')
            px = src.pixels[:]                     # raw stored values, no colour transform
            dst = bpy.data.images.new('converted', w, h, alpha=True)
            dst.pixels[:] = px
            os.makedirs(os.path.dirname(png), exist_ok=True)
            dst.filepath_raw = png
            dst.file_format = 'PNG'
            dst.save()
            bpy.data.images.remove(dst)
            bpy.data.images.remove(src)
            done[dds] = True
        except Exception as e:
            done[dds] = repr(e)
    return done


def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--job', required=True)
    args = ap.parse_args(argv)
    with open(args.job, 'r', encoding='utf-8') as f:
        job = json.load(f)

    results = {'models': [], 'textures': {}}
    if job.get('texture_fallback'):
        results['textures'] = convert_textures_with_blender(job['texture_fallback'])

    m3s = load_m3studio(job['m3studio'])
    opts = dict(job.get('options', {}))
    opts['equirect'] = [norm_tex_key(k) for k in job.get('equirect', [])]
    tex_map = {norm_tex_key(k): v for k, v in job.get('textures', {}).items()}

    for model in job.get('models', []):
        try:
            rep = convert_model(m3s, model, tex_map, opts)
        except Exception:
            rep = {'m3': model['m3'], 'dae': model['dae'], 'ok': False, 'error': traceback.format_exc()}
        rep['missing_textures'] = sorted(rep.get('missing_textures', []))
        results['models'].append(rep)
        status = 'OK ' if rep.get('ok') else 'ERR'
        print(f"[sc2rip] {status} {os.path.basename(model['m3'])}"
              + (f" anims={len(rep.get('animations', []))}" if rep.get('ok') else ''), flush=True)
        if not rep.get('ok'):
            print(rep.get('error', ''), flush=True)

    if job.get('result'):
        with open(job['result'], 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=1)


if __name__ == '__main__':
    argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else sys.argv[1:]
    main(argv)
    # With M3Studio registered, interpreter teardown can crash (seen with the bpy module) or hang.
    # Every output file is already written and closed at this point, so leave immediately.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
