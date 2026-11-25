"""glTF binary (.glb) export of a converted SC2 model, written straight from the Blender scene.

* meshes: bind-pose geometry with split normals and the first UV set (already baked for the export
  materials), skinned to the full bone hierarchy (4 weights per vertex)
* one glTF animation per SC2 sequence ("Stand", "Walk", "Death"...), sampled per frame
* mesh visibility (death debris swapped per variant, effects shown only in some animations): a
  morph target that collapses the mesh, driven by STEP-interpolated morph weights
* materials: glTF PBR from the same export plan as the .dae/.obj (baked colour textures with
  transparency in alpha), plus what glTF adds over them: normal maps, emission textures, glows as
  unlit materials, double-sided and alpha-test settings. Textures are embedded.

Blender is Z-up, glTF is Y-up: everything is written in Blender space under one root node that
rotates it upright (skinned meshes follow it through their joints)."""

import os
import tempfile

import bpy
import numpy as np

import sc2_material as sm
from gltf_writer import GLTFBuilder, FLOAT, UBYTE, USHORT, UINT, ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER

Y_UP = [-0.7071067811865476, 0.0, 0.0, 0.7071067811865476]   # -90 degrees about X
ROUGHNESS = 0.6
EPS = 1e-5


# --------------------------------------------------------------------------- textures

def _file_bytes(img):
    path = bpy.path.abspath(img.filepath) if img is not None and img.filepath else ''
    if not path or not os.path.isfile(path):
        return None
    with open(path, 'rb') as f:
        return f.read()


def _png_bytes(name, rgb):
    """PNG file content for an (h, w, 3) float array (Blender's bottom-up row order)."""
    h, w = rgb.shape[:2]
    img = bpy.data.images.new(name, w, h, alpha=False)
    img.colorspace_settings.name = 'Non-Color'
    px = np.concatenate([rgb, np.ones((h, w, 1), np.float32)], axis=2)
    img.pixels.foreach_set(px.astype(np.float32).ravel())
    fd, tmp = tempfile.mkstemp(suffix='.png')
    os.close(fd)
    try:
        img.filepath_raw = tmp
        img.file_format = 'PNG'
        img.save()
        with open(tmp, 'rb') as f:
            return f.read()
    finally:
        bpy.data.images.remove(img)
        os.remove(tmp)


def gl_normal_png(img, normal_mode):
    """glTF normal maps are plain RGB, OpenGL convention (green up). SC2's are DirectX convention,
    packed "DXT5nm" (X in alpha, Y in green) or plain RGB."""
    if normal_mode == 'rgb':          # the PNG was already rebuilt in that format
        return _file_bytes(img)
    px = sm._pixels(img)
    x = px[..., 3] if sm.normal_is_packed(img) else px[..., 0]
    x = x * 2 - 1
    y = 1 - px[..., 1] * 2            # green down -> green up
    z = np.sqrt(np.clip(1 - x * x - y * y, 0, 1))
    return _png_bytes(img.name + '_gl', np.stack([x, y, z], axis=2) * 0.5 + 0.5)


# --------------------------------------------------------------------------- materials

def material_entry(b, name, info, normal_mode, cache):
    pbr = {'baseColorFactor': [0.0, 0.0, 0.0, 1.0], 'metallicFactor': 0.0, 'roughnessFactor': ROUGHNESS}
    m = {'name': name, 'pbrMetallicRoughness': pbr}
    if info.get('double_sided'):
        m['doubleSided'] = True
    if info.get('transparent'):       # distortion / volume / flare effects: not drawn
        pbr['baseColorFactor'] = [0.0, 0.0, 0.0, 0.0]
        m['alphaMode'] = 'BLEND'
        return m
    if info.get('const_color') is not None:
        pbr['baseColorFactor'] = [float(c) for c in info['const_color'][:3]] + [1.0]

    def tex(img, kind='color'):
        key = (img.name, kind)
        if key not in cache:
            data = _file_bytes(img) if kind == 'color' else gl_normal_png(img, normal_mode)
            cache[key] = b.texture(data, key, img.name) if data else None
        return cache[key]

    base = info.get('base_image')
    t = tex(base) if base is not None else None
    if t is not None:
        pbr['baseColorFactor'] = [1.0, 1.0, 1.0, 1.0]
        pbr['baseColorTexture'] = {'index': t}
        kind = info.get('alpha_kind')
        if kind in ('blend', 'additive'):
            m['alphaMode'] = 'BLEND'
        elif kind == 'mask':
            m['alphaMode'] = 'MASK'
            m['alphaCutoff'] = round(info.get('alpha_cutoff', 0.5), 4)
    if info.get('unlit'):
        m['extensions'] = {'KHR_materials_unlit': {}}
        return m
    eimg = info.get('emission_image')
    te = tex(eimg) if eimg is not None else None
    if te is not None:
        m['emissiveTexture'] = {'index': te}
        m['emissiveFactor'] = [1.0, 1.0, 1.0]
    nimg = info.get('normal_image')
    tn = tex(nimg, 'normal') if nimg is not None else None
    if tn is not None:
        m['normalTexture'] = {'index': tn}
    return m


# --------------------------------------------------------------------------- geometry

def mesh_arrays(mob, bone_index, fallback_joint):
    """Unique (position, normal, uv) vertices + triangle indices + 4 joint weights per vertex,
    in Blender world space (bind pose). Returns None for meshes without triangles."""
    me = mob.data
    me.calc_loop_triangles()
    if not len(me.loop_triangles):
        return None
    mw = np.array(mob.matrix_world, dtype=np.float64)
    nv, nl = len(me.vertices), len(me.loops)
    co = np.empty(nv * 3, np.float32)
    me.vertices.foreach_get('co', co)
    co = co.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]
    loop_v = np.empty(nl, np.int32)
    me.loops.foreach_get('vertex_index', loop_v)
    nrm = np.empty(nl * 3, np.float32)
    me.corner_normals.foreach_get('vector', nrm)
    nrm = nrm.reshape(-1, 3) @ np.linalg.inv(mw[:3, :3])     # (M^-T n) as row vectors
    nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-12)
    uv = np.zeros((nl, 2), np.float32)
    if me.uv_layers:
        me.uv_layers[0].data.foreach_get('uv', uv.ravel())
    tan = None
    if me.uv_layers:
        try:      # MikkTSpace tangents, as glTF viewers expect for normal maps
            me.calc_tangents(uvmap=me.uv_layers[0].name)
            t = np.empty(nl * 3, np.float32)
            me.loops.foreach_get('tangent', t)
            sign = np.empty(nl, np.float32)
            me.loops.foreach_get('bitangent_sign', sign)
            t = t.reshape(-1, 3) @ mw[:3, :3].T
            t /= np.maximum(np.linalg.norm(t, axis=1, keepdims=True), 1e-12)
            if np.linalg.det(mw[:3, :3]) < 0:
                sign = -sign
            tan = np.concatenate([t, sign[:, None]], axis=1)
        except RuntimeError:
            tan = None
    corners = np.empty(len(me.loop_triangles) * 3, np.int32)
    me.loop_triangles.foreach_get('loops', corners)

    vid = loop_v[corners]
    key = np.concatenate([vid[:, None].astype(np.int64),
                          np.round(nrm[corners] * 1e4).astype(np.int64),
                          np.round(uv[corners] * 1e5).astype(np.int64)], axis=1)
    _, first, inverse = np.unique(key, axis=0, return_index=True, return_inverse=True)
    src = corners[first]
    out = {'position': co[loop_v[src]].astype(np.float32), 'normal': nrm[src].astype(np.float32),
           'uv': np.stack([uv[src, 0], 1.0 - uv[src, 1]], axis=1).astype(np.float32),   # glTF: V down
           'indices': inverse.ravel().astype(np.uint32)}
    if tan is not None:
        out['tangent'] = tan[src].astype(np.float32)

    if bone_index is not None:
        names = {g.index: g.name for g in mob.vertex_groups}
        joints = np.zeros((nv, 4), np.int32)
        weights = np.zeros((nv, 4), np.float32)
        for v in me.vertices:
            infl = sorted(((g.weight, bone_index[names[g.group]]) for g in v.groups
                           if g.weight > 1e-6 and names.get(g.group) in bone_index), reverse=True)[:4]
            total = sum(w for w, _ in infl)
            if total <= 0:
                infl, total = [(1.0, fallback_joint)], 1.0
            for k, (w, j) in enumerate(infl):
                joints[v.index, k] = j
                weights[v.index, k] = w / total
        vsrc = loop_v[src]
        out['joints'] = joints[vsrc]
        out['weights'] = weights[vsrc]
    return out


# --------------------------------------------------------------------------- animation sampling

def sample_pose(ob, bones, frames):
    """Local TRS of every bone (root bones: relative to the scene, armature transform included)
    at each frame -> (T [F, J, 3], R [F, J, 4] xyzw, S [F, J, 3])."""
    sc = bpy.context.scene
    pbs = [ob.pose.bones[bn.name] for bn in bones]
    obw = ob.matrix_world.copy()
    mods = [m for o in bpy.data.objects for m in o.modifiers if m.type == 'ARMATURE' and m.show_viewport]
    for m in mods:
        m.show_viewport = False
    T = np.zeros((len(frames), len(bones), 3), np.float32)
    R = np.zeros((len(frames), len(bones), 4), np.float32)
    S = np.ones((len(frames), len(bones), 3), np.float32)
    try:
        for i, f in enumerate(frames):
            if f is not None:
                fi = int(f)
                sc.frame_set(fi, subframe=f - fi)
            for j, pb in enumerate(pbs):
                mat = obw @ pb.matrix if pb.parent is None else pb.parent.matrix.inverted_safe() @ pb.matrix
                t, q, s = mat.decompose()
                T[i, j] = t
                R[i, j] = (q.x, q.y, q.z, q.w)
                S[i, j] = s
    finally:
        for m in mods:
            m.show_viewport = True
    return T, R, S


def _continuous(q, ref):
    """Flip quaternion signs so consecutive keys (and the first key vs ref) take the short way."""
    q = q.copy()
    if np.dot(q[0], ref) < 0:
        q[0] = -q[0]
    for i in range(1, len(q)):
        if np.dot(q[i], q[i - 1]) < 0:
            q[i] = -q[i]
    return q


# --------------------------------------------------------------------------- export

def export_glb(path, ob, meshes, mesh_material, export_info, clips, vis, fps, step, normal_mode,
               default_pose, clip_pose, report):
    """meshes: region mesh objects; mesh_material: {mesh: material name}; export_info: {material
    name: info from sc2_material.build_export_material}; clips: [{'name', 'seq', 'offset',
    'frame_start', 'frame_end'}] on one timeline; default_pose / clip_pose: callables that assign
    the default-pose action / the clips' timeline action; vis: Visibility or None."""
    b = GLTFBuilder(f'sc2rip (Blender {bpy.app.version_string})')
    bones = list(ob.data.bones)
    bone_index = {bn.name: i for i, bn in enumerate(bones)}

    # ---- default pose = node transforms
    default_pose()
    T0, R0, S0 = sample_pose(ob, bones, [None])
    T0, R0, S0 = T0[0], R0[0], S0[0]

    root = b.node(name=ob.name, rotation=Y_UP, children=[])
    joint_nodes = []
    for j, bn in enumerate(bones):
        joint_nodes.append(b.node(name=bn.name, translation=[float(v) for v in T0[j]],
                                  rotation=[float(v) for v in R0[j]], scale=[float(v) for v in S0[j]]))
    for j, bn in enumerate(bones):
        parent = root if bn.parent is None else joint_nodes[bone_index[bn.parent.name]]
        b.doc['nodes'][parent].setdefault('children', []).append(joint_nodes[j])

    skin = None
    if bones:
        obw = np.array(ob.matrix_world, dtype=np.float64)
        ibm = np.stack([np.linalg.inv(obw @ np.array(bn.matrix_local, dtype=np.float64)) for bn in bones])
        ibm = ibm.transpose(0, 2, 1).astype(np.float32)          # column-major
        skin = b.skin({'name': ob.name + '_Skin', 'joints': joint_nodes, 'skeleton': root,
                       'inverseBindMatrices': b.accessor(ibm, 'MAT4')})

    # ---- materials
    mat_index, tex_cache = {}, {}
    for name, info in export_info.items():
        mat_index[name] = b.material(material_entry(b, name, info, normal_mode, tex_cache))

    # ---- meshes
    hideable = {}           # mesh node -> (mesh object, default weight)
    scene_nodes = [root]
    for mob in meshes:
        skinned = skin is not None and any(m.type == 'ARMATURE' and m.object == ob for m in mob.modifiers)
        data = mesh_arrays(mob, bone_index if skinned else None, 0)
        if data is None:
            continue
        attrs = {'POSITION': b.accessor(data['position'], 'VEC3', FLOAT, ARRAY_BUFFER, minmax=True),
                 'NORMAL': b.accessor(data['normal'], 'VEC3', FLOAT, ARRAY_BUFFER),
                 'TEXCOORD_0': b.accessor(data['uv'], 'VEC2', FLOAT, ARRAY_BUFFER)}
        if 'tangent' in data:
            attrs['TANGENT'] = b.accessor(data['tangent'], 'VEC4', FLOAT, ARRAY_BUFFER)
        if skinned:
            jt = UBYTE if len(bones) <= 256 else USHORT
            attrs['JOINTS_0'] = b.accessor(data['joints'], 'VEC4', jt, ARRAY_BUFFER)
            attrs['WEIGHTS_0'] = b.accessor(data['weights'], 'VEC4', FLOAT, ARRAY_BUFFER)
        idx = data['indices']
        it = USHORT if len(data['position']) < 65536 else UINT
        prim = {'attributes': attrs, 'indices': b.accessor(idx, 'SCALAR', it, ELEMENT_ARRAY_BUFFER),
                'mode': 4}
        mname = mesh_material.get(mob)
        if mname in mat_index:
            prim['material'] = mat_index[mname]
        mesh = {'name': mob.name, 'primitives': [prim]}
        hide = vis is not None and vis.batch_bones.get(mob.name) and (
            not vis.mesh_visible({}, mob.name, 0) or
            any(not vis.mesh_visible(c['seq'], mob.name, rel) for c in clips
                for rel in _vis_events(vis, c['seq'], mob.name)))
        if hide:
            # morph target that moves every vertex onto the origin: weight 1 = hidden
            prim['targets'] = [{'POSITION': b.accessor(-data['position'], 'VEC3', FLOAT, ARRAY_BUFFER,
                                                       minmax=True)}]
            w0 = 0.0 if vis.mesh_visible({}, mob.name, 0) else 1.0
            mesh['weights'] = [w0]
            mesh['extras'] = {'targetNames': ['hidden']}
        node = b.node(name=mob.name, mesh=b.mesh(mesh), skin=skin if skinned else None)
        if skinned:
            scene_nodes.append(node)          # skinned meshes are placed by their joints
        else:
            b.doc['nodes'][root]['children'].append(node)
        if hide:
            hideable[node] = (mob, w0)
    b.doc['scenes'][0]['nodes'] = scene_nodes

    # ---- animations
    if clips and bones:
        frames, frame_ix = [], {}
        for c in clips:
            fs = list(np.arange(c['frame_start'], c['frame_end'] + 1e-6, step))
            if not fs or abs(fs[-1] - c['frame_end']) > 1e-6:
                fs.append(c['frame_end'])
            c['_frames'] = [float(f) for f in fs]
            for f in c['_frames']:
                if f not in frame_ix:
                    frame_ix[f] = len(frames)
                    frames.append(f)
        clip_pose()
        T, R, S = sample_pose(ob, bones, frames)
        # channels that move in any clip are written in every clip (constant where they don't move),
        # so switching animations in a viewer never leaves a bone where the previous one put it
        moving = {'translation': np.abs(T - T0).max(axis=(0, 2)) > EPS,
                  'scale': np.abs(S - S0).max(axis=(0, 2)) > EPS,
                  'rotation': np.minimum(np.abs(R - R0).max(axis=2), np.abs(R + R0).max(axis=2)).max(axis=0) > EPS}
        for c in clips:
            ix = [frame_ix[f] for f in c['_frames']]
            times = (np.array(c['_frames']) - c['frame_start']) / fps
            t_acc = None
            channels, samplers = [], []

            t0_acc = None

            def add(node, prop, values, interp='LINEAR', input_acc=None, constant=False):
                nonlocal t_acc, t0_acc
                if constant:          # one key at the start
                    if t0_acc is None:
                        t0_acc = b.accessor(np.zeros(1, np.float32), 'SCALAR', FLOAT, minmax=True)
                    input_acc, values = t0_acc, values[:1]
                if input_acc is None:
                    if t_acc is None:
                        t_acc = b.accessor(times.astype(np.float32), 'SCALAR', FLOAT, minmax=True)
                    input_acc = t_acc
                type_ = {'translation': 'VEC3', 'scale': 'VEC3', 'rotation': 'VEC4', 'weights': 'SCALAR'}[prop]
                samplers.append({'input': input_acc, 'output': b.accessor(values, type_), 'interpolation': interp})
                channels.append({'sampler': len(samplers) - 1, 'target': {'node': node, 'path': prop}})

            for j in range(len(bones)):
                r = _continuous(R[ix, j], R0[j])
                for prop, vals in (('translation', T[ix, j]), ('rotation', r), ('scale', S[ix, j])):
                    if not moving[prop][j]:
                        continue
                    still = np.abs(vals - vals[:1]).max() <= EPS
                    add(joint_nodes[j], prop, vals, constant=still)
            for node, (mob, w0) in hideable.items():
                keys = []
                for f, tm in zip(c['_frames'], times):
                    w = 0.0 if vis.mesh_visible(c['seq'], mob.name, min(f - c['offset'], c['seq']['length'])) else 1.0
                    if not keys or keys[-1][1] != w:
                        keys.append((tm, w))
                kt = np.array([k[0] for k in keys], np.float32)
                add(node, 'weights', np.array([k[1] for k in keys], np.float32), 'STEP',
                    input_acc=b.accessor(kt, 'SCALAR', FLOAT, minmax=True))
            if not channels:          # keep the (static) animation listed: one constant key
                add(joint_nodes[0], 'translation', np.repeat(T0[:1], len(times), axis=0))
            b.animation({'name': c['name'], 'channels': channels, 'samplers': samplers})

    size = b.write(path)
    report['glb_bytes'] = size
    return size


def _vis_events(vis, seq, mesh_name):
    """Frames (relative to the sequence) where a mesh's visibility may change."""
    events = {0.0}
    for bn in vis.batch_bones.get(mesh_name, []):
        events.update(f for f, _ in seq.get('vis_channels', {}).get(bn, []) if 0 <= f <= seq['length'])
    return sorted(events)
