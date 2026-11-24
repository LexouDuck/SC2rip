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
    return path.startswith('m3_materiallayers[') and path.rsplit('.', 1)[-1] in sm.ANIMATED_LAYER_PROPS


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

    Returns (defaults, sequences, layer_defaults) where
        defaults       = {(data_path, index): value}            bone channels
        layer_defaults = {(data_path, index): value}            material-layer channels
        sequences = [{'name', 'length', 'looping', 'source_frames',
                      'channels': {key: [(f, v, interp)]}, 'mat_channels': {...}}]
    with frames relative to the start of the sequence.
    """
    defaults, layer_defaults = {}, {}
    if ob.m3_animations_default:
        for fc in fcurves_of(ob.m3_animations_default):
            if not len(fc.keyframe_points):
                continue
            if is_bone_transform(fc.data_path):
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
        channels, mat_channels = {}, {}
        for a in sorted([a for a in g.animations if a.action], key=lambda a: a.priority):
            for fc in fcurves_of(a.action):
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
        sequences.append({'name': name, 'length': f1 - f0, 'looping': not g.not_looping,
                          'source_frames': [f0, f1], 'channels': channels, 'mat_channels': mat_channels})
    return defaults, sequences, layer_defaults


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


def sample_joint_matrices(ob, frames):
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
            for pb in bones:
                m = pb.matrix if pb.parent is None else pb.parent.matrix.inverted_safe() @ pb.matrix
                out[pb.name].append((m[0][0], m[0][1], m[0][2], m[0][3], m[1][0], m[1][1], m[1][2], m[1][3],
                                     m[2][0], m[2][1], m[2][2], m[2][3], m[3][0], m[3][1], m[3][2], m[3][3]))
    finally:
        for m in mods:
            m.show_viewport = True
    return out


def _fmt(values):
    return ' '.join('%.7g' % v for v in values)


def inject_animations(dae_path, ob, frame_start, frame_end, fps, step=1):
    """Write a <library_animations> with one matrix animation per moving joint into a .dae that
    Blender exported without animation (the static pose must be the pose at `frame_start`)."""
    frames = list(range(int(frame_start), int(frame_end) + 1, max(1, int(step))))
    if frames[-1] != int(frame_end):
        frames.append(int(frame_end))
    mats = sample_joint_matrices(ob, frames)

    ET.register_namespace('', COLLADA_NS)
    tree = ET.parse(dae_path)
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
        nid = joint_ids.get(bone)
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


def postprocess_dae(dae_path, clips=None, fps=30):
    """Make texture paths relative and add <library_animation_clips> describing each sequence.

    clips: [{'name', 'frame_start', 'frame_end'}] on the exported timeline (frames at `fps`).
    """
    ET.register_namespace('', COLLADA_NS)
    tree = ET.parse(dae_path)
    root = tree.getroot()
    ns = {'c': COLLADA_NS}
    q = lambda tag: f'{{{COLLADA_NS}}}{tag}'
    dae_dir = os.path.dirname(os.path.abspath(dae_path))

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

    lib_anims = root.find('c:library_animations', ns)
    if clips and lib_anims is not None:
        top_ids = [a.get('id') for a in lib_anims.findall('c:animation', ns) if a.get('id')]
        old = root.find('c:library_animation_clips', ns)
        if old is not None:
            root.remove(old)
        lib_clips = ET.Element(q('library_animation_clips'))
        used = set()
        for c in clips:
            cid = ''.join(ch if ch.isalnum() or ch in '_-' else '_' for ch in c['name']) or 'clip'
            base, k = cid, 2
            while cid in used:
                cid, k = f'{base}_{k}', k + 1
            used.add(cid)
            el = ET.SubElement(lib_clips, q('animation_clip'), {
                'id': 'clip_' + cid, 'name': c['name'],
                'start': f"{c['frame_start'] / fps:.6f}", 'end': f"{c['frame_end'] / fps:.6f}"})
            for aid in top_ids:
                ET.SubElement(el, q('instance_animation'), {'url': '#' + aid})
        kids = list(root)
        root.insert(kids.index(lib_anims) + 1, lib_clips)
    ET.indent(tree, space='  ')
    tree.write(dae_path, xml_declaration=True, encoding='utf-8')


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


def build_blend_materials(mesh_materials, tex_map, equirect, report, normal_mode, animated_paths,
                          layer_defaults, placed):
    """Swap the export (Principled) materials for SC2-style ones and animate their layers."""
    built, targets_by_mat = {}, {}
    for mob, name, m3mat, layers, mtype in mesh_materials:
        if name not in built:
            if mtype == 'm3_materials_standard' and m3mat is not None:
                old = bpy.data.materials.get(name)
                if old is not None:
                    old.name = name + '_export'
                mat, targets = sm.build_sc2_material(name, m3mat, layers, tex_map, equirect, report,
                                                     normal_mode, animated_paths)
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
            lines[0] = f'# Exported by sc2rip with Blender {bpy.app.version_string}'
        with open(mtl, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))


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

    # lighting for Rendered mode (Material Preview uses its own studio HDRI)
    sun = bpy.data.lights.new('SC2rip_Sun', 'SUN')
    sun.energy = 3.0
    sun_ob = bpy.data.objects.new('SC2rip_Sun', sun)
    sc.collection.objects.link(sun_ob)
    sun_ob.rotation_euler = (0.8, 0.0, 0.6)
    world = sc.world or bpy.data.worlds.new('World')
    sc.world = world
    world.use_nodes = True
    bg = next((n for n in world.node_tree.nodes if n.type == 'BACKGROUND'), None)
    if bg is not None:
        bg.inputs['Color'].default_value = (0.25, 0.25, 0.27, 1.0)
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

    for m3a in model.get('m3a', []) if get_anims else []:
        try:
            imp2 = io_imp.Importer()
            imp2.m3a_import(m3a, ob)
            report['warnings'].extend(imp2.warn_strings)
            report.setdefault('m3a_imported', []).append(os.path.basename(m3a))
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

    defaults, sequences, layer_defaults = collect_sequences(ob) if get_anims else ({}, [], {})
    animated_paths = {k[0] for seq in sequences for k in seq['mat_channels']}
    normal_mode = opts.get('normal_maps', 'raw')

    model_stem = os.path.splitext(os.path.basename(model['dae']))[0]
    ob.name = model_stem
    ob.data.name = model_stem + '_Armature'

    # materials + mesh clean-up
    mat_cache = {}
    mesh_materials = []   # (mesh, material name, m3 material, layers) for the .blend materials
    for i, mob in enumerate(region_meshes):
        mob.name = f'{model_stem}_Mesh{i:02d}'
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
        m3mat, mtype = sm.m3_material_from_ref(ob, matref, shared) if matref else (None, None)
        mat_name = matref.name if matref else f'{model_stem}_NoMaterial'
        layers = sm.collect_layers(ob, m3mat, shared)
        if mat_name not in mat_cache:
            mat_cache[mat_name] = sm.build_export_material(mat_name, layers, tex_map, report,
                                                           normal_mode, animated_paths)
        mob.data.materials.clear()
        mob.data.materials.append(mat_cache[mat_name])
        mesh_materials.append((mob, mat_name, m3mat, layers, mtype))

    report['meshes'] = len(region_meshes)
    report['bones'] = len(ob.data.bones)

    drop_m3studio_actions(ob)
    fps = bpy.context.scene.render.fps

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
            clips.append({'name': seq['name'], 'frame_start': off, 'frame_end': off + seq['length'],
                          'looping': seq['looping'], 'source_frames': seq['source_frames']})
            off += seq['length'] + gap
        act = build_action(model_stem + '_AllAnimations', defaults, placed)
        timeline_act = act
        assign_action(ob, act, 0, clips[-1]['frame_end'])
        export_collada(model['dae'], opts, False)
        inject_animations(model['dae'], ob, 0, clips[-1]['frame_end'], fps, opts.get('sampling_rate', 1))
        postprocess_dae(model['dae'], clips, fps)
        write_anim_list(os.path.splitext(model['dae'])[0] + '_animations.txt', model_stem, clips, fps)
    else:
        # static export in the default pose
        if sequences:
            act = build_action(model_stem + '_Default', defaults, [(dict(sequences[0], channels={}, length=1), 0)])
            assign_action(ob, act, 0, 1)
        export_collada(model['dae'], opts, False)
        postprocess_dae(model['dae'])

    # 2) optional: one file per sequence
    if sequences and anim_mode in ('separate', 'both'):
        anim_dir = os.path.join(os.path.dirname(model['dae']), model_stem + '_anims')
        os.makedirs(anim_dir, exist_ok=True)
        for seq in sequences:
            act = build_action(f"{model_stem}_{seq['name']}", defaults, [(seq, 0)])
            assign_action(ob, act, 0, seq['length'])
            path = os.path.join(anim_dir, f"{model_stem}_{safe_name(seq['name'])}.dae")
            export_collada(path, opts, False)
            inject_animations(path, ob, 0, seq['length'], fps, opts.get('sampling_rate', 1))
            postprocess_dae(path, [{'name': seq['name'], 'frame_start': 0, 'frame_end': seq['length']}], fps)
            bpy.data.actions.remove(act)
        if not clips:
            clips = [{'name': s_['name'], 'frame_start': 0, 'frame_end': s_['length'],
                      'looping': s_['looping'], 'source_frames': s_['source_frames']} for s_ in sequences]

    report['animations'] = clips

    # 3) Wavefront OBJ + MTL (static mesh in its bind pose; OBJ has no skeleton/animation)
    if opts.get('obj', True):
        export_obj(os.path.splitext(model['dae'])[0] + '.obj', region_meshes)

    if opts.get('save_blend'):
        build_blend_materials(mesh_materials, tex_map, set(opts.get('equirect', [])), report, normal_mode,
                              animated_paths, layer_defaults, placed if timeline_act else [])
        setup_viewer(ob, region_meshes, clips if timeline_act else [], timeline_act)
        blend_path = os.path.abspath(os.path.splitext(model['dae'])[0] + '.blend')
        # store texture paths relative to the .blend ("//Foo.png") so the folder can be moved/shared
        for img in bpy.data.images:
            if img.filepath and not img.filepath.startswith('//'):
                img.filepath = '//' + os.path.relpath(os.path.abspath(img.filepath),
                                                      os.path.dirname(blend_path)).replace(os.sep, '/')
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
            dst = bpy.data.images.new('sc2rip_tmp', w, h, alpha=True)
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
