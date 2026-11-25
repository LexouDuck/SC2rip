"""glTF materials for the .glb export: core PBR plus extensions for what SC2 materials have and the
core can't express. Viewers that don't know an extension simply ignore it.

* KHR_materials_specular   the specular map (highlight colour/strength) and SC2's glossiness
* KHR_texture_transform    + KHR_animation_pointer: texture animations (scrolling / rotating /
                           scaling UVs) and brightness pulses/fades, keyed in each animation
* KHR_materials_variants   team colours: the default material has the first team colour baked in,
                           every other requested colour is a variant
* KHR_materials_unlit      glow/effect materials
"""

import math
import os
import tempfile

import bpy
import numpy as np

import sc2_material as sm
import team_colors

ROUGHNESS = 0.6

def srgb_to_linear(x):
    x = np.clip(np.asarray(x, dtype=np.float32), 0.0, 1.0)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(x):
    x = np.clip(np.asarray(x, dtype=np.float32), 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * x ** (1 / 2.4) - 0.055)


def parse_team_colors(spec):
    """--team-colors value -> [(name, linear rgb array or None)] (see team_colors.parse)."""
    return [(n, None if c is None else srgb_to_linear(np.array(c, np.float32) / 255.0))
            for n, c in team_colors.parse(spec)]


# --------------------------------------------------------------------------- pixels

def _file_bytes(img):
    path = bpy.path.abspath(img.filepath) if img is not None and img.filepath else ''
    if not path or not os.path.isfile(path):
        return None
    with open(path, 'rb') as f:
        return f.read()


def png_bytes(name, px):
    """PNG file content for (h, w, 3|4) values in 0..1, stored as-is (Blender's bottom-up rows)."""
    h, w, n = px.shape
    if n == 3:
        px = np.concatenate([px, np.ones((h, w, 1), np.float32)], axis=2)
    img = bpy.data.images.new(name, w, h, alpha=(n == 4))
    img.colorspace_settings.name = 'Non-Color'      # no conversion: the values are written as given
    img.alpha_mode = 'STRAIGHT'
    img.pixels.foreach_set(np.clip(px, 0, 1).astype(np.float32).ravel())
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


def _resized(px, h, w):
    if px.shape[:2] == (h, w):
        return px
    ys = np.arange(h) * px.shape[0] // h
    xs = np.arange(w) * px.shape[1] // w
    return px[ys][:, xs]


def _layer_rgb(img, ops):
    """A layer's colour after its operations (channel select, brightness, ...), sRGB-encoded."""
    return sm._apply_ops(sm._select(sm._pixels(img), ops['channels']), ops)


def gl_normal_px(img, normal_mode):
    """glTF normal maps are plain RGB, OpenGL convention (green up). SC2's are DirectX convention,
    packed "DXT5nm" (X in alpha, Y in green) or plain RGB."""
    px = sm._pixels(img)
    if normal_mode == 'rgb':          # the PNG was already rebuilt in that format
        return None
    x = px[..., 3] if sm.normal_is_packed(img) else px[..., 0]
    x = x * 2 - 1
    y = 1 - px[..., 1] * 2            # green down -> green up
    z = np.sqrt(np.clip(1 - x * x - y * y, 0, 1))
    return np.stack([x, y, z], axis=2) * 0.5 + 0.5


# --------------------------------------------------------------------------- UV animation maths

def uv_matrix(layer):
    """3x3 matrix of a layer's UV transform in glTF texture space (V down, like M3): scale and
    rotate about the texture centre, then offset (same as sc2_material.transform_uvs)."""
    _, (ox, oy), (tx, ty), ang = sm.static_uv(layer)
    c, s = math.cos(ang), math.sin(ang)
    to_c = np.array([[1, 0, -0.5], [0, 1, -0.5], [0, 0, 1]], np.float64)
    m = np.array([[c * tx, -s * ty, 0], [s * tx, c * ty, 0], [0, 0, 1]], np.float64)
    back = np.array([[1, 0, ox + 0.5], [0, 1, oy + 0.5], [0, 0, 1]], np.float64)
    return back @ m @ to_c


def decompose_uv(x):
    """KHR_texture_transform (offset, rotation, scale) for a 3x3 affine UV matrix, plus how far it
    is from being expressible (shear)."""
    a, t = x[:2, :2], x[:2, 2]
    sx = math.hypot(a[0, 0], a[1, 0])
    r = math.atan2(a[1, 0], a[0, 0])
    sy = -a[0, 1] * math.sin(r) + a[1, 1] * math.cos(r)
    rebuilt = np.array([[math.cos(r) * sx, -math.sin(r) * sy], [math.sin(r) * sx, math.cos(r) * sy]])
    return (float(t[0]), float(t[1])), r, (sx, sy), float(np.abs(rebuilt - a).max())


# --------------------------------------------------------------------------- material building

def _stem(img):
    return os.path.splitext(img.name)[0]


class MaterialSet:
    """Builds the glTF materials (one per export material and team colour) and keeps what the
    animation export needs: which texture slots / factors follow which layer."""

    SLOT_POINTERS = {
        'base': '/pbrMetallicRoughness/baseColorTexture',
        'emissive': '/emissiveTexture',
        'normal': '/normalTexture',
        'specular': '/extensions/KHR_materials_specular/specularColorTexture',
    }

    def __init__(self, builder, normal_mode, team_colors, animated, report, texture_anims='safe'):
        self.b = builder
        self.normal_mode = normal_mode
        self.team = team_colors or []
        self.animated = animated            # {layer path: {animated property names}}
        self.report = report
        self.texture_anims = texture_anims  # 'safe' | 'all' | 'none' (see slot_animatable)
        self.tex_cache = {}
        self.index = {}                     # material name -> default glTF material index
        self.variant_mats = {}              # material name -> [(variant index, material index)]
        self.slot_anims = []                # (material index, slot, layer, rest matrix)
        self.factor_anims = []              # (material index, kind, layer, peak scale)
        self.sheared = False

    # ---- helpers
    def _tex(self, key, make, name):
        if key not in self.tex_cache:
            data = make()
            self.tex_cache[key] = self.b.texture(data, key, name) if data else None
        return self.tex_cache[key]

    def _uv_animated(self, layer):
        return layer is not None and bool(self.animated.get(sm.layer_path(layer), set())
                                          & {'uv_offset', 'uv_angle', 'uv_tiling'})

    def _color_animated(self, layer):
        return layer is not None and bool(self.animated.get(sm.layer_path(layer), set())
                                          & {'color_brightness', 'color_multiply'})

    def slot_animatable(self, slot, unlit, blended=False):
        """Blender's glTF importer aborts the whole import on some material animations (tested
        on 4.2 to 5.3, see README "glTF extension support"). 'safe' only writes what every
        version imports: texture transforms of lit base colour textures, emissive factors, and
        base colour factors of alpha-blended unlit materials. slot: 'base' / 'emissive' /
        'normal' / 'specular' (texture transform), 'base_factor' / 'emissive_factor'."""
        if self.texture_anims == 'all':
            return True
        if self.texture_anims == 'none':
            return False
        return ((slot == 'base' and not unlit) or (slot == 'base_factor' and unlit and blended)
                or slot == 'emissive_factor')

    def _texinfo(self, index, layer, slot, unlit):
        ti = {'index': index}
        if self._uv_animated(layer) and self.slot_animatable(slot, unlit):
            ti['extensions'] = {'KHR_texture_transform': {'offset': [0.0, 0.0], 'rotation': 0.0, 'scale': [1.0, 1.0]}}
            self.b.extension('KHR_texture_transform')
        return ti

    # ---- textures
    def base_texture(self, info, team):
        base = info['base_image']
        if team is None or not info.get('team_mask') or info.get('main_raw_image') is None:
            return self._tex(('color', base.name), lambda: _file_bytes(base), _stem(base))
        name, color = team

        def make():
            px = sm._pixels(base)
            mask = _resized(sm._pixels(info['main_raw_image']), *px.shape[:2])[..., 3:4]
            lin = srgb_to_linear(px[..., :3]) * mask + np.asarray(color, np.float32) * (1 - mask)
            return png_bytes(f'{_stem(base)}_{name}', np.concatenate([linear_to_srgb(lin), px[..., 3:4]], axis=2))
        return self._tex(('team', base.name, name), make, f'{_stem(base)}_{name}')

    def emissive_texture(self, info, team):
        eimg = info.get('emission_image')
        tems = info.get('team_emis') or []
        if team is None or not tems:
            if eimg is None:
                return None
            return self._tex(('color', eimg.name), lambda: _file_bytes(eimg), _stem(eimg))
        name, color = team

        def make():
            parts = [srgb_to_linear(_layer_rgb(img, ops)) * np.asarray(color, np.float32) for _, img, ops in tems]
            if eimg is not None:
                parts.append(srgb_to_linear(sm._pixels(eimg)[..., :3]))
            h = max(p.shape[0] for p in parts)
            w = max(p.shape[1] for p in parts)
            total = sum(_resized(p, h, w) for p in parts)
            return png_bytes(f'Emission_{name}', linear_to_srgb(total))
        key = ('team_emis', eimg.name if eimg else '', tuple(img.name for _, img, _ in tems), name)
        return self._tex(key, make, f'{_stem(tems[0][1])}_TeamEmission_{name}')

    def normal_texture(self, info):
        nimg = info.get('normal_image')
        if nimg is None:
            return None

        def make():
            px = gl_normal_px(nimg, self.normal_mode)
            return _file_bytes(nimg) if px is None else png_bytes(_stem(nimg) + '_gl', px)
        return self._tex(('normal', nimg.name), make, _stem(nimg) + '_GL')

    def specular_texture(self, info):
        simg, ops = info.get('spec_image'), info.get('spec_ops')
        if simg is None:
            return None
        ops = dict(ops, channels='A' if info.get('spec_mode') == 'A' else 'RGB')

        def make():
            if sm.ops_identity(ops):
                return _file_bytes(simg)
            return png_bytes(_stem(simg) + '_spec', _layer_rgb(simg, ops))
        return self._tex(('spec', simg.name, tuple(sorted(ops.items()))), make, _stem(simg))

    # ---- materials
    def entry(self, name, info, team, label=None):
        pbr = {'baseColorFactor': [0.0, 0.0, 0.0, 1.0], 'metallicFactor': 0.0, 'roughnessFactor': ROUGHNESS}
        m = {'name': f'{name} ({label})' if label else name, 'pbrMetallicRoughness': pbr}
        slots = {}
        if info.get('double_sided'):
            m['doubleSided'] = True
        if info.get('transparent'):       # distortion / volume / flare effects: not drawn
            pbr['baseColorFactor'] = [0.0, 0.0, 0.0, 0.0]
            m['alphaMode'] = 'BLEND'
            return m, slots
        if info.get('const_color') is not None:
            pbr['baseColorFactor'] = [float(c) for c in info['const_color'][:3]] + [1.0]
        main = info.get('main_layer')
        t = self.base_texture(info, team) if info.get('base_image') is not None else None
        if t is not None:
            pbr['baseColorFactor'] = [1.0, 1.0, 1.0, 1.0]
            pbr['baseColorTexture'] = self._texinfo(t, main, 'base', bool(info.get('unlit')))
            slots['base'] = main
            kind = info.get('alpha_kind')
            if kind in ('blend', 'additive'):
                m['alphaMode'] = 'BLEND'
            elif kind == 'mask':
                m['alphaMode'] = 'MASK'
                m['alphaCutoff'] = round(info.get('alpha_cutoff', 0.5), 4)
        if info.get('unlit'):
            m['extensions'] = {'KHR_materials_unlit': {}}
            return m, slots
        te = self.emissive_texture(info, team)
        if te is not None:
            el = info.get('emissive_layer') or (info['team_emis'][0][0] if info.get('team_emis') else None)
            m['emissiveTexture'] = self._texinfo(te, el, 'emissive', False)
            m['emissiveFactor'] = [1.0, 1.0, 1.0]
            slots['emissive'] = el
        tn = self.normal_texture(info)
        if tn is not None:
            m['normalTexture'] = self._texinfo(tn, info.get('normal_layer'), 'normal', False)
            slots['normal'] = info.get('normal_layer')
        if info.get('lit'):
            n = max(info.get('specularity', 20.0), 1.0)
            # SC2: Blinn-Phong highlight, exponent n, strength = spec map x hdr_spec. GGX with
            # alpha^2 = 2 / (n + 2) has the same shape; its peak is F0 (n + 2) / 8, so
            # F0 = 8 hdr spec / (n + 2) = 0.04 x specularColor
            pbr['roughnessFactor'] = round((2.0 / (n + 2.0)) ** 0.25, 4)
            ts = self.specular_texture(info)
            if ts is not None:
                f = round(min(8.0 * info.get('hdr_spec', 1.0) / (n + 2.0) / 0.04, 25.0), 4)
                spec = {'specularColorTexture': self._texinfo(ts, info.get('spec_layer'), 'specular', False),
                        'specularColorFactor': [f, f, f]}
                slots['specular'] = info.get('spec_layer')
            else:
                spec = {'specularFactor': 0.0}       # no specular layer: no highlight in SC2 either
            m.setdefault('extensions', {})['KHR_materials_specular'] = spec
        return m, slots

    def add(self, name, info):
        """Add the default material (and its team colour variants). Returns its index."""
        teams = self.team if (info.get('team_mask') or info.get('team_emis')) and self.team else [None]
        variants = []
        for vi, team in enumerate(teams):
            label = team[0] if team is not None and len(teams) > 1 else None
            if team is not None and team[1] is None:
                team = None                        # "no team colour": the plain textures
            mat, slots = self.entry(name, info, team, label)
            idx = self.b.material(mat)
            variants.append((vi, idx))
            self._record_anims(idx, info, slots)
        self.index[name] = variants[0][1]
        if len(teams) > 1:
            self.variant_mats[name] = variants
        return variants[0][1]

    def _record_anims(self, idx, info, slots):
        main = info.get('main_layer')
        if main is None:
            return
        rest = uv_matrix(main)              # the exported UVs already carry the main layer's rest transform
        unlit = bool(info.get('unlit'))
        for slot, layer in slots.items():
            if self._uv_animated(layer) and self.slot_animatable(slot, unlit) \
                    and sm.static_uv(layer)[0] == sm.static_uv(main)[0]:
                self.slot_anims.append((idx, slot, layer, np.linalg.inv(rest)))
        blended = info.get('alpha_kind') in ('blend', 'additive')
        if self._color_animated(main) and info.get('main_ops') and self.slot_animatable('base_factor', unlit, blended):
            kind = 'alpha' if info.get('alpha_kind') == 'additive' else 'rgb'
            self.factor_anims.append((idx, 'base_' + kind, main, info['main_ops']['scale']))
        el = info.get('emissive_layer')
        if 'emissive' in slots and el is not None and self._color_animated(el) and info.get('emissive_ops') \
                and self.slot_animatable('emissive_factor', unlit):
            self.factor_anims.append((idx, 'emissive', el, info['emissive_ops']['scale']))

    def finish(self):
        """Root KHR_materials_variants declaration; returns the variant mappings per material name."""
        if not self.variant_mats:
            return {}
        names = [t[0] for t in self.team]
        self.b.doc.setdefault('extensions', {})['KHR_materials_variants'] = {'variants': [{'name': n} for n in names]}
        self.b.extension('KHR_materials_variants')
        return {name: [{'material': idx, 'variants': [vi]} for vi, idx in vs] for name, vs in self.variant_mats.items()}

    # ---- animation values (called with the layers posed at a frame)
    def sample_targets(self):
        """{pointer: value list} for every animated texture transform / factor at the current frame."""
        out = {}
        for idx, slot, layer, rest_inv in self.slot_anims:
            off, rot, scl, err = decompose_uv(uv_matrix(layer) @ rest_inv)
            if err > 1e-3 and not self.sheared:
                self.sheared = True
                self.report.setdefault('warnings', []).append(
                    'glTF: a texture animation combines rotation with uneven scaling, which '
                    'KHR_texture_transform can only approximate')
            p = f'/materials/{idx}{self.SLOT_POINTERS[slot]}/extensions/KHR_texture_transform'
            out[p + '/offset'] = list(off)
            out[p + '/rotation'] = [rot]
            out[p + '/scale'] = list(scl)
        for idx, kind, layer, peak in self.factor_anims:
            k = layer.color_brightness * layer.color_multiply / peak if abs(peak) > 1e-6 else 1.0
            k = float(np.clip(k, 0.0, 1.0))
            if kind == 'base_alpha':
                out[f'/materials/{idx}/pbrMetallicRoughness/baseColorFactor'] = [1.0, 1.0, 1.0, k]
            elif kind == 'base_rgb':
                out[f'/materials/{idx}/pbrMetallicRoughness/baseColorFactor'] = [k, k, k, 1.0]
            else:
                out[f'/materials/{idx}/emissiveFactor'] = [k, k, k]
        return out
