"""Generate small .glb test files, one per glTF feature, to test which ones an importer reads.

    python3 make_tests.py <output folder>

Needs numpy and Pillow. See check.py to run them through Blender."""
import sys, io, copy, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
import numpy as np
from PIL import Image
from gltf_writer import GLTFBuilder, FLOAT, USHORT, ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER

OUT = sys.argv[1]

def png(color, size=16):
    a = np.zeros((size, size, 4), np.uint8); a[...] = color
    a[: size // 2, : size // 2] = (255, 255, 255, 255)
    b = io.BytesIO(); Image.fromarray(a).save(b, 'PNG'); return b.getvalue()

def base(mat_fn, anim_fn=None, morph=False, node_ext=None, variants=False):
    b = GLTFBuilder('glTF support test')
    pos = np.array([[-1, 0, -1], [1, 0, -1], [1, 0, 1], [-1, 0, 1]], np.float32)
    nrm = np.tile([0, 1, 0], (4, 1)).astype(np.float32)
    uv = np.array([[0, 1], [1, 1], [1, 0], [0, 0]], np.float32)
    tan = np.tile([1, 0, 0, 1], (4, 1)).astype(np.float32)
    idx = np.array([0, 2, 1, 0, 3, 2], np.uint16)
    tex = {k: b.texture(png(c), k, k) for k, c in (('color', (200, 30, 30, 255)), ('emis', (30, 200, 30, 255)),
                                                    ('normal', (128, 128, 255, 255)), ('spec', (90, 90, 90, 255)))}
    mat = mat_fn(tex)
    mi = b.material(mat)
    prim = {'attributes': {'POSITION': b.accessor(pos, 'VEC3', FLOAT, ARRAY_BUFFER, minmax=True),
                           'NORMAL': b.accessor(nrm, 'VEC3', FLOAT, ARRAY_BUFFER),
                           'TANGENT': b.accessor(tan, 'VEC4', FLOAT, ARRAY_BUFFER),
                           'TEXCOORD_0': b.accessor(uv, 'VEC2', FLOAT, ARRAY_BUFFER)},
            'indices': b.accessor(idx, 'SCALAR', USHORT, ELEMENT_ARRAY_BUFFER), 'material': mi}
    mesh = {'name': 'Quad', 'primitives': [prim]}
    if morph:
        prim['targets'] = [{'POSITION': b.accessor(-pos, 'VEC3', FLOAT, ARRAY_BUFFER, minmax=True)}]
        mesh['weights'] = [0.0]
    if variants:
        mat2 = copy.deepcopy(mat); mat2['name'] = 'Blue'; mi2 = b.material(mat2)
        prim['extensions'] = {'KHR_materials_variants': {'mappings': [{'material': mi, 'variants': [0]}, {'material': mi2, 'variants': [1]}]}}
        b.doc['extensions'] = {'KHR_materials_variants': {'variants': [{'name': 'Red'}, {'name': 'Blue'}]}}
        b.extension('KHR_materials_variants')
    node = b.node(name='Quad', mesh=b.mesh(mesh))
    if node_ext:
        b.doc['nodes'][node]['extensions'] = node_ext
        for e in node_ext: b.extension(e)
    b.doc['scenes'][0]['nodes'] = [node]
    if anim_fn:
        t = b.accessor(np.array([0, 1], np.float32), 'SCALAR', FLOAT, minmax=True)
        chans = []
        samplers = []
        for target, vals in anim_fn():
            vals = np.asarray(vals, np.float32)
            typ = {1: 'SCALAR', 2: 'VEC2', 3: 'VEC3', 4: 'VEC4'}[vals.shape[1]]
            interp = 'STEP' if target == 'weights' else 'LINEAR'
            samplers.append({'input': t, 'output': b.accessor(vals, typ), 'interpolation': interp})
            if target == 'weights':
                tg = {'node': node, 'path': 'weights'}
            else:
                tg = {'path': 'pointer', 'extensions': {'KHR_animation_pointer': {'pointer': target}}}
                b.extension('KHR_animation_pointer')
            chans.append({'sampler': len(samplers) - 1, 'target': tg})
        b.animation({'name': 'Anim', 'channels': chans, 'samplers': samplers})
    for m in b.doc['materials']:
        for e in m.get('extensions', {}): b.extension(e)
        for k in ('emissiveTexture', 'normalTexture'):
            if 'extensions' in m.get(k, {}): b.extension('KHR_texture_transform')
        for ti in (m['pbrMetallicRoughness'].get('baseColorTexture', {}),
                   m.get('extensions', {}).get('KHR_materials_specular', {}).get('specularColorTexture', {})):
            if 'extensions' in ti: b.extension('KHR_texture_transform')
    return b

TT = {'KHR_texture_transform': {'offset': [0.25, 0.0], 'rotation': 0.0, 'scale': [1.0, 1.0]}}
def lit(t, tt=(), emis=True, normal=True, spec=False, unlit=False, factor=None):
    m = {'name': 'Mat', 'pbrMetallicRoughness': {'baseColorTexture': {'index': t['color']}, 'metallicFactor': 0.0, 'roughnessFactor': 0.5}}
    if factor is not None: m['pbrMetallicRoughness']['baseColorFactor'] = factor
    if emis: m['emissiveTexture'] = {'index': t['emis']}; m['emissiveFactor'] = [1, 1, 1]
    if normal: m['normalTexture'] = {'index': t['normal']}
    if spec: m.setdefault('extensions', {})['KHR_materials_specular'] = {'specularColorTexture': {'index': t['spec']}, 'specularColorFactor': [2, 2, 2]}
    if unlit:
        m.setdefault('extensions', {})['KHR_materials_unlit'] = {}
        m.pop('emissiveTexture', None); m.pop('emissiveFactor', None); m.pop('normalTexture', None)
    for slot in tt:
        ti = {'base': m['pbrMetallicRoughness'].get('baseColorTexture'), 'emissive': m.get('emissiveTexture'),
              'normal': m.get('normalTexture'),
              'specular': m.get('extensions', {}).get('KHR_materials_specular', {}).get('specularColorTexture')}[slot]
        ti['extensions'] = copy.deepcopy(TT)
    return m

P = {'base': '/materials/0/pbrMetallicRoughness/baseColorTexture/extensions/KHR_texture_transform/offset',
     'emissive': '/materials/0/emissiveTexture/extensions/KHR_texture_transform/offset',
     'normal': '/materials/0/normalTexture/extensions/KHR_texture_transform/offset',
     'specular': '/materials/0/extensions/KHR_materials_specular/specularColorTexture/extensions/KHR_texture_transform/offset'}
OFF = [[0, 0], [0.5, 0]]
tests = {
    'core_lit_textured': lambda: base(lambda t: lit(t)),
    'core_morph_weights_anim': lambda: base(lambda t: lit(t), lambda: [('weights', [[0], [1]])], morph=True),
    'unlit': lambda: base(lambda t: lit(t, unlit=True)),
    'specular': lambda: base(lambda t: lit(t, spec=True)),
    'emissive_strength': lambda: base(lambda t: dict(lit(t), extensions={'KHR_materials_emissive_strength': {'emissiveStrength': 4.0}})),
    'variants': lambda: base(lambda t: lit(t), variants=True),
    'texture_transform_static_base': lambda: base(lambda t: lit(t, tt=['base'])),
    'texture_transform_static_emissive': lambda: base(lambda t: lit(t, tt=['emissive'])),
    'texture_transform_static_normal': lambda: base(lambda t: lit(t, tt=['normal'])),
    'texture_transform_static_specular': lambda: base(lambda t: lit(t, spec=True, tt=['specular'])),
    'texture_transform_static_unlit_base': lambda: base(lambda t: lit(t, unlit=True, tt=['base'])),
    'node_visibility_static': lambda: base(lambda t: lit(t), node_ext={'KHR_node_visibility': {'visible': False}}),
    'pointer_tt_lit_base': lambda: base(lambda t: lit(t, tt=['base']), lambda: [(P['base'], OFF)]),
    'pointer_tt_unlit_base': lambda: base(lambda t: lit(t, unlit=True, tt=['base']), lambda: [(P['base'], OFF)]),
    'pointer_tt_emissive': lambda: base(lambda t: lit(t, tt=['emissive']), lambda: [(P['emissive'], OFF)]),
    'pointer_tt_normal': lambda: base(lambda t: lit(t, tt=['normal']), lambda: [(P['normal'], OFF)]),
    'pointer_tt_specular': lambda: base(lambda t: lit(t, spec=True, tt=['specular']), lambda: [(P['specular'], OFF)]),
    'pointer_basecolorfactor_lit_textured': lambda: base(lambda t: lit(t), lambda: [('/materials/0/pbrMetallicRoughness/baseColorFactor', [[1, 1, 1, 1], [.2, .2, .2, 1]])]),
    'pointer_basecolorfactor_lit_untextured': lambda: base(lambda t: dict(lit(t, emis=False, normal=False), pbrMetallicRoughness={'baseColorFactor': [1, 0, 0, 1]}), lambda: [('/materials/0/pbrMetallicRoughness/baseColorFactor', [[1, 0, 0, 1], [0, 0, 1, 1]])]),
    'pointer_basecolorfactor_unlit': lambda: base(lambda t: lit(t, unlit=True), lambda: [('/materials/0/pbrMetallicRoughness/baseColorFactor', [[1, 1, 1, 1], [.2, .2, .2, 1]])]),
    'pointer_basecolorfactor_unlit_blend': lambda: base(lambda t: dict(lit(t, unlit=True), alphaMode='BLEND'), lambda: [('/materials/0/pbrMetallicRoughness/baseColorFactor', [[1, 1, 1, 1], [1, 1, 1, .2]])]),
    'pointer_basecolorfactor_lit_textured_blend': lambda: base(lambda t: dict(lit(t), alphaMode='BLEND'), lambda: [('/materials/0/pbrMetallicRoughness/baseColorFactor', [[1, 1, 1, 1], [.2, .2, .2, .5]])]),
    'pointer_tt_unlit_base_blend': lambda: base(lambda t: dict(lit(t, unlit=True, tt=['base']), alphaMode='BLEND'), lambda: [(P['base'], OFF)]),
    'pointer_emissivefactor_untextured': lambda: base(lambda t: dict(lit(t, emis=False), emissiveFactor=[1, 1, 1]), lambda: [('/materials/0/emissiveFactor', [[1, 1, 1], [0, 0, 0]])]),
    'pointer_emissivefactor': lambda: base(lambda t: lit(t), lambda: [('/materials/0/emissiveFactor', [[1, 1, 1], [0, 0, 0]])]),
    'pointer_roughnessfactor': lambda: base(lambda t: lit(t), lambda: [('/materials/0/pbrMetallicRoughness/roughnessFactor', [[0.2], [0.9]])]),
    'pointer_node_visibility': lambda: base(lambda t: lit(t), lambda: [('/nodes/0/extensions/KHR_node_visibility/visible', [[1], [0]])], node_ext={'KHR_node_visibility': {'visible': True}}),
}
os.makedirs(OUT, exist_ok=True)
for name, fn in tests.items():
    fn().write(os.path.join(OUT, name + '.glb'))
print(len(tests), 'files')
