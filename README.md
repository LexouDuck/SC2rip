# sc2rip: StarCraft II unit & building models → COLLADA + PNG

Batch-rips every unit and building model from an installed copy of StarCraft II into
**one folder per unit/building**. Each folder holds a `.dae` file per model, with skeleton, skin
weights, materials and **all animations**, an `.obj`/`.mtl` of the same model (static), and its
textures as `.png`.

```
<out>/
  Units/
    Melee/Terran/Marine/                  Marine.dae, Marine.obj, Marine.mtl,
                                          Marine_Portrait.dae, Marine_Portrait.obj, ...
                                          Marine_Diffuse.png, Marine_Normal.png, ...
                                          Marine_animations.txt
    Campaign - Wings of Liberty/Terran/<campaign-only unit>/...
    Campaign - Heart of the Swarm/...  Campaign - Legacy of the Void/...  Campaign - Nova Covert Ops/...
  Buildings/
    Melee/Protoss/Nexus/...
    Campaign - .../
  (all reports are tab-separated)
  _report.tsv      one row per model: ok / error / duplicate, animation count, missing textures
  _plan.tsv        what was selected, and from which game container
  _containers.tsv  every mod/campaign that holds unit/building models, and how it was classified
  _log.txt
  _work/           scratch files (raw .m3, texture cache, per-folder Blender logs). Safe to delete.
```

"Campaign" folders contain only models that are **not byte-identical** to a model already ripped
from the standard (melee) data, so you get the campaign-only models without duplicates.

## How it works

1. **CASC**: reads the game's storage directly with [CascLib](https://github.com/ladislav-zezula/CascLib)
   (through Python ctypes), or reads files you already extracted with CascView.
2. **Selection**: takes every `.m3`/`.m3a` under `Assets/Units` and `Assets/Buildings` of the
   melee mods (`core`, `liberty`, `swarm`, `void`, `*multi`, applying SC2's override order) and
   the campaign containers (`campaigns/*`, `novacampaign`).
3. **Textures**: finds every texture each model references, preferring the model's own
   mod/campaign. Converts DDS → PNG with Pillow, falling back to Blender's image loader.
4. **Conversion**: runs **Blender 4.5 LTS headless** (no GUI) with the
   [M3Studio](https://github.com/Solstice245/m3studio) importer, then builds proper Blender
   materials and exports COLLADA. Runs several Blender processes in parallel.

## Requirements

| What | Notes |
|---|---|
| **Python 3.9+** with **Pillow** | `python3 -m pip install pillow numpy` (numpy is optional; it's only used by `--normal-maps rgb`) |
| **Blender 4.5 LTS** | **Must be 4.5 or older**: Blender 5.0 removed COLLADA export. Download the *portable .zip* of 4.5 LTS from blender.org; it can sit next to a normal Blender 5 install without conflict. |
| **CascLib** | Bundled in `tools/casclib/` (see below) |
| **M3Studio** | Bundled in `tools/m3studio/` (the exact version this was tested with) |

Alternatively, instead of Blender itself you can use the `bpy` module from pip. It requires
**Python 3.11** exactly:

```sh
python3.11 -m venv ~/bpy45 && ~/bpy45/bin/pip install "bpy==4.5.*" pillow
# then run sc2rip with:  --bpy-python ~/bpy45/bin/python
```

### Getting CascLib

* **Linux**: `tools/casclib/libcasc.so` is included (x86-64, CascLib commit `6b889ec`, Oct 2026).
  It needs only glibc ≥ 2.16 and has the C++ runtime built in, so it runs on any mainstream distro
  from the last decade (Ubuntu/Pop!_OS 22.04 included). To build it yourself instead:
  ```sh
  git clone https://github.com/ladislav-zezula/CascLib && cd CascLib
  cmake -B build -DCMAKE_BUILD_TYPE=Release -DCASC_BUILD_SHARED_LIB=ON && cmake --build build -j
  cp build/libcasc.so.1.0.0 /path/to/sc2rip/tools/casclib/libcasc.so   # or pass --casclib
  ```
* **Windows**: `tools/casclib/CascLib.dll` is a 64-bit build of the same commit, cross-compiled
  and **not tested** on Windows. If it fails to load, build it the same way with Visual Studio +
  CMake (`build/Release/casc.dll`).
* **No CascLib at all?** Open the game in [CascView](http://www.zezula.net/en/casc/main.html),
  extract the `mods` and `campaigns` folders somewhere, and run with `--from-extracted <that folder>`
  instead of `--game`.

## Usage

Always start with a dry run. It only lists files and takes a few seconds:

```sh
GAME="$HOME/Games/battlenet/drive_c/Program Files (x86)/StarCraft II"   # folder with SC2Data and .build.info
BLENDER="$HOME/opt/blender-4.5.x-linux-x64/blender"

python3 sc2rip.py --game "$GAME" --out ~/sc2rip_out --list-only
```

Check `_containers.tsv` and `_plan.tsv` (see "Things to verify" below). Then try a handful of folders:

```sh
python3 sc2rip.py --game "$GAME" --out ~/sc2rip_out --blender "$BLENDER" \
    --filter 'Marine|Nexus' --save-blend
```

Then the whole thing:

```sh
python3 sc2rip.py --game "$GAME" --out ~/sc2rip_out --blender "$BLENDER" --jobs 6
```

For Units only or buildings only, use `--categories units` or `--categories buildings`.

**Re-running / interrupting.** You can stop with Ctrl+C and run the same command again: folders
that already finished are skipped. A folder only counts as finished if it was converted **with the
same options** (e.g. `--save-blend`, `--anim-mode`, `--normal-maps`, `--no-obj`) and by the same
version of sc2rip, and all its output files are still there. Otherwise it's redone. So adding
`--save-blend` to a re-run redoes everything to add the .blend files. `--force` redoes everything
regardless.

### Useful options

| Option | Meaning |
|---|---|
| `--categories units,buildings` | what to rip (default: both) |
| `--sources melee,campaign` | add `coop` for Co-op Commander models, `other` for everything else |
| `--containers mods/liberty.sc2mod ...` | pick exact containers (overrides `--sources`) |
| `--filter REGEX` / `--exclude REGEX` | match output folder path / skip storage paths (e.g. `--exclude _portrait`) |
| `--anim-mode timeline\|separate\|both\|none` | see below (default `timeline`) |
| `--anim-sampling N` | sample every N frames (base rate 30 fps). Use 2 to halve animated file sizes. |
| `--normal-maps raw\|rgb` | `raw` keeps SC2's DXT5nm layout (X in alpha, Y in green); `rgb` rebuilds a standard (OpenGL/Blender-convention) normal map |
| `--save-blend` | also save a ready-to-view `.blend` per model (see "Viewing in Blender") |
| `--no-obj` | don't write the `.obj`/`.mtl` files |
| `--no-dedupe` | keep campaign models that are identical to melee ones |
| `--jobs N`, `--timeout S` | parallel Blender processes, per-folder time limit |

### Animations

SC2 models store every animation ("Stand", "Walk", "Attack 01", "Death", ...) on one model, and
some animations are built from several concurrent parts (e.g. legs + upper body). sc2rip merges
each animation's parts into one complete clip, with every bone keyed.

* **`timeline` (default)**: one `.dae` per model containing **all** animations back to back on
  one timeline. The file has a standard COLLADA `<library_animation_clips>` block naming each
  clip's start/end time, and `<Model>_animations.txt` lists the same ranges in frames (30 fps) and
  seconds, with looping flags.
* **`separate`**: additionally writes `<Model>_anims/<Model>_<Animation>.dae`, one file per
  animation, each starting at frame 0. This is easiest to use in tools that ignore animation clips.
* **`both`** = timeline + separate.

**Clip ranges and looping.** In SC2's data the end of a sequence is exclusive: the key sitting
exactly on the end frame is normally a copy of the first pose, there so looping animations wrap
smoothly, and the game never shows it. sc2rip handles this as follows:

* **looping** animations (Stand, Walk, ...) stop one frame before that wrap key, so they loop
  seamlessly;
* **non-looping** animations (Death, Birth, Attack, ...) whose end key just repeats the opening pose
  have it removed and **hold their final pose**, so death animations stay dead instead of
  blending back to standing. If the end key is a genuine final pose, it's kept.

The looping flag of each clip is in `<Model>_animations.txt`.

Bone animation is written as sampled 4×4 matrices, as Blender itself does. Blender 4.5's built-in
COLLADA animation export is broken for multiple actions, and very slow on big rigs (minutes per
model), so sc2rip samples the poses itself and writes the same format. It was verified to match
Blender's own output.

## OBJ files

Each model also gets a Wavefront `.obj` + `.mtl`. OBJ can't store a skeleton or animation, so it's
the model in its **bind pose** (the pose the mesh is stored in), Y-up as is usual for OBJ, with
every part included. Materials are described under "What's in the .dae / .obj". The normal map is
only referenced with `--normal-maps rgb`, since no OBJ viewer understands SC2's packed normal maps.

## Viewing in Blender

With `--save-blend`, each `.blend` is set up to open ready to look at:

* the viewport is in **Material Preview** mode (Blender's built-in studio HDRI lighting), framed on
  the model;
* textures use paths relative to the `.blend`, so the folder can be moved or shared;
* the animation timeline is loaded with a **named marker at the start of every animation**
  (space to play; Shift+arrow keys or the marker menu to jump between them), including
  **texture animations** (see below);
* a sun lamp (named "Light") and a neutral grey world are included for **Rendered** mode (Z → Rendered).

### SC2-style materials

The `.blend` doesn't use Principled BSDF. SC2's shading predates PBR: there's no metallic or
roughness, it's diffuse lighting plus a coloured specular highlight, with a reflection map and
glow added on top. sc2rip rebuilds that from the M3 material's own parameters:

| M3 material | Blender nodes |
|---|---|
| diffuse layer (+ team colour when its channels are ARGB), decal (screen-blended by its alpha), vertex colour | Diffuse BSDF colour |
| specular map × *HDR specular*, sharpness from *specularity* (× gloss map) | Glossy BSDF, added on top. The Blinn-Phong exponent n becomes roughness (2/(n+2))^¼ |
| environment ("envio") map, looked up by the reflection vector, × envio mask × *HDR envio* | added as emission. Cubemaps are converted to a panorama |
| emissive 1 & 2 × *HDR emissive* (team-coloured for "team colour emissive" blend modes) | added as emission |
| normal map (SC2's DXT5nm packing decoded by the *SC2 Normal Decode* group) | Normal Map |
| alpha masks, blend mode (opaque cut-out / alpha blend / additive), two-sided, unshaded | transparency, backface culling, emission-only |
| per-layer UV source (UV sets 1–4, planar, tri-planar, screen), UV offset / rotation / tiling, channel selection, invert, brightness, fresnel | UV Map / Mapping / math nodes in front of each texture |

### Team colour and other settings

All materials read a few settings from the **scene**. To change them, open the **Scene
Properties** tab (cone-and-spheres icon in the Properties editor) and expand **Custom Properties**:

| property | meaning |
|---|---|
| `team_color` | team colour (colour picker). Default is red, SC2's player-1 colour |
| `specular_strength` | multiplier on specular highlights (1.0 = the model's own values) |
| `reflection_strength` | multiplier on environment reflections |
| `emission_strength` | multiplier on emissive glow |
| `distortion_visibility` | how visible distortion effects are (warp-in fields, heat haze; see below) |

Changing a value changes every material in the file at once. Material Preview updates live.

In the shader graph, each material has a node group labelled **SC2 Settings (shared)**. It
contains one *Attribute* node per setting (type *View Layer*, which falls back to the scene), and
its *Team Color* output feeds a Mix node with the diffuse texture's alpha as the factor. So the
colour itself isn't stored in the graph; it comes from the scene property. If you append a
material into another file, add the same scene properties there, or the values read as 0.

There's no metallic value to set: the "sheen" SC2 models have comes from their specular map and
reflection map, which are both reproduced.

### Effect materials

Besides standard materials, SC2 uses a few effect-only material types, rebuilt like this:

* **displacement / reflection**: screen-space refraction shimmer (e.g. warp-in fields, heat haze),
  with only a normal map and a strength mask. Rebuilt as a transparent surface with a faint
  normal-mapped sheen, scaled by the strength mask, the material's (animated) distortion factor and
  `distortion_visibility`.
* **volume**: soft additive glow.
* **lens flare / buffer**: invisible.

In the `.dae`/`.obj` these effect materials are written **fully transparent** (alpha 0 / `d 0`).

### Normal maps

The *SC2 Normal Decode (DXT5nm)* group has a **Flip Green** input, set to 1
(DirectX convention, as the older SC2 Blender add-on also assumes). If bumps look like dents, set
it to 0. With `--normal-maps rgb`, the PNGs are converted to regular OpenGL-convention (Blender)
normal maps instead.

### Texture animations

Animated layer properties (scrolling, rotating or scaling UVs, brightness/colour pulses) are keyed
on the material nodes, on the same timeline and with the same markers as the skeleton, so they play
along with each animation in the `.blend`. **They can't be carried in the `.dae` or `.obj`**: COLLADA
and OBJ have no standard way to store texture animation, and no common importer would read it.

## What's in the .dae / .obj

**Materials.** COLLADA and OBJ only understand "a colour texture, optionally with alpha", so each
material is reduced to that:

* **main texture**: the diffuse texture, or the **emissive** texture for glow/effect materials that
  have no diffuse (energy fields, beams, glows). Its UV set and UV tiling/offset/rotation are
  **baked into the exported UVs**, so decals on a second UV set or tiled beams map correctly;
* **alpha**:
  * alpha-masked (cut-out) and alpha-blended materials get their alpha mask;
  * **additive** materials (glows that add light) get alpha = brightness, so their black parts
    are see-through. OBJ/COLLADA can't do true additive blending, so glows are alpha-blended
    over what's behind them instead;
* **colour operations** the layer applies in game (brightness, multiply, add, clamp, the glow's
  HDR strength) are baked into the texture.

When anything needs baking, an extra PNG is written next to the original:
`<texture>_Additive.png`, `<texture>_Alpha.png` or `<texture>_Color.png`. The original textures are
always there unchanged too.

In the files:
* **DAE:** the texture is written as diffuse, with standard COLLADA `<transparent>` (alpha) and
  `<emission>` (glow) entries. Blender's COLLADA exporter can't write those, so sc2rip adds them.
* **OBJ:** `map_Kd`, `map_d` (alpha) and `map_Ke` (glow).

Distortion/volume/flare-only materials are written fully transparent. Normal/specular maps are
only in the `.blend` (plus the PNGs).

**Mesh visibility.** SC2 shows and hides parts of a model per animation. For example, death models
show a different debris set in each death variant. In the `.blend` this is animated object
visibility. COLLADA has no visibility channel, so in the `.dae` a hidden part is collapsed to a
point by shrinking the bones only it uses. If a hidden part shares bones with a visible one, it
stays visible rather than distorting the visible part. The OBJ (static) contains every part.

**Validity.** Every `.dae` is valid against the official COLLADA 1.4.1 schema. sc2rip fixes names,
IDs and empty sections that Blender's exporter gets wrong.

**Other limitations**
* **Not exported**: particle systems, ribbons, lights, hit-test/physics volumes, attachment points
  (they have no COLLADA equivalent). Models that are *only* effects (no mesh) are skipped and
  listed as `skipped` in the report (`--keep-empty` to export them anyway).
* **Video textures** (`.ogv`, e.g. the Terran blimp ads) can't be converted. They're listed under
  missing textures in the report.
* **Localized asset packages** (e.g. `zhcn.sc2assets`, regional variants of some models) are ignored;
  only the base models are ripped.
* **`.m3a` files** (external animation files) are attached to the model in the same folder with
  the longest matching name prefix. Unmatched ones are ignored.

## Opening the files in other programs

| | .blend | .obj | .dae |
|---|---|---|---|
| Blender 4.5 | everything | mesh, textures, alpha, glow | mesh & textures. **Alpha is ignored and bone animation is misread by Blender's COLLADA importer** |
| Blender 5.x | everything | mesh, textures, alpha, glow | no built-in COLLADA support (removed in 5.0). Third-party importer extensions vary |
| Assimp-based tools, Noesis, 3ds Max, ... | – | mesh, textures, alpha | mesh, skeleton, animation, diffuse + emission + opacity textures |

Blender's built-in COLLADA importer was unmaintained for years and removed in Blender 5.0. Its
shortcomings show up on *any* skinned, matrix-animated COLLADA file, not just these. To check a
`.dae`, use an Assimp-based viewer or Noesis; to work in Blender, use the `.blend`.

## Things to verify on the first run

This was developed without access to a StarCraft II installation. The conversion side was tested
end-to-end on synthetic `.m3` models (rigged, textured, multi-part animations), with the results
checked numerically against ground truth. The **CASC side has not been run against the real game**.
On your first `--list-only` run, please check:

1. `_containers.tsv` lists the melee mods (`mods/liberty.sc2mod`, `mods/swarm.sc2mod`,
   `mods/void.sc2mod`, ...) and the campaigns with non-zero unit/building counts. If it's empty or
   odd, look at a few lines of `_plan.tsv` and tell me what the paths look like.
2. Anything classified as `other` that you consider campaign or standard can be added with `--containers`.
3. A few shading details are educated guesses, because no public documentation covers them:
   * which way the green channel of normal maps points (*Flip Green*, see above);
   * how cube environment maps are oriented (assumed Direct3D cube faces, with the game's Z-up mapped to D3D's Y-up);
   * where UV rotation pivots (assumed the UV origin);
   * the specularity → roughness conversion;
   * how strong distortion effects should look (`distortion_visibility`);
   * the non-looping end-key rule (see "Clip ranges and looping"). If a non-looping animation
     still drifts back at its end, tell me which model and animation, so I can look at its keys.

   If one of these looks consistently wrong on real models, tell me what you see and I'll adjust it.

If something fails, the useful bits to send back are `_containers.tsv`, the first lines of
`_report.tsv`, and the matching log from `_work/jobs/` (each folder's Blender output is logged there).

## Files

```
sc2rip.py           main script (run this)
blender_convert.py  runs inside Blender: M3 import, animation merge, COLLADA/OBJ export, .blend
sc2_material.py     runs inside Blender: export materials and the SC2-style .blend materials
casc.py             CASC (CascLib) and extracted-folder readers
tools/casclib/      CascLib builds (MIT licence)
tools/m3studio/     M3Studio add-on by Solstice245, unmodified (GPL-2.0-or-later)
```
