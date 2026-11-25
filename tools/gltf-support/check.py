"""Import each test .glb into Blender and report whether it imports and what it produced.

    blender -b --factory-startup --python check.py -- <folder>/*.glb

Prints one line starting with RESULT, followed by JSON."""
import bpy, sys, os, json, io, contextlib
files = sys.argv[sys.argv.index('--') + 1:]

def fcurves():
    for a in bpy.data.actions:
        if getattr(a, 'layers', None) and len(a.layers):
            for l in a.layers:
                for st in l.strips:
                    for cb in st.channelbags:
                        for fc in cb.fcurves:
                            yield cb.slot.identifier, fc
        else:
            try:
                for fc in a.fcurves:
                    yield a.id_root, fc
            except AttributeError:
                pass

def evidence():
    ev = {}
    mats = [m for m in bpy.data.materials if m.node_tree and m.name not in ('Dots Stroke',)]
    def walk(nt, seen=set()):
        for n in nt.nodes:
            yield n
            if n.type == 'GROUP' and n.node_tree and n.node_tree not in seen:
                seen.add(n.node_tree); yield from walk(n.node_tree)
    allnodes = [n for m in mats for n in walk(m.node_tree)]
    ev['mapping_nodes'] = sum(1 for n in allnodes if n.type == 'MAPPING')
    ev['principled'] = sum(1 for n in allnodes if n.type == 'BSDF_PRINCIPLED')
    ev['emission_nodes'] = sum(1 for n in allnodes if n.type in ('EMISSION', 'BACKGROUND'))
    spec_linked = False; emis_strength = None
    for n in allnodes:
        if n.type == 'BSDF_PRINCIPLED':
            for name in ('Specular Tint', 'Specular'):
                if name in n.inputs and n.inputs[name].is_linked:
                    spec_linked = True
            if 'Emission Strength' in n.inputs:
                emis_strength = round(n.inputs['Emission Strength'].default_value, 3)
            if 'Specular IOR Level' in n.inputs:
                ev['spec_ior_level'] = round(n.inputs['Specular IOR Level'].default_value, 3)
    ev['spec_tex_linked'] = spec_linked
    ev['emission_strength'] = emis_strength
    fcs = list(fcurves())
    ev['anim_paths'] = sorted({f'{ident[:2]}:{fc.data_path}' for ident, fc in fcs if 'pose.bones' not in fc.data_path})
    sc = bpy.context.scene
    ev['variants'] = len(sc.gltf2_KHR_materials_variants_variants) if hasattr(sc, 'gltf2_KHR_materials_variants_variants') else None
    obs = [o for o in bpy.data.objects if o.type == 'MESH']
    ev['hidden'] = [o.name for o in obs if o.hide_render or o.hide_viewport or o.hide_get()]
    return ev

res = {'version': bpy.app.version_string}
for f in files:
    bpy.ops.wm.read_homefile(use_empty=True)
    for a in list(bpy.data.actions): bpy.data.actions.remove(a)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            r = bpy.ops.import_scene.gltf(filepath=f)
        ok = 'FINISHED' in r
        err = ''
    except Exception as e:
        ok = False
        lines = [l for l in str(e).splitlines() if l.strip()]
        err = lines[-1][:200] if lines else repr(e)
    entry = {'ok': ok, 'error': err}
    if ok:
        try:
            entry.update(evidence())
        except Exception as e:
            entry['evidence_error'] = repr(e)
    res[os.path.basename(f)[:-4]] = entry
print('RESULT ' + json.dumps(res))
