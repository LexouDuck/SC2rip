"""Minimal glTF 2.0 binary (.glb) builder: buffers, accessors, images, materials, meshes, skins,
nodes and animations, written into a single self-contained .glb file. No Blender dependency."""

import json
import struct

import numpy as np

FLOAT, UBYTE, USHORT, UINT = 5126, 5121, 5123, 5125
ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER = 34962, 34963
_NCOMP = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT4': 16}
_DTYPE = {FLOAT: np.float32, UBYTE: np.uint8, USHORT: np.uint16, UINT: np.uint32}


class GLTFBuilder:
    def __init__(self, generator='sc2rip'):
        self.doc = {'asset': {'version': '2.0', 'generator': generator}, 'scene': 0,
                    'scenes': [{'nodes': []}], 'nodes': [], 'meshes': [], 'materials': [],
                    'textures': [], 'images': [], 'samplers': [], 'skins': [], 'animations': [],
                    'accessors': [], 'bufferViews': [], 'buffers': []}
        self.bin = bytearray()
        self._used_ext = set()
        self._images = {}       # key -> image index
        self._textures = {}     # image index -> texture index

    # ---------------------------------------------------------------- low level
    def extension(self, name):
        self._used_ext.add(name)

    def buffer_view(self, data, target=None):
        while len(self.bin) % 4:
            self.bin.append(0)
        view = {'buffer': 0, 'byteOffset': len(self.bin), 'byteLength': len(data)}
        if target is not None:
            view['target'] = target
        self.bin += data
        self.doc['bufferViews'].append(view)
        return len(self.doc['bufferViews']) - 1

    def accessor(self, array, type_, component=FLOAT, target=None, minmax=False, normalized=False):
        arr = np.ascontiguousarray(array, dtype=_DTYPE[component])
        n = _NCOMP[type_]
        count = arr.size // n
        acc = {'bufferView': self.buffer_view(arr.tobytes(), target), 'componentType': component,
               'count': int(count), 'type': type_}
        if normalized:
            acc['normalized'] = True
        if minmax:
            flat = arr.reshape(count, n)
            if component == FLOAT:
                acc['min'] = [float(v) for v in flat.min(axis=0)]
                acc['max'] = [float(v) for v in flat.max(axis=0)]
            else:
                acc['min'] = [int(v) for v in flat.min(axis=0)]
                acc['max'] = [int(v) for v in flat.max(axis=0)]
        self.doc['accessors'].append(acc)
        return len(self.doc['accessors']) - 1

    # ---------------------------------------------------------------- textures / materials
    def texture(self, png_bytes, key, name=None):
        """Texture index for a PNG (embedded once per key)."""
        img = self._images.get(key)
        if img is None:
            entry = {'bufferView': self.buffer_view(png_bytes), 'mimeType': 'image/png'}
            if name:
                entry['name'] = name
            self.doc['images'].append(entry)
            img = self._images[key] = len(self.doc['images']) - 1
        tex = self._textures.get(img)
        if tex is None:
            if not self.doc['samplers']:
                self.doc['samplers'].append({'magFilter': 9729, 'minFilter': 9987, 'wrapS': 10497, 'wrapT': 10497})
            self.doc['textures'].append({'sampler': 0, 'source': img})
            tex = self._textures[img] = len(self.doc['textures']) - 1
        return tex

    def material(self, mat):
        self.doc['materials'].append(mat)
        for ext in mat.get('extensions', {}):
            self.extension(ext)
        return len(self.doc['materials']) - 1

    # ---------------------------------------------------------------- scene graph
    def node(self, **kw):
        self.doc['nodes'].append({k: v for k, v in kw.items() if v is not None})
        return len(self.doc['nodes']) - 1

    def mesh(self, m):
        self.doc['meshes'].append(m)
        return len(self.doc['meshes']) - 1

    def skin(self, s):
        self.doc['skins'].append(s)
        return len(self.doc['skins']) - 1

    def animation(self, a):
        self.doc['animations'].append(a)
        return len(self.doc['animations']) - 1

    # ---------------------------------------------------------------- output
    def write(self, path):
        doc = {k: v for k, v in self.doc.items() if v != [] or k in ('scenes', 'nodes')}
        doc['buffers'] = [{'byteLength': len(self.bin)}]
        doc['bufferViews'] = self.doc['bufferViews']
        if self._used_ext:
            doc['extensionsUsed'] = sorted(self._used_ext)
        js = json.dumps(doc, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        js += b' ' * (-len(js) % 4)
        binary = bytes(self.bin) + b'\0' * (-len(self.bin) % 4)
        total = 12 + 8 + len(js) + 8 + len(binary)
        with open(path, 'wb') as f:
            f.write(struct.pack('<4sII', b'glTF', 2, total))
            f.write(struct.pack('<I4s', len(js), b'JSON'))
            f.write(js)
            f.write(struct.pack('<I4s', len(binary), b'BIN\0'))
            f.write(binary)
        return total
