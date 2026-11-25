"""
Blender materials for StarCraft II (.m3) models. Imported by blender_convert.py, runs inside Blender.

Two kinds of material are built:

* export materials - a plain Principled BSDF that Blender's COLLADA / OBJ exporters understand.
  Only textures that use the first UV set with no UV transform are wired in, because those formats
  can't express anything else (a decal on UV set 2 would otherwise be mapped with the wrong UVs).

* SC2 materials - used in the saved .blend. They mimic SC2's (pre-PBR) shading instead:
      Lambert diffuse (diffuse map, team colour, decal)
    + coloured specular highlight (specular map x HDR spec, roughness from "specularity" / gloss map)
    + environment reflection (envio map looked up by the reflection vector, x envio mask)
    + emissive layers
  with per-layer UV sources (UV sets 1-4, planar/tri-planar, reflection, screen), UV transforms,
  channel selection, fresnel, alpha masks and blend modes, and layer animations (scrolling /
  rotating UVs, brightness) keyed on the same timeline as the skeleton.
"""
import math
import os

import bpy

# M3 standard-material layer slot -> role
LAYER_ROLES = [
    ('layer_diff', 'diffuse'),
    ('layer_decal', 'decal'),
    ('layer_spec', 'specular'),
    ('layer_gloss', 'gloss'),
    ('layer_emis1', 'emissive'),
    ('layer_emis2', 'emissive2'),
    ('layer_envi', 'envio'),
    ('layer_envi_mask', 'envio_mask'),
    ('layer_alpha1', 'alpha'),
    ('layer_alpha2', 'alpha2'),
    ('layer_norm', 'normal'),
    ('layer_height', 'height'),
    ('layer_light', 'lightmap'),
    ('layer_ao', 'ao'),
    ('layer_strength', 'strength'),     # displacement / reflection materials
    ('layer_blur', 'blur'),
    ('layer_color', 'diffuse'),         # volume materials
]
DISTORTION_TYPES = {'m3_materials_displacement', 'm3_materials_reflection'}
VOLUME_TYPES = {'m3_materials_volume', 'm3_materials_volumenoise'}
INVISIBLE_TYPES = {'m3_materials_lensflare', 'm3_materials_buffer'}
# effect-only materials: no visible surface of their own in game -> fully transparent in DAE/OBJ
EXPORT_TRANSPARENT_TYPES = DISTORTION_TYPES | VOLUME_TYPES | INVISIBLE_TYPES
ANIMATED_MATERIAL_PROPS = ('strength_factor', 'density')
UV_SET = {'UV0': 'uv0', 'UV1': 'uv1', 'UV2': 'uv2', 'UV3': 'uv3', 'PARTICLE': 'uv0'}
ENV_SOURCES = {'REFCUBE', 'REFSPHERE', 'CUBE', 'SPHERE'}
ANIMATED_LAYER_PROPS = ('uv_offset', 'uv_angle', 'uv_tiling', 'color_brightness', 'color_multiply',
                        'color_add', 'color_value')
TEAM_COLOR_LINEAR = (0.47, 0.006, 0.012)   # red, SC2's player-1 colour

# Settings shown under Scene Properties > Custom Properties; every material reads them.
SCENE_SETTINGS = [
    ('team_color', TEAM_COLOR_LINEAR,
     dict(subtype='COLOR', min=0.0, max=1.0, description='Team colour used by every material')),
    ('specular_strength', 1.0, dict(min=0.0, soft_max=4.0, description='Multiplier on specular highlights')),
    ('reflection_strength', 1.0, dict(min=0.0, soft_max=4.0, description='Multiplier on environment reflections')),
    ('emission_strength', 1.0, dict(min=0.0, soft_max=4.0, description='Multiplier on emissive glow')),
    ('distortion_visibility', 0.15, dict(min=0.0, soft_max=2.0,
                                          description='How visible distortion effects (warp shimmer, heat haze) are')),
    ('sun_direction', None, dict(subtype='DIRECTION', description='Direction towards the sun, used for '
                                 'specular highlights. Driven by the "Light" object')),
]
SUN_EULER = (0.7, 0.0, 0.3)    # rotation of the "Light" sun lamp: from the front-top, like the default view


def norm_tex_key(path):
    return path.replace('/', '\\').strip().lower()


# --------------------------------------------------------------------------------------
# Reading the M3 material
# --------------------------------------------------------------------------------------

def m3_material_from_ref(ob, matref, shared, depth=0):
    """Resolve an M3 material reference to (material, type) - following composite materials."""
    if matref is None or depth > 4:
        return None, None
    coll = getattr(ob, matref.mat_type, None)
    if coll is None:
        return None, None
    mat = shared.m3_pointer_get(coll, matref.mat_handle)
    if mat is None:
        return None, None
    if matref.mat_type == 'm3_materials_composite':
        for section in mat.sections:
            sub = shared.m3_pointer_get(ob.m3_materialrefs, section.material)
            m, t = m3_material_from_ref(ob, sub, shared, depth + 1)
            if m is not None:
                return m, t
        return None, None
    return mat, matref.mat_type


def collect_layers(ob, m3mat, shared):
    """{role: M3 layer} for the layers of a material that carry a bitmap or a constant colour."""
    out = {}
    if m3mat is None:
        return out
    roles = list(LAYER_ROLES)
    if not any(hasattr(m3mat, a) for a, _ in roles):   # terrain / volume / creep ... materials
        roles = [(a, 'diffuse') for a in dir(m3mat) if a.startswith('layer_')]
    for attr, role in roles:
        handle = getattr(m3mat, attr, None)
        if not isinstance(handle, str) or not handle or role in out:
            continue
        layer = shared.m3_pointer_get(ob.m3_materiallayers, handle)
        if layer is None:
            continue
        if layer_bitmap(layer) or getattr(layer, 'color_type', 'BITMAP') == 'COLOR':
            out[role] = layer
    return out


TERRAIN_STANDIN_COLOR = (0.22, 0.17, 0.12)   # linear; a neutral earth brown, when no texture is found


class StandInLayer:
    """Looks like an M3 layer to the material code. Used for terrain materials, which have no
    texture of their own: in game they show the map's terrain textures, so a stand-in terrain
    texture (or colour) is mapped onto them instead."""
    color_channels = 'RGB'
    uv_source = 'UV0'
    uv_offset = (0.0, 0.0)
    uv_tiling = (1.0, 1.0)
    uv_angle = (0.0, 0.0, 0.0)
    uv_wrap_x = uv_wrap_y = True
    uv_flipbook_rows = uv_flipbook_cols = 0
    color_invert = color_clamp = False
    color_brightness = color_multiply = 1.0
    color_add = 0.0
    fresnel_type = 'DISABLED'
    fresnel_exponent, fresnel_min, fresnel_max = 1.0, 0.0, 1.0

    def __init__(self, role, bitmap='', color=TERRAIN_STANDIN_COLOR):
        self.role = role
        self.color_type = 'BITMAP' if bitmap else 'COLOR'
        self.color_bitmap = bitmap or ''
        self.color_value = tuple(color[:3]) + (1.0,)

    def path_from_id(self, prop=None):
        return f'terrain_standin_{self.role}' + (f'.{prop}' if prop else '')


def terrain_standin_layers(terrain):
    """{role: layer} for a terrain material, from the job's terrain settings
    ({'diffuse': game texture path or None, 'normal': ... or None})."""
    layers = {'diffuse': StandInLayer('diffuse', terrain.get('diffuse') or '',
                                      terrain.get('color') or TERRAIN_STANDIN_COLOR)}
    if terrain.get('diffuse') and terrain.get('normal'):
        layers['normal'] = StandInLayer('normal', terrain['normal'])
    return layers


def layer_bitmap(layer):
    if getattr(layer, 'color_type', 'BITMAP') != 'BITMAP':
        return ''
    return (getattr(layer, 'color_bitmap', '') or '').strip()


def layer_path(layer):
    return layer.path_from_id()          # e.g. 'm3_materiallayers[3]'


def layer_uv_is_plain(layer, animated_paths=()):
    """True when the layer samples UV set 1 with an identity, non-animated UV transform."""
    if layer.uv_source not in ('UV0', 'PARTICLE'):
        return False
    if any(abs(v) > 1e-6 for v in layer.uv_offset) or any(abs(v - 1) > 1e-6 for v in layer.uv_tiling) \
            or any(abs(v) > 1e-6 for v in layer.uv_angle) or layer.uv_flipbook_rows > 1 or layer.uv_flipbook_cols > 1:
        return False
    lp = layer_path(layer)
    return not any(p.startswith(lp + '.uv_') for p in animated_paths)


def load_image(png_path, non_color=False):
    if not png_path or not os.path.isfile(png_path):
        return None
    img = bpy.data.images.load(os.path.abspath(png_path), check_existing=True)
    img.alpha_mode = 'CHANNEL_PACKED'      # SC2 alpha channels are data (team colour, normal X...)
    if non_color:
        try:
            img.colorspace_settings.name = 'Non-Color'
        except TypeError:
            pass
    return img


# --------------------------------------------------------------------------------------
# Shared node groups
# --------------------------------------------------------------------------------------

def _math(nt, op, a, b=None, loc=(0, 0), clamp=False, c=None):
    n = nt.nodes.new('ShaderNodeMath')
    n.operation = op
    n.location = loc
    n.use_clamp = clamp
    for i, v in enumerate((a, b, c)):
        if v is None:
            continue
        if isinstance(v, (int, float)):
            n.inputs[i].default_value = v
        else:
            nt.links.new(v, n.inputs[i])
    return n.outputs[0]


def sc2_normal_decode_group():
    """SC2 normal texture -> standard (OpenGL-convention) tangent-space normal colour.
    'Packed (DXT5nm)' = 1: X is in the alpha channel, Y in green, Z rebuilt; 0: plain RGB normal map.
    SC2 normal maps are DirectX-style (green points down), so 'Flip Green' defaults to 1."""
    name = 'SC2 Normal Decode'
    ng = bpy.data.node_groups.get(name)
    if ng is not None:
        return ng
    ng = bpy.data.node_groups.new(name, 'ShaderNodeTree')
    iface = ng.interface
    iface.new_socket('Color', in_out='INPUT', socket_type='NodeSocketColor')
    iface.new_socket('Alpha', in_out='INPUT', socket_type='NodeSocketFloat')
    flip = iface.new_socket('Flip Green', in_out='INPUT', socket_type='NodeSocketFloat')
    flip.default_value, flip.min_value, flip.max_value = 1.0, 0.0, 1.0
    packed = iface.new_socket('Packed (DXT5nm)', in_out='INPUT', socket_type='NodeSocketFloat')
    packed.default_value, packed.min_value, packed.max_value = 1.0, 0.0, 1.0
    iface.new_socket('Normal Color', in_out='OUTPUT', socket_type='NodeSocketColor')
    N, L = ng.nodes, ng.links
    gin, gout = N.new('NodeGroupInput'), N.new('NodeGroupOutput')
    gin.location, gout.location = (-900, 0), (700, 0)
    sep = N.new('ShaderNodeSeparateColor')
    sep.location = (-700, 100)
    L.new(gin.outputs['Color'], sep.inputs['Color'])
    g = sep.outputs['Green']
    one_minus_2g = _math(ng, 'MULTIPLY_ADD', g, -2.0, c=1.0, loc=(-500, -150))          # 1 - 2g
    g_flip = _math(ng, 'ADD', g, _math(ng, 'MULTIPLY', gin.outputs['Flip Green'], one_minus_2g,
                                       loc=(-300, -100)), loc=(-100, 0))               # g or 1-g
    # X source: alpha (packed) or red (plain):  r + packed * (alpha - r)
    x_src = _math(ng, 'ADD', sep.outputs['Red'], _math(ng, 'MULTIPLY', gin.outputs['Packed (DXT5nm)'],
                  _math(ng, 'SUBTRACT', gin.outputs['Alpha'], sep.outputs['Red'], loc=(-300, 350)), loc=(-150, 350)),
                  loc=(0, 350))
    x = _math(ng, 'MULTIPLY_ADD', x_src, 2.0, c=-1.0, loc=(150, 250))
    y = _math(ng, 'MULTIPLY_ADD', g_flip, 2.0, c=-1.0, loc=(150, 0))
    xx = _math(ng, 'MULTIPLY', x, x, loc=(300, 250))
    yy = _math(ng, 'MULTIPLY', y, y, loc=(300, 0))
    z = _math(ng, 'SQRT', _math(ng, 'SUBTRACT', _math(ng, 'SUBTRACT', 1.0, xx, loc=(450, 250)), yy,
                                loc=(450, 100), clamp=True), loc=(450, -50))
    comb = N.new('ShaderNodeCombineColor')
    comb.location = (550, 0)
    L.new(x_src, comb.inputs['Red'])
    L.new(g_flip, comb.inputs['Green'])
    L.new(_math(ng, 'MULTIPLY_ADD', z, 0.5, c=0.5, loc=(500, -200)), comb.inputs['Blue'])
    L.new(comb.outputs['Color'], gout.inputs['Normal Color'])
    return ng


_PACKED_CACHE = {}


def normal_is_packed(img):
    """True for SC2's packed "DXT5nm" normal maps (X in alpha, blue unused), False for plain RGB
    normal maps (blue = Z, mostly near 1, alpha constant)."""
    if img is None:
        return True
    if img.name in _PACKED_CACHE:
        return _PACKED_CACHE[img.name]
    try:
        px = _pixels(img)[::4, ::4]
        blue, alpha = px[..., 2], px[..., 3]
        plain = blue.mean() > 0.6 and alpha.std() < 0.02
        packed = not plain
    except Exception:
        packed = True
    _PACKED_CACHE[img.name] = packed
    return packed


SETTINGS_OUTPUTS = (('Team Color', 'team_color'), ('Specular Strength', 'specular_strength'),
                    ('Reflection Strength', 'reflection_strength'), ('Emission Strength', 'emission_strength'),
                    ('Distortion Visibility', 'distortion_visibility'), ('Sun Direction', 'sun_direction'))


def settings_group():
    """Node group shared by every material. It reads the scene's custom properties (team_color,
    specular_strength, ...) through Attribute nodes, so they can be changed in the Scene tab."""
    name = 'SC2 Settings'
    ng = bpy.data.node_groups.get(name)
    if ng is not None:
        return ng
    ng = bpy.data.node_groups.new(name, 'ShaderNodeTree')
    out = ng.nodes.new('NodeGroupOutput')
    out.location = (300, 0)
    for i, (label, prop) in enumerate(SETTINGS_OUTPUTS):
        kind = {'team_color': 'NodeSocketColor', 'sun_direction': 'NodeSocketVector'}.get(prop, 'NodeSocketFloat')
        ng.interface.new_socket(label, in_out='OUTPUT', socket_type=kind)
        n = ng.nodes.new('ShaderNodeAttribute')
        n.attribute_type = 'VIEW_LAYER'          # looks up the view layer, then the scene
        n.attribute_name = prop
        n.label = f'Scene property "{prop}"'
        n.location = (0, -i * 220)
        outname = {'team_color': 'Color', 'sun_direction': 'Vector'}.get(prop, 'Fac')
        ng.links.new(n.outputs[outname], out.inputs[label])
    return ng


# --------------------------------------------------------------------------------------
# Export material (Principled) - for COLLADA / OBJ
# --------------------------------------------------------------------------------------

def static_uv(layer):
    """(uv set name or None for non-UV sources, offset, tiling, z-angle) of a layer at rest."""
    src = layer.uv_source
    uvset = UV_SET.get(src)
    return uvset, (layer.uv_offset[0], layer.uv_offset[1]), (layer.uv_tiling[0], layer.uv_tiling[1]), layer.uv_angle[2]


def _offset_equal(a, b, wrap):
    if wrap:   # with a repeating texture, whole-number offsets change nothing
        return all(abs((x - y) - round(x - y)) < 1e-4 for x, y in zip(a, b))
    return all(abs(x - y) < 1e-4 for x, y in zip(a, b))


def same_mapping(a, b):
    sa, sb = static_uv(a), static_uv(b)
    wrap = a.uv_wrap_x and a.uv_wrap_y and b.uv_wrap_x and b.uv_wrap_y
    return (sa[0] == sb[0] and _offset_equal(sa[1], sb[1], wrap)
            and all(abs(x - y) < 1e-4 for x, y in zip(sa[2], sb[2])) and abs(sa[3] - sb[3]) < 1e-4)


def mapping_is_identity(layer):
    uvset, off, tile, ang = static_uv(layer)
    if uvset is None:
        return True          # planar / reflection / screen: nothing we could bake anyway
    return (uvset == 'uv0' and _offset_equal(off, (0, 0), layer.uv_wrap_x and layer.uv_wrap_y)
            and all(abs(t - 1) < 1e-4 for t in tile) and abs(ang) < 1e-4)


def transform_uvs(uvs, layer):
    """Apply a layer's rest UV transform (M3 texture space: V down, pivot at the texture centre) to a
    flat [u0, v0, u1, v1...] list."""
    _, (ox, oy), (tx, ty), ang = static_uv(layer)
    c, s_ = math.cos(ang), math.sin(ang)
    out = []
    for i in range(0, len(uvs), 2):
        x, y = (uvs[i] - 0.5) * tx, (0.5 - uvs[i + 1]) * ty          # centred M3 space (V down)
        x, y = x * c - y * s_ + ox, x * s_ + y * c + oy
        out += (x + 0.5, 0.5 - y)
    return out


def export_plan(layers, m3mat, mtype):
    """Decide how a material is represented in COLLADA/OBJ, which only know "a colour texture
    with optional alpha". Picks the main texture (diffuse, else the emissive glow), and how its
    alpha should behave."""
    plan = {'main': None, 'role': None, 'alpha': None, 'alpha_layer': None, 'glow': False,
            'transparent': mtype in EXPORT_TRANSPARENT_TYPES, 'const_color': None}
    if plan['transparent'] or m3mat is None:
        return plan
    diff = layers.get('diffuse')
    if diff is not None and getattr(diff, 'color_type', 'BITMAP') == 'COLOR':
        plan['const_color'] = tuple(diff.color_value[:3])
    for role in ('diffuse', 'emissive', 'decal', 'emissive2'):
        layer = layers.get(role)
        if layer is not None and layer_bitmap(layer):
            plan['main'], plan['role'] = layer, role
            break
    plan['glow'] = plan['role'] in ('emissive', 'emissive2') or bool(getattr(m3mat, 'unshaded', False))
    plan['emissive_layer'] = None
    if plan['role'] in ('diffuse', 'decal'):     # a separate glow on top of a diffuse texture
        for role, mode_attr in (('emissive', 'blend_mode_emis1'), ('emissive2', 'blend_mode_emis2')):
            el = layers.get(role)
            if el is not None and layer_bitmap(el) and getattr(m3mat, mode_attr, 'ADD') not in ('TEAMEMIS', 'TEAMDIFF') \
                    and same_mapping(el, plan['main']):
                plan['emissive_layer'] = el
                break
    plan['hdr_emis'] = float(getattr(m3mat, 'hdr_emis', 1.0) or 1.0)
    plan['double_sided'] = bool(getattr(m3mat, 'two_sided', False))
    # for the glTF export: specular map, team colour
    main = plan['main']
    spec = layers.get('specular')
    plan['spec_layer'] = spec if (spec is not None and layer_bitmap(spec) and main is not None
                                  and same_mapping(spec, main)) else None
    plan['specularity'] = float(getattr(m3mat, 'specularity', 20.0) or 20.0)
    plan['hdr_spec'] = float(getattr(m3mat, 'hdr_spec', 1.0) or 1.0)
    plan['spec_mode'] = getattr(m3mat, 'spec_mode', 'RGB')
    plan['lit'] = not plan['glow'] and plan['role'] in ('diffuse', 'decal')
    plan['team_mask'] = plan['role'] == 'diffuse' and getattr(main, 'color_channels', '') == 'ARGB'
    plan['team_emis'] = []
    if plan['role'] in ('diffuse', 'decal'):
        for role, mode_attr in (('emissive', 'blend_mode_emis1'), ('emissive2', 'blend_mode_emis2')):
            el = layers.get(role)
            if el is not None and layer_bitmap(el) and getattr(m3mat, mode_attr, 'ADD') in ('TEAMEMIS', 'TEAMDIFF') \
                    and same_mapping(el, main):
                plan['team_emis'].append(el)
    plan['alpha_cutoff'] = (getattr(m3mat, 'alpha_test_threshold', 0) or 128) / 255.0
    blend = getattr(m3mat, 'blend_mode', 'OPAQUE')
    if blend in ('ADD', 'ALPHAA'):
        plan['alpha'] = 'additive'           # adds light: black = see-through
    else:
        al = layers.get('alpha')
        if al is not None and layer_bitmap(al):
            plan['alpha'] = 'blend' if blend == 'ALPHAB' else 'mask'
            plan['alpha_layer'] = al
    return plan


def _pixels(img):
    import numpy as np
    w, h = img.size
    arr = np.empty(w * h * 4, dtype=np.float32)
    img.pixels.foreach_get(arr)
    return arr.reshape(h, w, 4)


def layer_ops(layer, peaks=None):
    """The colour operations a layer applies on top of its texture, in the order the .blend
    material applies them: channel select -> invert -> x brightness x multiply -> + add -> clamp
    -> x fresnel (only a constant fresnel, min == max, can be baked)."""
    f = 1.0
    if layer.fresnel_type != 'DISABLED':
        f = (layer.fresnel_min + layer.fresnel_max) / 2.0
    # animated brightness/multiply/add (fade-ins, pulses, effects that are off at rest): use the
    # strongest value the animation reaches, so the baked texture shows the effect when it's on
    peaks = peaks or {}
    lp = layer_path(layer)
    get = lambda prop: peaks.get(f'{lp}.{prop}', getattr(layer, prop))
    return {'channels': layer.color_channels, 'invert': bool(layer.color_invert),
            'scale': get('color_brightness') * get('color_multiply'), 'add': get('color_add'),
            'clamp': bool(layer.color_clamp), 'fresnel': f, 'post': 1.0}


def _srgb_to_linear(x):
    import numpy as np
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(x):
    import numpy as np
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * x ** (1 / 2.4) - 0.055)


def ops_identity(ops):
    return (ops['channels'] in ('RGB', 'ARGB') and not ops['invert'] and abs(ops['scale'] - 1) < 1e-6
            and abs(ops['add']) < 1e-6 and abs(ops['fresnel'] - 1) < 1e-6 and abs(ops.get('post', 1) - 1) < 1e-6)


def _apply_ops(x, ops):
    import numpy as np
    if ops['invert']:
        x = 1.0 - x
    x = x * ops['scale'] + ops['add']
    x = np.maximum(x, 0.0)
    if ops['clamp']:
        x = np.minimum(x, 1.0)
    return np.clip(x * ops['fresnel'] * ops.get('post', 1.0), 0.0, 1.0)


def _select(px, channels):
    """RGB (h, w, 3) for a channel setting: single channels become grey."""
    import numpy as np
    idx = {'R': 0, 'G': 1, 'B': 2, 'A': 3}.get(channels)
    if idx is None:
        return px[..., :3]
    return np.repeat(px[..., idx:idx + 1], 3, axis=2)


def _scalar(px, channels):
    idx = {'R': 0, 'G': 1, 'B': 2, 'A': 3}.get(channels)
    return px[..., idx] if idx is not None else px[..., :3].mean(axis=2)


def derive_texture(out_path, main_img, main_ops, alpha_mode, alpha_img=None, alpha_ops=None):
    """Write the texture COLLADA/OBJ get for a material: the main layer with its colour operations
    baked in, and an alpha channel encoding its transparency:
       alpha_mode 'additive': alpha = brightness (max of R, G, B) - black parts of a glow vanish
       alpha_mode 'mask':     alpha = alpha_img's channel with alpha_ops (resampled to main size)
       alpha_mode None:       alpha = 1"""
    import numpy as np
    px = _pixels(main_img)
    rgb = _apply_ops(_select(px, main_ops['channels']), main_ops)
    if alpha_mode == 'additive':
        # straight alpha that reproduces the added light over black: alpha = brightest channel
        # (linear), colour = light / alpha, so viewers blending in linear space get colour x alpha
        # = the original light
        lin = _srgb_to_linear(rgb)
        a = lin.max(axis=2)
        rgb = _linear_to_srgb(lin / np.maximum(a, 1e-6)[..., None])
    elif alpha_mode == 'mask':
        src = _pixels(alpha_img)
        if src.shape[:2] != px.shape[:2]:
            ys = (np.arange(px.shape[0]) * src.shape[0] // px.shape[0])
            xs = (np.arange(px.shape[1]) * src.shape[1] // px.shape[1])
            src = src[ys][:, xs]
        a = _apply_ops(_scalar(src, alpha_ops['channels']), alpha_ops)
    else:
        a = np.ones(rgb.shape[:2], dtype=np.float32)
    out_px = np.concatenate([rgb, a[..., None]], axis=2).astype(np.float32)
    h, w = out_px.shape[:2]
    out = bpy.data.images.new(os.path.splitext(os.path.basename(out_path))[0], w, h, alpha=True)
    out.alpha_mode = 'STRAIGHT'
    out.pixels.foreach_set(out_px.ravel())
    out.filepath_raw = out_path
    out.file_format = 'PNG'
    out.save()
    return out


_DERIVED = {}   # (out_dir, signature) -> png path, so identical derived textures are written once


def build_export_material(name, plan, tex_map, report, out_dir, normal_layer=None, normal_mode='raw',
                          peaks=None):
    """Principled material for Blender's COLLADA/OBJ exporters. Returns (material, info) where info
    tells postprocess_dae what Blender's exporter can't write ({'alpha': bool, 'glow': bool})."""
    info = {'alpha': False, 'glow': False, 'emission_node': None, 'emission_file': None,
            # for the glTF export
            'transparent': plan['transparent'], 'const_color': plan['const_color'],
            'double_sided': plan.get('double_sided', False), 'alpha_cutoff': plan.get('alpha_cutoff', 0.5),
            'base_image': None, 'alpha_kind': None, 'unlit': False, 'emission_image': None,
            'normal_image': None, 'main_layer': None, 'main_raw_image': None, 'main_ops': None,
            'emissive_layer': None, 'emissive_ops': None, 'normal_layer': None,
            'spec_layer': None, 'spec_image': None, 'spec_ops': None,
            'specularity': plan.get('specularity', 20.0), 'hdr_spec': plan.get('hdr_spec', 1.0),
            'spec_mode': plan.get('spec_mode', 'RGB'), 'lit': plan.get('lit', False),
            'team_mask': plan.get('team_mask', False), 'team_emis': []}
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next(n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED')
    bsdf.inputs['Base Color'].default_value = (0.0, 0.0, 0.0, 1.0)   # SC2: no diffuse layer = black
    if plan['transparent']:
        bsdf.inputs['Alpha'].default_value = 0.0                    # distortion / volume / flare only
        return mat, info
    if plan['const_color'] is not None:
        bsdf.inputs['Base Color'].default_value = plan['const_color'] + (1.0,)
    main = plan['main']
    if main is None:
        return mat, info
    png = tex_map.get(norm_tex_key(layer_bitmap(main)))
    img = load_image(png)
    if img is None:
        report['missing_textures'].add(layer_bitmap(main))
        return mat, info

    stem = os.path.splitext(os.path.basename(png))[0]
    main_ops = layer_ops(main, peaks)
    if plan['role'] in ('emissive', 'emissive2'):
        main_ops['post'] = plan.get('hdr_emis', 1.0)   # glow strength, capped at full brightness
    alpha_mode, alpha_img, alpha_ops = None, None, None
    if plan['alpha'] == 'additive':
        alpha_mode = 'additive'
    elif plan['alpha'] in ('mask', 'blend'):
        al = plan['alpha_layer']
        al_png = tex_map.get(norm_tex_key(layer_bitmap(al)))
        alpha_img = load_image(al_png)
        if alpha_img is None:
            report['missing_textures'].add(layer_bitmap(al))
        elif not same_mapping(al, main):
            report.setdefault('not_in_export', []).append(f'{name}:alpha (different UV mapping)')
            alpha_img = None
        else:
            alpha_mode, alpha_ops = 'mask', layer_ops(al, peaks)

    use_img, has_alpha = img, False
    # the plain texture works as-is when nothing needs baking
    plain_alpha = (alpha_mode == 'mask' and alpha_img == img and alpha_ops['channels'] == 'A'
                   and not alpha_ops['invert'] and abs(alpha_ops['scale'] - 1) < 1e-6
                   and abs(alpha_ops['add']) < 1e-6 and abs(alpha_ops['fresnel'] - 1) < 1e-6)
    if ops_identity(main_ops) and (alpha_mode is None or plain_alpha):
        has_alpha = alpha_mode is not None
    else:
        kind = {'additive': 'Additive', 'mask': 'Alpha', None: 'Color'}[alpha_mode]
        sig = (out_dir, stem, kind, tuple(sorted(main_ops.items())),
               os.path.basename(al_png) if alpha_img is not None else None,
               tuple(sorted(alpha_ops.items())) if alpha_ops else None)
        path = _DERIVED.get(sig)
        try:
            if path is None:
                path = os.path.join(out_dir, f'{stem}_{kind}.png')
                k = 2
                while path in _DERIVED.values():
                    path = os.path.join(out_dir, f'{stem}_{kind}{k}.png')
                    k += 1
                derive_texture(path, img, main_ops, alpha_mode, alpha_img, alpha_ops)
                _DERIVED[sig] = path
            use_img = load_image(path)
            has_alpha = alpha_mode is not None
        except Exception as e:   # never lose the export over a derived texture
            report.setdefault('warnings', []).append(f'{name}: derived texture failed: {e!r}')
            use_img, has_alpha = img, False

    info['base_image'] = use_img
    info['alpha_kind'] = plan['alpha'] if has_alpha else None
    info['unlit'] = bool(plan['glow'])
    info.update(main_layer=main, main_raw_image=img, main_ops=main_ops)
    sl = plan.get('spec_layer')
    if sl is not None:
        simg = load_image(tex_map.get(norm_tex_key(layer_bitmap(sl))))
        if simg is not None:
            info.update(spec_layer=sl, spec_image=simg, spec_ops=layer_ops(sl, peaks))
    for tl in plan.get('team_emis', []):
        timg = load_image(tex_map.get(norm_tex_key(layer_bitmap(tl))))
        if timg is not None:
            tops = layer_ops(tl, peaks)
            tops['post'] = plan.get('hdr_emis', 1.0)
            info['team_emis'].append((tl, timg, tops))
    tex = nt.nodes.new('ShaderNodeTexImage')
    tex.image = use_img
    tex.location = (-600, 300)
    nt.links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
    if has_alpha:
        nt.links.new(tex.outputs['Alpha'], bsdf.inputs['Alpha'])
        if hasattr(mat, 'surface_render_method'):
            mat.surface_render_method = 'BLENDED' if plan['alpha'] != 'mask' else 'DITHERED'
        info['alpha'] = True
    info['glow'] = plan['glow']
    info['emission_node'] = tex          # linked to Emission only for the OBJ export (see blender_convert)
    el = plan.get('emissive_layer')
    if el is not None:
        epng = tex_map.get(norm_tex_key(layer_bitmap(el)))
        eimg = load_image(epng)
        if eimg is not None:
            eops = layer_ops(el, peaks)
            eops['post'] = plan.get('hdr_emis', 1.0)
            try:
                if ops_identity(eops):
                    use_e = eimg
                else:
                    estem = os.path.splitext(os.path.basename(epng))[0]
                    sig = (out_dir, estem, 'Emission', tuple(sorted(eops.items())))
                    path = _DERIVED.get(sig)
                    if path is None:
                        path = os.path.join(out_dir, f'{estem}_Emission.png')
                        k = 2
                        while path in _DERIVED.values():
                            path = os.path.join(out_dir, f'{estem}_Emission{k}.png')
                            k += 1
                        derive_texture(path, eimg, eops, None)
                        _DERIVED[sig] = path
                    use_e = load_image(path)
                info.update(emission_image=use_e, emissive_layer=el, emissive_ops=eops)
                enode = nt.nodes.new('ShaderNodeTexImage')
                enode.image = use_e
                enode.location = (-600, -300)
                info.update(glow=True, emission_node=enode,
                            emission_file=os.path.basename(bpy.path.abspath(use_e.filepath)))
            except Exception as e:
                report.setdefault('warnings', []).append(f'{name}: emission texture failed: {e!r}')
        else:
            report['missing_textures'].add(layer_bitmap(el))
    if normal_layer is not None and layer_bitmap(normal_layer) and same_mapping(normal_layer, main):
        nimg = load_image(tex_map.get(norm_tex_key(layer_bitmap(normal_layer))), non_color=True)
        info.update(normal_image=nimg, normal_layer=normal_layer)
        if nimg is not None and normal_mode == 'rgb':
            ntex = nt.nodes.new('ShaderNodeTexImage')
            ntex.image = nimg
            nmap = nt.nodes.new('ShaderNodeNormalMap')
            nt.links.new(ntex.outputs['Color'], nmap.inputs['Color'])
            nt.links.new(nmap.outputs['Normal'], bsdf.inputs['Normal'])
    return mat, info


def set_export_emission(mat, info, on):
    """OBJ (map_Ke) can carry an emission texture; Blender's COLLADA exporter would write it as a
    flat white colour instead, so it's only linked while exporting the OBJ."""
    nt = mat.node_tree
    bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if bsdf is None or not info.get('glow') or info.get('emission_node') is None:
        return
    sock = bsdf.inputs['Emission Color']
    for l in list(sock.links):
        nt.links.remove(l)
    if on:
        nt.links.new(info['emission_node'].outputs['Color'], sock)
        bsdf.inputs['Emission Strength'].default_value = 1.0
    else:
        bsdf.inputs['Emission Strength'].default_value = 0.0


# --------------------------------------------------------------------------------------
# SC2 material - for the .blend
# --------------------------------------------------------------------------------------

class SC2MaterialBuilder:
    def __init__(self, name, m3mat, layers, tex_map, equirect, report, normal_mode, animated_paths):
        self.name, self.m3mat, self.layers = name, m3mat, layers
        self.tex_map, self.equirect, self.report = tex_map, equirect, report
        self.normal_mode, self.animated = normal_mode, set(animated_paths)
        self.targets = []     # (m3 data path, m3 index, socket, socket index or None)
        self.y = 0
        self._settings = None

    # ---- helpers
    def node(self, kind, x=-400, **props):
        n = self.nt.nodes.new(kind)
        n.location = (x, self.y)
        for k, v in props.items():
            setattr(n, k, v)
        return n

    def link(self, a, b):
        self.nt.links.new(a, b)

    def math(self, op, a, b=None, c=None, clamp=False):
        return _math(self.nt, op, a, b, c=c, loc=(-300, self.y), clamp=clamp)

    def vmath(self, op, a, b=None, scale=None):
        n = self.node('ShaderNodeVectorMath', -250, operation=op)
        for i, v in enumerate((a, b)):
            if v is None:
                continue
            if isinstance(v, (tuple, list)):
                n.inputs[i].default_value = v
            else:
                self.link(v, n.inputs[i])
        if scale is not None:
            if isinstance(scale, (int, float)):
                n.inputs['Scale'].default_value = scale
            else:
                self.link(scale, n.inputs['Scale'])
        return n

    def mix(self, blend, fac, a, b):
        n = self.node('ShaderNodeMix', -150, data_type='RGBA', blend_type=blend)
        for sock, v in ((n.inputs[0], fac), (n.inputs[6], a), (n.inputs[7], b)):
            if isinstance(v, (int, float)):
                sock.default_value = v
            elif isinstance(v, tuple):
                sock.default_value = v
            else:
                self.link(v, sock)
        return n.outputs[2]

    def is_animated(self, layer, prop):
        return f'{layer_path(layer)}.{prop}' in self.animated

    def setting(self, name):
        if self._settings is None:
            self._settings = self.node('ShaderNodeGroup', -1300)
            self._settings.node_tree = settings_group()
            self._settings.label = 'SC2 Settings (shared)'
        return self._settings.outputs[name]

    def team_color(self):
        return self.setting('Team Color')

    def material_value(self, prop, default=1.0):
        """A Value node mirroring an (animatable) M3 material property."""
        v = self.node('ShaderNodeValue', -500, label=prop)
        v.outputs[0].default_value = getattr(self.m3mat, prop, default)
        self.targets.append((f'{self.m3mat.path_from_id()}.{prop}', 0, v.outputs[0], None))
        return v.outputs[0]

    def blinn_phong(self, normal, exponent):
        """Scalar Blinn-Phong highlight for the scene's sun direction (scene property sun_direction)."""
        geo = self.node('ShaderNodeNewGeometry', -900)
        n = normal if normal is not None else geo.outputs['Normal']
        light = self.vmath('NORMALIZE', self.setting('Sun Direction')).outputs[0]
        half = self.vmath('NORMALIZE', self.vmath('ADD', light, geo.outputs['Incoming']).outputs[0]).outputs[0]
        ndh = self.math('MAXIMUM', self.vmath('DOT_PRODUCT', n, half).outputs['Value'], 0.0)
        ndl = self.vmath('DOT_PRODUCT', n, light).outputs['Value']
        facing = self.math('MINIMUM', self.math('MAXIMUM', self.math('MULTIPLY', ndl, 8.0), 0.0), 1.0)
        return self.math('MULTIPLY', self.math('POWER', ndh, exponent), facing)

    def normal_socket(self):
        nrm = self.sample('normal', non_color=True)
        if nrm is None:
            return None
        nmap = self.node('ShaderNodeNormalMap', 0, uv_map=UV_SET.get(nrm['layer'].uv_source, 'uv0'))
        if self.normal_mode == 'raw':
            # SC2 uses both packed "DXT5nm" (X in alpha) and plain RGB normal maps
            png = self.tex_map.get(norm_tex_key(layer_bitmap(nrm['layer'])))
            img = load_image(png, non_color=True)
            g = self.node('ShaderNodeGroup', -150)
            g.node_tree = sc2_normal_decode_group()
            g.inputs['Packed (DXT5nm)'].default_value = 1.0 if normal_is_packed(img) else 0.0
            self.link(nrm['rgb'], g.inputs['Color'])
            self.link(nrm['alpha'], g.inputs['Alpha'])
            self.link(g.outputs['Normal Color'], nmap.inputs['Color'])
        else:   # already converted to standard OpenGL-convention RGB by sc2rip
            self.link(nrm['rgb'], nmap.inputs['Color'])
        return nmap.outputs['Normal']

    def _new_material(self):
        mat = bpy.data.materials.new(self.name)
        mat.use_nodes = True
        self.nt = mat.node_tree
        for n in list(self.nt.nodes):
            self.nt.nodes.remove(n)
        return mat, self.node('ShaderNodeOutputMaterial', 900)

    def build_distortion(self):
        """Displacement/reflection materials: SC2 renders them as a screen-space refraction shimmer
        (warp-in fields, heat haze) with no colour of their own. Approximated as a transparent
        surface with a faint normal-mapped sheen, scaled by the strength mask, the material's
        (animated) distortion factor and the scene's distortion_visibility."""
        mat, out = self._new_material()
        self.y = 0
        normal = self.normal_socket()
        strength = self.material_value('strength_factor') if hasattr(self.m3mat, 'strength_factor') \
            else self.material_value('reflection_strength')
        st = self.sample('strength')
        if st is not None:
            strength = self.math('MULTIPLY', strength, st['gray'])
        strength = self.math('MULTIPLY', strength, self.setting('Distortion Visibility'))
        comb = self.node('ShaderNodeCombineColor', 100)
        for i in range(3):
            self.link(strength, comb.inputs[i])
        gl = self.node('ShaderNodeBsdfGlossy', 300, distribution='GGX')
        gl.inputs['Roughness'].default_value = 0.15
        self.link(comb.outputs[0], gl.inputs['Color'])
        if normal is not None:
            self.link(normal, gl.inputs['Normal'])
        tr = self.node('ShaderNodeBsdfTransparent', 300)
        add = self.node('ShaderNodeAddShader', 600)
        self.link(tr.outputs[0], add.inputs[0])
        self.link(gl.outputs[0], add.inputs[1])
        self.link(add.outputs[0], out.inputs['Surface'])
        self._blended(mat)
        mat.use_backface_culling = False
        return mat

    def build_volume(self):
        """Volume (fog) materials: a soft additive glow of the colour layer, scaled by density."""
        mat, out = self._new_material()
        self.y = 0
        col = self.sample('diffuse')
        rgb = col['rgb'] if col is not None else self.node('ShaderNodeRGB', -300).outputs[0]
        dens = self.math('MULTIPLY', self.material_value('density', 1.0), 0.2, clamp=True)
        rgb = self.vmath('SCALE', rgb, scale=dens).outputs[0]
        rgb = self.vmath('SCALE', rgb, scale=self.setting('Emission Strength')).outputs[0]
        em = self.node('ShaderNodeEmission', 300)
        self.link(rgb, em.inputs['Color'])
        tr = self.node('ShaderNodeBsdfTransparent', 300)
        add = self.node('ShaderNodeAddShader', 600)
        self.link(tr.outputs[0], add.inputs[0])
        self.link(em.outputs[0], add.inputs[1])
        self.link(add.outputs[0], out.inputs['Surface'])
        self._blended(mat)
        mat.use_backface_culling = False
        return mat

    def build_invisible(self):
        mat, out = self._new_material()
        tr = self.node('ShaderNodeBsdfTransparent', 300)
        self.link(tr.outputs[0], out.inputs['Surface'])
        self._blended(mat)
        return mat

    # ---- layer sampling
    def uv_vector(self, layer):
        """Vector socket for an image lookup, or None for the default UV set with no transform."""
        src = layer.uv_source
        vec = None
        if src in UV_SET:
            if src not in ('UV0', 'PARTICLE'):
                vec = self.node('ShaderNodeUVMap', -1100, uv_map=UV_SET[src]).outputs['UV']
        elif src.startswith('PLANAR') or src.startswith('TRIPLANAR'):
            tc = self.node('ShaderNodeTexCoord', -1100)
            vec = tc.outputs['Object'] if ('L' in src[6:]) else self.node(
                'ShaderNodeNewGeometry', -1100).outputs['Position']
            axis = src[-1] if src[-1] in 'XYZ' and src.startswith('PLANAR') else 'Z'
            if src.startswith('PLANAR') and axis != 'Z':   # project along the axis
                order = {'X': (1, 2, 0), 'Y': (0, 2, 1)}[axis]
                sep = self.node('ShaderNodeSeparateXYZ', -1000)
                self.link(vec, sep.inputs[0])
                comb = self.node('ShaderNodeCombineXYZ', -900)
                for i, o in enumerate(order):
                    self.link(sep.outputs[o], comb.inputs[i])
                vec = comb.outputs[0]
        elif src == 'SCREEN':
            vec = self.node('ShaderNodeTexCoord', -1100).outputs['Window']

        uv_anim = any(self.is_animated(layer, p) for p in ('uv_offset', 'uv_angle', 'uv_tiling'))
        identity = (all(abs(v) < 1e-6 for v in layer.uv_offset) and all(abs(v - 1) < 1e-6 for v in layer.uv_tiling)
                    and all(abs(v) < 1e-6 for v in layer.uv_angle))
        if identity and not uv_anim:
            return vec
        if vec is None:
            vec = self.node('ShaderNodeUVMap', -1100, uv_map='uv0').outputs['UV']
        # M3 texture space has V pointing down, and tiling/rotation pivot on the texture centre (as in
        # 3ds Max, which these models come from): flip into M3 space centred on (0.5, 0.5), apply
        # tiling/rotation/offset, then move back
        flip_in = self.node('ShaderNodeMapping', -950, vector_type='POINT')
        flip_in.inputs['Location'].default_value = (-0.5, 0.5, 0)      # (u - .5, (1 - v) - .5)
        flip_in.inputs['Scale'].default_value = (1, -1, 1)
        self.link(vec, flip_in.inputs['Vector'])
        m = self.node('ShaderNodeMapping', -800, vector_type='POINT', label='M3 UV transform')
        m.inputs['Location'].default_value = (layer.uv_offset[0], layer.uv_offset[1], 0)
        m.inputs['Rotation'].default_value = tuple(layer.uv_angle)
        m.inputs['Scale'].default_value = (layer.uv_tiling[0], layer.uv_tiling[1], 1)
        self.link(flip_in.outputs[0], m.inputs['Vector'])
        lp = layer_path(layer)
        for i in range(2):
            self.targets.append((lp + '.uv_offset', i, m.inputs['Location'], i))
            self.targets.append((lp + '.uv_tiling', i, m.inputs['Scale'], i))
        for i in range(3):
            self.targets.append((lp + '.uv_angle', i, m.inputs['Rotation'], i))
        flip_out = self.node('ShaderNodeMapping', -650, vector_type='POINT')
        flip_out.inputs['Location'].default_value = (0.5, 0.5, 0)      # (x + .5, 1 - (y + .5))
        flip_out.inputs['Scale'].default_value = (1, -1, 1)
        self.link(m.outputs[0], flip_out.inputs['Vector'])
        return flip_out.outputs[0]

    def sample(self, role, non_color=False):
        """Returns {'rgb', 'alpha', 'gray'} sockets for a layer (after channel selection, invert,
        brightness and fresnel), or None if the layer is absent or its texture is missing."""
        layer = self.layers.get(role)
        if layer is None:
            return None
        self.y -= 350
        lp = layer_path(layer)
        if getattr(layer, 'color_type', 'BITMAP') == 'COLOR' or not layer_bitmap(layer):
            rgbn = self.node('ShaderNodeRGB', -500, label=f'{role} colour')
            rgbn.outputs[0].default_value = tuple(layer.color_value[:3]) + (1.0,)
            for i in range(3):
                self.targets.append((lp + '.color_value', i, rgbn.outputs[0], i))
            rgb, alpha = rgbn.outputs[0], None
            alpha_val = layer.color_value[3]
        else:
            png = self.tex_map.get(norm_tex_key(layer_bitmap(layer)))
            img = load_image(png, non_color)
            if img is None:
                self.report['missing_textures'].add(layer_bitmap(layer))
                return None
            vec = None if layer.uv_source in ENV_SOURCES else self.uv_vector(layer)
            tex = self.node('ShaderNodeTexImage', -550, image=img, label=role)
            if not (layer.uv_wrap_x and layer.uv_wrap_y):
                tex.extension = 'EXTEND'
            if layer.uv_source.startswith('TRIPLANAR'):
                tex.projection = 'BOX'
                tex.projection_blend = 0.2
            if vec is not None:
                self.link(vec, tex.inputs['Vector'])
            rgb, alpha, alpha_val = tex.outputs['Color'], tex.outputs['Alpha'], None

        # channel selection
        ch = layer.color_channels
        gray = None
        if ch in ('R', 'G', 'B'):
            sep = self.node('ShaderNodeSeparateColor', -350)
            self.link(rgb, sep.inputs[0])
            gray = sep.outputs[{'R': 0, 'G': 1, 'B': 2}[ch]]
        elif ch == 'A':
            gray = alpha if alpha is not None else self.math('ADD', alpha_val or 1.0, 0.0)
        if gray is not None:
            comb = self.node('ShaderNodeCombineColor', -300)
            for i in range(3):
                self.link(gray, comb.inputs[i])
            rgb = comb.outputs[0]
        else:
            gray = self.node('ShaderNodeRGBToBW', -300)
            self.link(rgb, gray.inputs[0])
            gray = gray.outputs[0]

        if layer.color_invert:
            inv = self.node('ShaderNodeInvert', -250)
            self.link(rgb, inv.inputs['Color'])
            rgb = inv.outputs[0]
            gray = self.math('SUBTRACT', 1.0, gray)

        # brightness x multiplier (+ add), animatable - applied to both the colour and the scalar
        for prop, default in (('color_brightness', 1.0), ('color_multiply', 1.0)):
            val = getattr(layer, prop)
            if abs(val - default) > 1e-6 or self.is_animated(layer, prop):
                v = self.vmath('SCALE', rgb, scale=val)
                rgb = v.outputs[0]
                self.targets.append((f'{lp}.{prop}', 0, v.inputs['Scale'], None))
                g = self.math('MULTIPLY', gray, val)
                gray = g
                self.targets.append((f'{lp}.{prop}', 0, g.node.inputs[1], None))
        if abs(layer.color_add) > 1e-6 or self.is_animated(layer, 'color_add'):
            addn = self.math('ADD', 0.0, layer.color_add)
            self.targets.append((f'{lp}.color_add', 0, addn.node.inputs[1], None))
            comb = self.node('ShaderNodeCombineXYZ', -200)
            for i in range(3):
                self.link(addn, comb.inputs[i])
            rgb = self.vmath('ADD', rgb, comb.outputs[0]).outputs[0]
            gray = self.math('ADD', gray, addn)
        # never negative (negative emission renders black); "clamp" also caps at 1
        rgb = self.vmath('MAXIMUM', rgb, (0.0, 0.0, 0.0)).outputs[0]
        gray = self.math('MAXIMUM', gray, 0.0)
        if layer.color_clamp:
            rgb = self.vmath('MINIMUM', rgb, (1.0, 1.0, 1.0)).outputs[0]
            gray = self.math('MINIMUM', gray, 1.0)

        # fresnel: strength from view angle
        if layer.fresnel_type != 'DISABLED':
            lw = self.node('ShaderNodeLayerWeight', -350)
            lw.inputs['Blend'].default_value = 0.5
            f = self.math('POWER', lw.outputs['Facing'], max(layer.fresnel_exponent, 0.01))
            if layer.fresnel_type == 'INVERTED':
                f = self.math('SUBTRACT', 1.0, f)
            f = self.math('MULTIPLY_ADD', f, layer.fresnel_max - layer.fresnel_min, c=layer.fresnel_min)
            rgb = self.vmath('SCALE', rgb, scale=f).outputs[0]
            gray = self.math('MULTIPLY', gray, f)
        if alpha is None:
            alpha = self.math('ADD', alpha_val if alpha_val is not None else 1.0, 0.0)
        return {'rgb': rgb, 'alpha': alpha, 'gray': gray, 'layer': layer}

    def env_lookup(self, role):
        """Environment reflection: envio texture sampled with the reflection vector."""
        layer = self.layers.get(role)
        if layer is None or not layer_bitmap(layer):
            return None
        png = self.tex_map.get(norm_tex_key(layer_bitmap(layer)))
        img = load_image(png)
        if img is None:
            self.report['missing_textures'].add(layer_bitmap(layer))
            return None
        self.y -= 350
        tc = self.node('ShaderNodeTexCoord', -900)
        env = self.node('ShaderNodeTexEnvironment', -550, image=img, label='envio (reflection)')
        is_equirect = norm_tex_key(layer_bitmap(layer)) in self.equirect
        env.projection = 'EQUIRECTANGULAR' if is_equirect else 'MIRROR_BALL'
        self.link(tc.outputs['Reflection'], env.inputs['Vector'])
        rgb = env.outputs['Color']
        b = layer.color_brightness * layer.color_multiply
        if abs(b - 1) > 1e-6:
            rgb = self.vmath('SCALE', rgb, scale=b).outputs[0]
        if layer.fresnel_type != 'DISABLED':
            lw = self.node('ShaderNodeLayerWeight', -350)
            f = self.math('POWER', lw.outputs['Facing'], max(layer.fresnel_exponent, 0.01))
            if layer.fresnel_type == 'INVERTED':
                f = self.math('SUBTRACT', 1.0, f)
            f = self.math('MULTIPLY_ADD', f, layer.fresnel_max - layer.fresnel_min, c=layer.fresnel_min)
            rgb = self.vmath('SCALE', rgb, scale=f).outputs[0]
        return rgb

    # ---- assembly
    def build(self):
        mat = bpy.data.materials.new(self.name)
        mat.use_nodes = True
        self.nt = nt = mat.node_tree
        for n in list(nt.nodes):
            nt.nodes.remove(n)
        out = self.node('ShaderNodeOutputMaterial', 900)
        m = self.m3mat

        # --- base colour
        diff = self.sample('diffuse')
        if diff is not None:
            base = diff['rgb']
            if diff['layer'].color_channels == 'ARGB':   # alpha = team-colour mask
                base = self.mix('MIX', diff['alpha'], self.team_color(), base)
        else:
            base = (0.0, 0.0, 0.0, 1.0)
        dec = self.sample('decal')
        if dec is not None:
            base = self.mix('SCREEN', dec['alpha'], base, dec['rgb'])
        if getattr(m, 'vertex_color', False):
            attr = self.node('ShaderNodeVertexColor', -400, layer_name='m3color')
            base = self.mix('MULTIPLY', 1.0, base, attr.outputs['Color'])
        if isinstance(base, tuple):
            rgbn = self.node('ShaderNodeRGB', -300)
            rgbn.outputs[0].default_value = base
            base = rgbn.outputs[0]

        # --- normal map
        normal = self.normal_socket()

        self.y = 600
        if getattr(m, 'unshaded', False):
            shader = self.node('ShaderNodeEmission', 300)
            self.link(base, shader.inputs['Color'])
            shader = shader.outputs[0]
        else:
            dif = self.node('ShaderNodeBsdfDiffuse', 300)
            self.link(base, dif.inputs['Color'])
            if normal is not None:
                self.link(normal, dif.inputs['Normal'])
            shader = dif.outputs[0]

            # --- specular highlight (SC2: Blinn-Phong, colour = specular map x HDR spec)
            self.y = 0
            spec = self.sample('specular')
            if spec is not None:
                srgb = spec['gray'] if getattr(m, 'spec_mode', 'RGB') == 'A' else spec['rgb']
                hdr = getattr(m, 'hdr_spec', 1.0)
                if abs(hdr - 1) > 1e-6:
                    srgb = self.vmath('SCALE', srgb, scale=hdr).outputs[0]
                srgb = self.vmath('SCALE', srgb, scale=self.setting('Specular Strength')).outputs[0]
                n_exp = max(getattr(m, 'specularity', 20.0) or 20.0, 1.0)
                gloss = self.sample('gloss')
                n_sock = self.math('MAXIMUM', self.math('MULTIPLY', gloss['gray'], n_exp), 1.0) if gloss else n_exp
                # SC2's highlight comes from its sun only (environment reflection is the envio layer's
                # job), so instead of a Glossy BSDF - which reflects the whole environment and blows
                # out at SC2's spec strengths - compute a Blinn-Phong highlight from the sun direction:
                #   spec = colour * max(N.H, 0)^n * [N.L > 0],   H = normalize(L + V)
                hl = self.blinn_phong(normal, n_sock)
                spec_rgb = self.vmath('SCALE', srgb, scale=hl).outputs[0]
                em = self.node('ShaderNodeEmission', 300, label='SC2 specular highlight')
                self.link(spec_rgb, em.inputs['Color'])
                add = self.node('ShaderNodeAddShader', 500)
                self.link(shader, add.inputs[0])
                self.link(em.outputs[0], add.inputs[1])
                shader = add.outputs[0]

        # --- emissive + environment reflection, added on top (as in SC2)
        self.y = -2000
        glow = []
        for role, mode_attr in (('emissive', 'blend_mode_emis1'), ('emissive2', 'blend_mode_emis2')):
            e = self.sample(role)
            if e is None:
                continue
            mode = getattr(m, mode_attr, 'ADD')
            if mode == 'TEAMEMIS':
                glow.append(self.mix('MULTIPLY', 1.0, self.team_color(), e['rgb']))
            elif mode == 'TEAMDIFF':
                glow.append(self.mix('MULTIPLY', 1.0, self.team_color(), e['rgb']))
            else:
                glow.append(e['rgb'])
        hdr_emis = getattr(m, 'hdr_emis', 1.0)
        env = self.env_lookup('envio')
        if env is not None:
            mask = self.sample('envio_mask')
            if mask is not None:
                env = self.vmath('SCALE', env, scale=mask['gray']).outputs[0]
            k = getattr(m, 'hdr_envi_const', 1.0)
            if abs(k - 1) > 1e-6:
                env = self.vmath('SCALE', env, scale=k).outputs[0]
        if glow or env is not None:
            total = None
            for g in glow:
                total = g if total is None else self.vmath('ADD', total, g).outputs[0]
            if total is not None and abs(hdr_emis - 1) > 1e-6:
                total = self.vmath('SCALE', total, scale=hdr_emis).outputs[0]
            if total is not None:
                total = self.vmath('SCALE', total, scale=self.setting('Emission Strength')).outputs[0]
            if env is not None:
                env = self.vmath('SCALE', env, scale=self.setting('Reflection Strength')).outputs[0]
                total = env if total is None else self.vmath('ADD', total, env).outputs[0]
            em = self.node('ShaderNodeEmission', 300)
            self.link(total, em.inputs['Color'])
            add = self.node('ShaderNodeAddShader', 600)
            self.link(shader, add.inputs[0])
            self.link(em.outputs[0], add.inputs[1])
            shader = add.outputs[0]

        # --- transparency / blend mode
        self.y = -2600
        alpha = None
        for role in ('alpha', 'alpha2'):
            a = self.sample(role)
            if a is not None:
                alpha = a['gray'] if alpha is None else self.math('MULTIPLY', alpha, a['gray'])
        if getattr(m, 'vertex_alpha', False):
            attr = self.node('ShaderNodeVertexColor', -400, layer_name='m3color')
            alpha = attr.outputs['Alpha'] if alpha is None else self.math('MULTIPLY', alpha, attr.outputs['Alpha'])
        blend = getattr(m, 'blend_mode', 'OPAQUE')
        if blend in ('ADD', 'ALPHAA'):
            # additive: the surface only adds light
            black = self.node('ShaderNodeEmission', 400)
            black.inputs['Color'].default_value = (0, 0, 0, 1)
            if alpha is not None:
                mixn = self.node('ShaderNodeMixShader', 600)
                self.link(alpha, mixn.inputs[0])
                self.link(black.outputs[0], mixn.inputs[1])
                self.link(shader, mixn.inputs[2])
                shader = mixn.outputs[0]
            tr = self.node('ShaderNodeBsdfTransparent', 600)
            add = self.node('ShaderNodeAddShader', 750)
            self.link(tr.outputs[0], add.inputs[0])
            self.link(shader, add.inputs[1])
            shader = add.outputs[0]
            self._blended(mat)
        elif alpha is not None:
            if blend == 'ALPHAB':
                fac = alpha
                self._blended(mat)
            else:   # opaque with alpha-test cut-out
                thr = (getattr(m, 'alpha_test_threshold', 0) or 128) / 255.0
                fac = self.math('GREATER_THAN', alpha, thr - 1e-4)
            tr = self.node('ShaderNodeBsdfTransparent', 600)
            mixn = self.node('ShaderNodeMixShader', 750)
            self.link(fac, mixn.inputs[0])
            self.link(tr.outputs[0], mixn.inputs[1])
            self.link(shader, mixn.inputs[2])
            shader = mixn.outputs[0]
        self.link(shader, out.inputs['Surface'])
        mat.use_backface_culling = not getattr(m, 'two_sided', False)
        mat['m3_blend_mode'] = blend
        return mat

    @staticmethod
    def _blended(mat):
        if hasattr(mat, 'surface_render_method'):
            mat.surface_render_method = 'BLENDED'
        elif hasattr(mat, 'blend_method'):
            mat.blend_method = 'BLEND'


def build_sc2_material(name, m3mat, layers, tex_map, equirect, report, normal_mode, animated_paths, mtype=None):
    b = SC2MaterialBuilder(name, m3mat, layers, tex_map, equirect, report, normal_mode, animated_paths)
    if mtype in DISTORTION_TYPES:
        mat = b.build_distortion()
    elif mtype in VOLUME_TYPES:
        mat = b.build_volume()
    elif mtype in INVISIBLE_TYPES:
        mat = b.build_invisible()
    else:                      # standard, terrain, creep, splat-terrain-bake ...
        mat = b.build()
    mat['m3_material_type'] = (mtype or '').replace('m3_materials_', '')
    return mat, b.targets


# --------------------------------------------------------------------------------------
# Layer animation -> node animation
# --------------------------------------------------------------------------------------

def socket_path(socket):
    return socket.path_from_id('default_value')


def build_material_actions(targets_by_mat, layer_defaults, placed, build_action, all_placed=None, out=None,
                           name='{mat}_LayerAnims'):
    """Key the node sockets that mirror animated M3 layer properties, on the model's timeline.

    targets_by_mat: {material: [(m3_path, m3_index, socket, socket_index)]}
    layer_defaults: {(m3_path, m3_index): value}
    placed:         [(sequence, frame_offset)] - sequences carry 'mat_channels'
    build_action:   blender_convert.build_action(name, defaults, placed)
    all_placed:     the sequences that decide which sockets are animated (default: placed), so that
                    actions of single sequences all key the same sockets
    out:            list that receives (material, action)
    """
    made = 0
    for mat, targets in targets_by_mat.items():
        defaults_t, keymap = {}, {}
        for m3_path, m3_idx, sock, s_idx in targets:
            key_old = (m3_path, m3_idx)
            if not any(key_old in seq['mat_channels'] for seq, _ in (all_placed or placed)):
                continue
            key_new = (socket_path(sock), s_idx if s_idx is not None else 0)
            keymap[key_new] = key_old
            dv = sock.default_value
            cur = dv[s_idx] if s_idx is not None else dv
            defaults_t[key_new] = layer_defaults.get(key_old, cur)
        if not keymap:
            continue
        placed_t = []
        for seq, off in placed:
            ch = {kn: seq['mat_channels'][ko] for kn, ko in keymap.items() if ko in seq['mat_channels']}
            placed_t.append((dict(seq, channels=ch), off))
        nt = mat.node_tree
        act = build_action(name.format(mat=mat.name), defaults_t, placed_t)
        if out is not None:
            out.append((mat, act))
        nt.animation_data_create()
        nt.animation_data.action = act
        try:
            if nt.animation_data.action_slot is None and len(act.slots):
                nt.animation_data.action_slot = act.slots[0]
        except AttributeError:
            pass
        made += 1
    return made
