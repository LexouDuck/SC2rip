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
  _report.csv      one row per model: ok / error / duplicate, animation count, missing textures
  _plan.csv        what was selected, and from which game container
  _containers.csv  every mod/campaign that holds unit/building models, and how it was classified
  _sc2rip.log
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

Check `_containers.csv` and `_plan.csv` (see "Things to verify" below). Then try a handful of folders:

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
| `--normal-maps raw\|rgb` | `raw` keeps SC2's DXT5nm layout (X in alpha, Y in green); `rgb` rebuilds a standard normal map |
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

Bone animation is written as sampled 4×4 matrices, as Blender itself does. Blender 4.5's built-in
COLLADA animation export is broken for multiple actions, and very slow on big rigs (minutes per
model), so sc2rip samples the poses itself and writes the same format. It was verified to match
Blender's own output.

## OBJ files

Each model also gets a Wavefront `.obj` + `.mtl`. OBJ can't store a skeleton or animation, so it's
the model in its **bind pose** (the pose the mesh is stored in), Y-up as is usual for OBJ. The
`.mtl` references the diffuse and emissive PNGs, and the normal map too when you use
`--normal-maps rgb`. With the default `raw` normal maps it's left out on purpose: no OBJ viewer
understands the SC2 packing.

## Viewing in Blender

With `--save-blend`, each `.blend` is set up to open ready to look at:

* the viewport is in **Material Preview** mode. That uses Blender's built-in studio HDRI lighting,
  which is the best way to see normal, specular and emissive maps, and needs no setup;
* the view is framed on the model;
* every texture is wired into the material, with paths relative to the `.blend`, so the folder can
  be moved or shared;
* the animation timeline is loaded with a **named marker at the start of every animation**. Press
  space to play, or jump between markers with the marker menu or Shift+arrow keys;
* there's a sun lamp and a neutral grey world, for when you switch to **Rendered** mode (Z → Rendered).
  In Material Preview, the lighting can be changed in the viewport-shading popover (the small arrow
  next to the shading buttons, top right): pick another HDRI, rotate it, or raise *World Opacity*
  to see it as a background.

**Normal maps**: SC2 stores them as "DXT5nm" (X in the alpha channel, Y in green, no blue).
The material decodes this with a small node group, *SC2 Normal Decode (DXT5nm)*, sitting between
the normal texture and the Normal Map node. Which way SC2's green channel points isn't documented,
so the group has a **Flip Green** input (0 or 1). If bumps look like dents, open the Shader Editor,
select the node group and set Flip Green to 1. If you rip with `--normal-maps rgb`, the PNG is
already a regular blue normal map, and it's plugged straight into the Normal Map node.

**Team colour**: the diffuse texture's alpha (the team-colour mask) is kept but not used by the
material, so team-coloured areas show the texture's underlying base colour.

## What's in the .dae / known limitations

* **Materials**: the DAE references the **diffuse** texture. That's all Blender's COLLADA exporter
  writes. Every other texture the model uses (normal, specular, emissive, ...) is still converted
  and put in the folder. The `.obj` references a few more (see above), and the `.blend` has them
  all wired up.
* **Team colour**: SC2 diffuse textures usually carry the team-colour mask in their **alpha
  channel**. It's preserved in the PNG. Some viewers display it as transparency.
* **Not exported**: particle systems, ribbons, lights, hit-test/physics volumes, attachment points
  (they have no COLLADA equivalent). Models that are *only* effects (no mesh) are skipped and
  listed as `skipped` in the report (`--keep-empty` to export them anyway).
* **`.m3a` files** (external animation files) are attached to the model in the same folder with
  the longest matching name prefix. Unmatched ones are ignored.
* **Checking results**: Blender's own COLLADA *importer* misreads these animations (it mangles
  matrix-animated bones), so don't judge the files by re-importing them into Blender. Use
  [Noesis](https://richwhitehouse.com/index.php?content=inc_projects.php&showproject=91), the
  Assimp viewer, or the `--save-blend` files.

## Things to verify on the first run

This was developed without access to a StarCraft II installation. The conversion side was tested
end-to-end on synthetic `.m3` models (rigged, textured, multi-part animations), with the results
checked numerically against ground truth. The **CASC side has not been run against the real game**.
On your first `--list-only` run, please check:

1. `_containers.csv` lists the melee mods (`mods/liberty.sc2mod`, `mods/swarm.sc2mod`,
   `mods/void.sc2mod`, ...) and the campaigns with non-zero unit/building counts. If it's empty or
   odd, look at a few lines of `_plan.csv` and tell me what the paths look like.
2. Anything classified as `other` that you consider campaign or standard can be added with `--containers`.

If something fails, the useful bits to send back are `_containers.csv`, the first lines of
`_report.csv`, and the matching log from `_work/jobs/` (each folder's Blender output is logged there).

## Files

```
sc2rip.py           main script (run this)
blender_convert.py  runs inside Blender: M3 import, materials, animation merge, COLLADA export
casc.py             CASC (CascLib) and extracted-folder readers
tools/casclib/      CascLib builds (MIT licence)
tools/m3studio/     M3Studio add-on by Solstice245, unmodified (GPL-2.0-or-later)
```
