# CAD STEP → GLB Quality Pipeline

Converts CATIA-exported **STEP** files into web-ready **GLB** (for VWV / Babylon.js)
and **verifies the conversion with objective numbers** — deviation (mm), draw calls,
and topology — instead of "it looks fine." Built for the GMTCK PQDQ workflow.

Core principles baked in:
1. **Structure first.** Assembly hierarchy + part names must survive into the GLB.
2. **Draw calls are the web bottleneck** (but polygons aren't ignored — no Nanite on web).
3. **Measure, don't eyeball.** Every quality claim is a measured number.
4. **Measurement and processing are separate** modules.

## Pipeline

```
STEP ──convert──▶ GLB ──measure──▶ numbers ──optimize──▶ smaller GLB
      (Phase 1)        (Phase 2)              (Phase 3)
                         run.py orchestrates all three + archives  (Phase 4)
```

| Module | Phase | Role |
|---|---|---|
| `cadpipe/convert.py`  | 1 | STEP→GLB; relative/absolute chord+angular deviation; per-part class-A hook; names/hierarchy preserved |
| `cadpipe/measure.py`  | 2 | bidirectional Hausdorff vs ultra-dense reference; P50/P95/max mm; draw calls; topology QA; colormap .ply |
| `cadpipe/optimize.py` | 3 | gltf-transform dedup/weld/(simplify)/(instance)/meshopt + hierarchy-safe in-part face merge; re-measure |
| `run.py`              | 4 | run 1→2→3, archive everything to a timestamped, reproducible folder + 1-page summary |

## Install

```bash
pip install -r requirements.txt
npm i -g @gltf-transform/cli      # the `gltf-transform` CLI
npm install                       # local @gltf-transform/core + functions (for merge_faces.mjs)
```

## Usage

```bash
# Full pipeline (recommended): STEP -> measured, optimized GLB, archived
python run.py path/to/model.step --target 0.1 --draw-call-budget 200

# Web-delivery preset: also reduces triangles (simplify) within the deviation target
python run.py path/to/model.step --web --target 0.1

# GPU-instance repeated parts (fewer draw calls, but loses per-instance node names)
python run.py path/to/model.step --instance

# Generate a synthetic test assembly (named hierarchy, instanced bolts, curved faces)
python tools/make_sample_step.py samples/DemoBracket.step

# Run a single phase
python -m cadpipe.convert  model.step out.glb --mode relative --chord 0.001 --angular 20
python -m cadpipe.measure  out.glb model.step --target 0.1
python -m cadpipe.optimize out.glb model.step --simplify --simplify-error 0.005
```

## Run as a standalone .exe (no Python install needed)

### Build it (from a git clone)

```bat
git clone https://github.com/bssani/CAD_STEP_Optimizer.git
cd CAD_STEP_Optimizer
build_exe.bat
```

**The BUILD machine needs** (the *target* machine that runs the exe needs nothing):
- **Python 3.13** (with pip) on PATH
- **Node.js + npm** on PATH
- **Internet** — the build downloads ~700 MB (cadquery-ocp, pymeshlab, vtk, pyinstaller,
  gltf-transform). Takes ~10–15 min.

`build_exe.bat` installs the Python deps, the gltf-transform CLI (global) + merge_faces
deps (local), then runs PyInstaller and copies the helper files. If the build PC is
locked down (no Python/Node/internet/admin), build on a machine that has them and copy
the `dist\cadpipe-run\` folder over instead.

This produces `dist\cadpipe-run\` — copy the **whole folder** and run:

```bat
cadpipe-run.exe path\to\model.step --web --target 0.1
```

or **drag a `.step` file onto `cadpipe-run.exe`**.

Fully self-contained — **zero install on the target machine**:
- convert + measure: OCP/OCCT + PyMeshLab are bundled.
- optimize: a portable `node.exe` **and** the `gltf-transform` CLI are bundled inside the
  exe too (`optimize.py` prefers the bundled copies via `sys._MEIPASS`, falling back to
  PATH in a dev checkout). So the optimize/diet phase works without installing Node.
  (If, on a dev machine, the bundled copies are absent, it falls back to PATH and skips
  gracefully when neither is found.)

> Build notes: the bundle excludes `torch`/`scipy`/`pandas` (not used; they otherwise
> bloat it to >1 GB and ship duplicate DLLs that segfault OCP's native import).
> Bundle is ~900 MB (OCCT + VTK + node + gltf-transform). Use the **onedir** layout (the
> spec default) — far more reliable than one-file for these native deps.

## Output (per run)

```
reports/run_<UTC>/
  production.glb  final.glb        # before / after optimization
  measure/  optimize/             # full per-phase artifacts (json, md, colormap .ply, stage GLBs)
  screenshots/                    # you drop Babylon Sandbox captures here (manual QA)
  manifest.json                   # params, tool versions, input SHA-256, all metrics, acceptance
  run_summary.md                  # one-page human summary + PASS/REVIEW verdict
```

## Triangle reduction

Two deviation-governed levers (both verified to stay within the measured budget):
- **At conversion** (`--chord` / `--mode absolute --chord 0.1`): coarser tessellation, fewer triangles, known deviation.
- **At optimize** (`--simplify` / `--web`): lossy reduction with an error bound; deviation is **re-measured** and checked against the target.

## Honest limitations

- **Class-A classification is a placeholder** (`convert.classify_part` returns `"standard"`); wire the team's naming/layer rule there — the per-class routing is already live.
- OCCT can flip normals / drop faces on **dirty** CAD. Topology QA **flags** these; it does **not** auto-repair.
- The reference mesh is OCCT ultra-dense tessellation — a fine **approximation** of the BREP surface, not the exact analytic surface.
- **No cross-part mesh merging** (hierarchy-destroying). Cross-part proxy/remeshing (Simplygon territory) is out of scope; if draw calls exceed budget it is **flagged for review**, not silently fixed.
- `meshopt` position quantization adds a small bounded error (reported, not folded into the measured P95).
