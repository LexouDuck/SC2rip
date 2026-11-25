# sc2rip: StarCraft II unit & building models → COLLADA + glTF + PNG

Batch-rips every unit and building model from an installed copy of StarCraft II into
**one folder per unit/building**. Each folder holds a `.dae` file per model, with skeleton, skin
weights, materials and **all animations**, a `.glb` (glTF binary) with the same plus normal maps
and mesh visibility, an `.obj`/`.mtl` of the same model (static), and its textures as `.png`.

```
<out>/
  Units/
    Melee/Terran/Marine/                  Marine.dae, Marine.glb, Marine.obj, Marine.mtl,
                                          Marine_Portrait.dae, Marine_Portrait.glb, ...
                                          Marine_Diffuse.png, Marine_Normal.png, ...
                                          Marine_animations.txt
    Campaign - Wings of Liberty/Terran/<campaign-only unit>/...
    Campaign - Heart of the Swarm/...  Campaign - Legacy of the Void/...  Campaign - Nova Covert Ops/...
    Unused/...                            models no game data uses
  Buildings/
    Melee/Protoss/Nexus/...
    Campaign - .../
  (all reports are tab-separated)
  _report.tsv      one row per model: ok / error / duplicate, animation count, missing textures,
                   terrain stand-in
  _plan.tsv        what was selected, where it is filed, where it is stored and what uses it
  _containers.tsv  every mod/campaign that holds unit/building models, and how it was classified
  _terrain_textures.tsv  the game's terrain textures (for --terrain-texture)
  _log.txt
  _work/           scratch files (raw .m3, texture cache, per-folder Blender logs). Safe to delete.
```

Models are sorted by **what uses them** in the game data (see "Sorting: melee, campaign,
unused"): "Melee" holds every model the standard (melee/multiplayer) data uses, "Campaign - ..."
only models that are used by that campaign and not by the melee data.

## Sorting: melee, campaign, unused

Since patch 4.0, nearly every model file is stored in `mods/liberty.sc2mod`, whatever uses it: the
folder a file is stored in says nothing about melee vs campaign. The game data does: every mod and
campaign lists the models it uses (`CModel` entries in its `GameData` `.xml` files, including the
data of individual campaign maps). sc2rip reads all of it and files each model as:

| Folder | Models that are... |
|---|---|
| `Melee` | used by the melee data (`core`, `liberty`, `swarm`, `void` and their `*multi` mods) |
| `Campaign - <campaign>` | not used by the melee data, but by a campaign (the earliest one if several: Wings of Liberty, then its story mode, Heart of the Swarm, ...) |
| `Co-op` | used only by Co-op Commanders data (`mods/starcoop/...`) |
| `Other - <mod>` | used only by some other mod, e.g. `Other - Warcraft III` for Blizzard's Warcraft III arcade mods (`war3`, `war3data`, `warcoop/...`) |
| `Unused` | not used by any game data: cut content, test models, or models only named in map scripts |

Models stored in a mod other than the melee ones (such as the Warcraft III asset mod) that no data
uses stay with that mod rather than going to `Unused`.

Most `CModel` entries don't spell out their file: they inherit a template path such as
`Assets\Units\##race##\##id##\##id##.m3` from a parent entry, with the entry's own attributes and
`<?token?>` defaults filling in the `##...##` tokens. sc2rip expands these. A model counts as used
when the data names its file (`path`), or names it without a variation / physics-part suffix
(`variant`: the game picks `x_00.m3`, `x_01.m3`... for a model `x.m3` with variations, and
`x_prtmdl_*.m3` are parts of `x.m3`), or has a `CModel` whose id is the file's name (`id`).
Failing all of those, a file inherits from the other files in its folder (`folder`).

A unit's folder is never split: if its main model is melee, its campaign-only portrait or variant
goes with it. `_plan.tsv` lists, for every model, where it's filed (`filed_as`), where it's stored
(`stored_in`), which mods/campaigns use it (`used_by`) and how that was found (`found_by`). Reading
the game data takes a while the first time; the result is cached in `_work/model_refs.json`.

### Death models

Death models are separate models with folders of their own, named after their unit
(`MarineDeathAcid`, `Marine_Silver_DeathRagdoll`, `BarracksFlyDeath`...). sc2rip files them in a
subfolder of their unit, without the unit's name:

    Units/Melee/Terran/Marine/                 Marine.dae, .glb ...
    Units/Melee/Terran/Marine/death/
    Units/Melee/Terran/Marine/deathacid/
    Units/Melee/Terran/Marine_Silver/deathragdoll/
    Buildings/Melee/Terran/Barracks/flydeath/

A death folder goes with its unit (Melee, Campaign...) wherever the unit goes; `found_by` says
`unit` when that's the only reason. The unit is the longest folder name of the same race that
starts the death folder's name, possibly followed by a state (`fly`, `lowered`, `sieged`,
`unpowered`, `hive`...). Death folders of no unit (`ProtossBuildingDeathEx1`, the shared building
deaths) stay where they are. `--flat-deaths` keeps every death folder at the top level.

### Portraits

The animated unit portraits (the face in the bottom of the game screen) are separate models under
`Assets/Portraits/<race>/...`. They get their own hierarchy, `Portraits/<Melee, Campaign...>/<race>/`,
named after the unit folder they belong to: `MarinePortrait` → `Portraits/Melee/Terran/Marine`,
`Marine_Silver_Portrait` → `Portraits/Melee/Terran/Marine_Silver`, `Marine1Portrait`...
`Marine11Portrait` (portrait variations) → also `Portraits/Melee/Terran/Marine`. A portrait matched to a
unit is sorted with it (Melee, Campaign...). Portraits of no unit folder (heroes, advisors,
civilians...) are named without "Portrait" (`TychusPortrait` → `Portraits/.../Terran/Tychus`) and
sorted by the game data that uses them. Their `.m3a` libraries (`TychusPortrait_RequiredAnims`) are
added to them like the units' ones. `--categories units,buildings` leaves portraits out.

### Extra animation libraries

Some folders hold only `.m3a` files: extra animations for a unit made for an expansion, a
campaign or a skin (`Marine_SwarmAnims`, `ArchonOptionalAnims`, `Marine_BlackOps_DLC2Anims`...).
sc2rip adds them to the model of the unit they animate (the longest folder of the same race with a
model whose name starts theirs), so `Marine.glb` / `.dae` / `.blend` also hold the Marine's
Heart of the Swarm animations. Where a library redefines an animation the model has, its version
is named after the library: `Stand (SwarmAnims)`. `--no-extra-anims` leaves them out.

`--sources` picks which of these to rip (default `melee,campaign,unused`; add `coop`, `other`).
`--classify location` goes back to sorting by where the files are stored. sc2rip also does that
when it finds no model references in the game data.

## How it works

1. **CASC**: reads the game's storage directly with [CascLib](https://github.com/ladislav-zezula/CascLib)
   (through Python ctypes), or reads files you already extracted with CascView.
2. **Selection**: takes every `.m3`/`.m3a` under `Assets/Units`, `Assets/Buildings` and `Assets/Portraits`
   (applying SC2's override order between the melee mods), and sorts them by the game data that
   uses them (see "Sorting").
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
    --filter 'Marine|Nexus'
```

Then the whole thing:

```sh
python3 sc2rip.py --game "$GAME" --out ~/sc2rip_out --blender "$BLENDER" --jobs 6
```

For Units only or buildings only, use `--categories units` or `--categories buildings`.

**Re-running / interrupting.** You can stop with Ctrl+C and run the same command again: folders
that already finished are skipped. A folder only counts as finished if it was converted **with the
same options** (e.g. `--no-blend`, `--anim-mode`, `--normal-maps`, `--no-obj`) and by the same
version of sc2rip, and all its output files are still there. Otherwise it's redone. So removing
`--no-blend` from a re-run redoes everything to add the .blend files. `--force` redoes everything
regardless.

### Useful options

| Option | Meaning |
|---|---|
| `--categories units,buildings,portraits` | what to rip (default: all three) |
| `--sources melee,campaign,unused` | which models to rip, by what uses them: add `coop` for Co-op Commander models, `other` for everything else (see "Sorting") |
| `--classify data\|location` | sort by what uses each model (default) or by where it's stored |
| `--containers mods/liberty.sc2mod ...` | pick exact containers (overrides `--sources`) |
| `--filter REGEX` / `--exclude REGEX` | match output folder path / skip storage paths (e.g. `--exclude _portrait`) |
| `--anim-mode timeline\|separate\|both\|none` | see below (default `timeline`) |
| `--anim-sampling N` | sample every N frames (base rate 30 fps). Use 2 to halve animated file sizes. |
| `--normal-maps raw\|rgb` | `raw` keeps SC2's DXT5nm layout (X in alpha, Y in green); `rgb` rebuilds a standard (OpenGL/Blender-convention) normal map |
| `--no-dae` | don't write the `.dae` files |
| `--no-obj` | don't write the `.obj`/`.mtl` files |
| `--no-glb` | don't write the `.glb` files |
| `--no-blend` | don't save the ready-to-view `.blend` per model (see "Viewing in Blender") |
| | with all four `--no-…` options: **textures only** (the PNGs of every planned folder, no models; Blender isn't needed) |
| `--glb-light` | put the `.blend`'s sun lamp in the `.glb` files too (see "glTF (.glb) files") |
| `--team-colors LIST` | `.glb` team colour(s): the first is baked in, the rest become material variants (default `none`: plain textures; see "glTF extensions") |
| `--glb-texture-anims safe\|all\|none` | which texture animations go in the `.glb` (see "glTF extensions") |
| `--no-dedupe` | keep campaign models that are identical to melee ones |
| `--terrain-texture auto\|ID\|PATH\|none` | stand-in for the map terrain on terrain materials (see "Terrain materials") |
| `--terrain-tile N` | game units covered by one repeat of that texture (default 4) |
| `--find REGEX` | only list the game files whose path matches, e.g. `--find mothershipcore` (see below), then exit |
| `--jobs N`, `--timeout S` | parallel Blender processes, per-folder time limit |

**Looking for a model.** If a model you expected isn't in the output, `--find` shows where (and
whether) its files are in the game storage. `--out` is optional here; with it, the list is also
written to `<out>/_find.tsv`:

```sh
python3 sc2rip.py --game "$SC2" --find mothershipcore
```

Each line shows the group its container is classified as (`melee`, `campaign`, `coop`, `other`) and
notes why a model file would be skipped (outside `Assets/Units` / `Assets/Buildings` / `Assets/Portraits`, or in a
localized package). To see what uses a model, look it up in `_plan.tsv` (`used_by`).

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

## glTF (.glb) files

Each model also gets a `.glb`: a single self-contained glTF 2.0 file (textures embedded), the
format most current tools and web viewers read best. It's written by sc2rip directly from the
converted scene (not by Blender's glTF exporter), and every file passes the official Khronos glTF
validator. It holds:

* the **mesh, skeleton and skin weights**, Y-up;
* **one glTF animation per SC2 animation** ("Stand", "Walk", "Attack 01", "Death 02", ...), named
  as in game, sampled per frame (`--anim-sampling` applies). The node transforms (what you see
  without playing anything) are the model's default pose.
  **In Blender**, the glTF importer makes each one an action, but only the first is active: the
  others are parked on muted NLA tracks, and the scene keeps its default 1-250 frame range. To
  switch, pick an action in the Dope Sheet's Action Editor, or turn on Preferences → Add-ons →
  glTF 2.0 → "Animation UI" for a "glTF" tab in the Dope Sheet sidebar that switches everything
  (bones, morph weights, materials) at once. Then set the frame range to the animation: in the
  Dope Sheet, select all keys (A) and View → Set Preview Range to Selected (Ctrl+Alt+P). In the
  `.blend`, picking one action switches everything (see "Viewing in Blender").
  **Lighting:** the `.glb` has no lamp of its own (unless `--glb-light`). In Blender, the lamp you
  see is the one from Blender's default startup scene (a point light 7 m away, whatever the
  model's size), which leaves most models dark in Rendered view; Material Preview uses its own
  studio lighting. `--glb-light` adds the `.blend`'s sun lamp (`KHR_lights_punctual`, written so
  that Blender imports it at the `.blend`'s strength). Other viewers read glTF light intensities in
  lux and may show it far too bright, or ignore it, hence off by default.
  Blender's importer also shows the bones as spheres drawn in front of the mesh: that's its
  default bone display; tick "Disable Bone Shape" (Bones section of the import options) for
  regular bones, and turn off Object Properties → Viewport Display → In Front;
* **mesh visibility**: parts that SC2 shows or hides per animation (death debris variants, the
  Stargate's work/unpowered effects) get a morph target called `hidden` (weight 1 = hidden),
  switched on and off by each animation. Unlike the `.dae`'s bone trick, this works for any mesh.
  A part that is hidden at rest has weight 1 by default;
* **materials** built from the same textures as the `.dae`/`.obj` (see "What's in the .dae / .obj"),
  plus what glTF can carry that they can't:
  * the **normal map**, converted to the standard glTF (OpenGL) format, with tangents;
  * the **glow** (emissive) texture;
  * glow/effect materials as **unlit**, so they show at full brightness whatever the lighting;
  * alpha-tested cut-outs as alpha **mask** (with SC2's threshold), blended ones as alpha **blend**;
  * **two-sided** materials;
  * no metalness; roughness from SC2's glossiness.

### glTF extensions

What core glTF can't express is stored with standard Khronos extensions. They're all optional:
a viewer that doesn't know one ignores it and shows the rest.

| Extension | What it holds | Without support |
|---|---|---|
| `KHR_materials_specular` | the **specular map** (colour and strength of the highlight), with SC2's glossiness as roughness. Materials without a specular layer get no highlight, as in game | default plastic-like highlight |
| `KHR_materials_variants` | **team colours**: see below | only the default team colour |
| `KHR_texture_transform` + `KHR_animation_pointer` | **texture animations**: scrolling/rotating/scaling UVs, and brightness pulses/fades, keyed in each animation like the bones | textures don't move |
| `KHR_materials_unlit` | glow/effect materials | lit like other surfaces |

**Team colour.** The team-coloured parts of the diffuse texture (and team-coloured glows) are baked
with the first colour of `--team-colors` (by default none: the plain texture, as in the `.dae`/`.obj`).
Every further colour is stored as a
**material variant**: the textures are baked once per colour, and viewers that support variants
can switch between them: Blender (turn on **Material Variants** in Edit › Preferences › Add-ons ›
glTF 2.0, then use the **glTF Variants** tab of the 3D Viewport sidebar), `<model-viewer>`,
Babylon.js.

```sh
--team-colors none                 # default: plain textures, no team colour
--team-colors red                  # red only
--team-colors red,blue,none        # red, plus variants Blue and "No Team Colour" (plain textures)
--team-colors all                  # all 15 SC2 player colours
--team-colors "#ff8800,light grey" # any colour as #rrggbb
```

Each extra variant adds a copy of the model's team-coloured textures to the file (the Stargate:
4 MB with `red`, 7.7 MB with `red,blue,none`, 33 MB with `all`).

**Texture animations and Blender.** Blender's glTF importer reads these, but has bugs that make
the whole import fail on some of them (see the table below). So by default
(`--glb-texture-anims safe`), the `.glb` only animates what every Blender version from 4.2 to 5.3
imports: scrolling/rotating diffuse textures of lit materials, the brightness of emission, and the
brightness of alpha-blended glows. Most SC2 texture animations are on glows, so those stay still.
`--glb-texture-anims all` writes all of them, for other viewers (Babylon.js, Godot, the Khronos
glTF Sample Viewer), but **those files won't import into Blender before 5.2**, and not into 5.3
either when a glow's texture moves. `none` writes none.

### glTF extension support in Blender and other programs

**Blender** (built-in glTF importer), tested with small test files, one per feature
(`tools/gltf-support/`, re-run them on any Blender with
`blender -b --factory-startup --python tools/gltf-support/check.py -- <files>`). 4.2.23, 4.3.0,
4.4.0, 4.5.17, 5.0.1 and 5.1.2 all behave the same.

| Feature | 4.2 – 5.1 | 5.2.5 | 5.3.0 |
|---|---|---|---|
| `KHR_materials_specular`, `_unlit`, `_emissive_strength`, `_variants` | ✅ | ✅ | ✅ |
| `KHR_texture_transform` (static, on any texture) | ✅ | ✅ | ✅ |
| morph-target weight animation (the `hidden` visibility) | ✅ | ✅ | ✅ |
| `KHR_node_visibility` | ignored | ignored | ignored |
| **Animated** (`KHR_animation_pointer`): | | | |
| texture transform, lit base colour | ✅ | ✅ | ✅ |
| texture transform, unlit base colour (glows) | ✅ | ✅ | ❌ import fails |
| texture transform, emission / normal / specular texture | ❌ import fails | ✅ | ✅ |
| base colour factor, lit + textured | ❌ import fails | ✅ | ✅ |
| base colour factor, lit, no texture | ✅ | ✅ | ✅ |
| base colour factor, unlit + opaque | ❌ import fails | ❌ import fails | ✅ |
| base colour factor, unlit + alpha blend | ✅ | ✅ | ✅ |
| emissive factor, roughness factor | ✅ | ✅ | ✅ |
| node visibility | ignored | ignored | ignored |

"Import fails" means the whole file doesn't import (Python error in the importer), not just that
one animation.

**Other programs** (from their source code or documentation, not tested here):

| | specular | variants | texture transform | animation pointer | unlit | node visibility |
|---|---|---|---|---|---|---|
| three.js r187 (`GLTFLoader`) | ✅ | plugin | ✅ | plugin (channels skipped otherwise) | ✅ | – |
| Babylon.js 9 (sandbox) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Godot 4.4+ | – | – | ✅ | ✅ | ✅ | ✅ (recent versions) |
| Unity glTFast 6.8 | – | ✅ | ✅ | – | ✅ | – |
| Assimp (and tools built on it) | ✅ | – | ✅ | – | ✅ | – |

"–" means the extension is ignored and the rest of the file loads. Unreal's documentation only
lists the extensions its glTF *exporter* writes; 3ds Max has an official open-source
importer/exporter from Khronos (2026) that covers PBR extensions, UV transforms and animation.

What the `.glb` can't carry: SC2's environment reflections, true additive blending (glows are
alpha-blended, with the alpha set so that they look right over dark backgrounds), and texture
animations of glows (see above). For the full SC2 look, use the `.blend`.

## Viewing in Blender

Each `.blend` (unless `--no-blend`) is set up to open ready to look at:

* the viewport is in **Material Preview** mode (Blender's built-in studio HDRI lighting), framed on
  the model;
* textures use paths relative to the `.blend`, so the folder can be moved or shared;
* **every animation is its own action** of the armature ("Stand", "Walk", "Attack 01"...), with
  its frame range and looping flag set, plus **"All Animations"**: every animation end to end, as
  on the `.dae`'s timeline. The file opens on "All Animations", with the frame range covering it
  and a named marker at the start of each animation (space to play; Shift+arrow keys or the
  marker menu to jump between them);
* to play one animation: with the armature selected (it is, when the file opens), pick its action
  in the Dope Sheet's **Action Editor**. Everything the animation changes follows: the bones, the
  **texture animations** (see below) and which meshes are shown (those are keyed on custom
  properties of the armature, "mat: ..." and "vis: ...", which drivers pass on to the materials and
  meshes; no script involved). To loop it, set the frame range to it: select all keys (A), then
  View → Set Preview Range to Selected (Ctrl+Alt+P);
* a sun lamp (named "Light") and a neutral grey world are included for **Rendered** mode (Z → Rendered).

### SC2-style materials

The `.blend` doesn't use Principled BSDF. SC2's shading predates PBR: there's no metallic or
roughness, it's diffuse lighting plus a coloured specular highlight, with a reflection map and
glow added on top. sc2rip rebuilds that from the M3 material's own parameters:

| M3 material | Blender nodes |
|---|---|
| diffuse layer (+ team colour when its channels are ARGB), decal (screen-blended by its alpha), vertex colour | Diffuse BSDF colour |
| specular map × *HDR specular*, sharpness from *specularity* (× gloss map) | Blinn-Phong highlight from the sun, added as emission (see below) |
| environment ("envio") map, looked up by the reflection vector, × envio mask × *HDR envio* | added as emission. Cubemaps are converted to a panorama |
| emissive 1 & 2 × *HDR emissive* (team-coloured for "team colour emissive" blend modes) | added as emission |
| normal map, packed "DXT5nm" or plain RGB, decoded by the *SC2 Normal Decode* group | Normal Map |
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
| `sun_direction` | direction of the light that makes the specular highlights. Driven by the **Light** object: rotate the lamp and the highlights follow |

Changing a value changes every material in the file at once. Material Preview updates live.

In the shader graph, each material has a node group labelled **SC2 Settings (shared)**. It
contains one *Attribute* node per setting (type *View Layer*, which falls back to the scene), and
its *Team Color* output feeds a Mix node with the diffuse texture's alpha as the factor. So the
colour itself isn't stored in the graph; it comes from the scene property. If you append a
material into another file, add the same scene properties there, or the values read as 0.

There's no metallic value to set: the "sheen" SC2 models have comes from their specular map and
reflection map, which are both reproduced.

**Specular highlights** follow SC2's model: a Blinn-Phong highlight from the scene's sun only.
Reflections of the surroundings come from the separate environment ("envio") layer, if the material
has one. A Glossy BSDF can't be used for this: it reflects the whole environment, and at SC2's spec
strengths (often several times full brightness, e.g. the Pylon crystal) it turns surfaces white.
So the highlight is computed in the material from `sun_direction`, which follows the "Light" lamp.
In Material Preview the HDRI lights the diffuse part, and the highlights still come from that lamp.

### Effect materials

Besides standard materials, SC2 uses a few effect-only material types, rebuilt like this:

* **displacement / reflection**: screen-space refraction shimmer (e.g. warp-in fields, heat haze),
  with only a normal map and a strength mask. Rebuilt as a transparent surface with a faint
  normal-mapped sheen, scaled by the strength mask, the material's (animated) distortion factor and
  `distortion_visibility`.
* **volume**: soft additive glow.
* **lens flare / buffer**: invisible.

In the `.dae`/`.obj` these effect materials are written **fully transparent** (alpha 0 / `d 0`).

### Terrain materials

Some models, like the mineral fields, have a patch of ground around them that uses SC2's
**terrain** material. It has no texture of its own: in game it shows whatever terrain the map has
under the model, so it blends in. To keep it from rendering black, sc2rip maps a stand-in terrain
texture onto it, projected from above like the game does. One repeat of the texture covers
`--terrain-tile` game units (default 4).

* `--terrain-texture auto` (default) picks a plain dirt texture from the game's terrain texture
  list. The choice is written in `_log.txt`, and the full list in `_terrain_textures.tsv`.
* `--terrain-texture <id>` uses another one from `_terrain_textures.tsv` (e.g. a tileset that
  matches what you're showing).
* `--terrain-texture none` uses a plain earth colour instead.

The texture's alpha channel is dropped, because terrain textures use it as a lighting mask rather
than transparency. The `terrain` column of `_report.tsv` shows which stand-in a model got.
Changing these options only redoes the models that have terrain materials.

### Normal maps

SC2 uses two normal-map formats: packed "DXT5nm" (X in the alpha channel, Y in green, no blue)
and plain RGB normal maps. sc2rip detects which one each texture is. The *SC2 Normal Decode* group's
**Packed (DXT5nm)** input is set to 1 or 0 accordingly; if a model's bumps look completely wrong,
that's the value to check. The group also has a **Flip Green** input, set to 1
(DirectX convention, as the older SC2 Blender add-on also assumes). If bumps look like dents, set
it to 0. With `--normal-maps rgb`, the PNGs are converted to regular OpenGL-convention (Blender)
normal maps instead.

### Texture animations

Animated layer properties (scrolling, rotating or scaling UVs, brightness/colour pulses) are keyed
on the material nodes, on the same timeline and with the same markers as the skeleton, so they play
along with each animation in the `.blend`. **They're not in the `.dae` or `.obj`**: COLLADA and OBJ
have no standard way to store texture animation. The `.glb` has part of them (see "glTF
extensions").

## What's in the .dae / .obj

**Materials.** COLLADA and OBJ only understand "a colour texture, optionally with alpha", so each
material is reduced to that:

* **main texture**: the diffuse texture, or the **emissive** texture for glow/effect materials that
  have no diffuse (energy fields, beams, glows). Its UV set and UV tiling/offset/rotation are
  **baked into the exported UVs**, so decals on a second UV set or tiled beams map correctly;
* **alpha**:
  * alpha-masked (cut-out) and alpha-blended materials get their alpha mask;
  * **additive** materials (glows that add light) get alpha = brightness, so their black parts
    are see-through. OBJ/COLLADA/glTF can't do true additive blending, so glows are alpha-blended
    over what's behind them instead. The colour is brightened to match, so that over a black
    background a glow looks exactly as in game;
* **colour operations** the layer applies in game (brightness, multiply, add, clamp, the glow's
  HDR strength) are baked into the texture. Animated brightness (effects that fade in, or are off
  at rest like the Stargate's halo) is baked at the **strongest value it reaches**;
* **glow**: when a material has a diffuse texture *and* a separate emissive layer on the same UVs
  (e.g. the Pylon crystal), the emissive layer is exported as a glow texture.

When anything needs baking, an extra PNG is written next to the original:
`<texture>_Additive.png`, `<texture>_Alpha.png`, `<texture>_Color.png` or `<texture>_Emission.png`. The original textures are
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

| | .blend | .glb | .obj | .dae |
|---|---|---|---|---|
| Blender 4.5 / 5.x | everything | everything in the .glb (File › Import › glTF). Each animation becomes an action; mesh visibility is the `hidden` shape key | mesh, textures, alpha, glow | 4.5: mesh & textures. **Alpha is ignored and bone animation is misread by Blender's COLLADA importer**. 5.x: no built-in COLLADA support |
| glTF viewers and engines (three.js-based web viewers such as gltf-viewer.donmccurdy.com, the Babylon.js sandbox, Godot, ...) | – | everything in the .glb | – | – |
| Assimp-based tools, Noesis, 3ds Max, ... | – | mesh, skeleton, animation, textures (morph-target support varies) | mesh, textures, alpha | mesh, skeleton, animation, diffuse + emission + opacity textures |

Blender's built-in COLLADA importer was unmaintained for years and removed in Blender 5.0. Its
shortcomings show up on *any* skinned, matrix-animated COLLADA file, not just these. To check a
`.dae`, use an Assimp-based viewer or Noesis; to work in Blender, use the `.blend` or the `.glb`.

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
glb_export.py       runs inside Blender: the .glb (meshes, skin, animations, visibility)
glb_materials.py    runs inside Blender: .glb materials, team colours, texture animations
gltf_writer.py      plain-Python glTF 2.0 binary writer used by glb_export.py
team_colors.py      SC2 player colours and the --team-colors option
tools/gltf-support/ test files + checker for glTF importer support (see "glTF extension support")
casc.py             CASC (CascLib) and extracted-folder readers
tools/casclib/      CascLib builds (MIT licence)
tools/m3studio/     M3Studio add-on by Solstice245, unmodified (GPL-2.0-or-later)
```
