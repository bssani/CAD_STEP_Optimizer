# CLAUDE.md — project context for Claude Code

> This file is read automatically by Claude Code when the repo is opened. It exists
> so a fresh Claude Code session (e.g. on a different machine) gets the same context
> without access to any local `.claude` memory. Keep it updated.

## What this is
A pipeline that converts CATIA-exported **STEP** files to web-ready **GLB** (for
Babylon.js / VWV) and **verifies the conversion with objective numbers** — deviation
(mm), draw calls, topology. Built for the GMTCK PQDQ workflow. Ships as a zero-install
Windows `.exe`.

## Non-negotiable principles
1. **Structure first**: assembly hierarchy + part names must survive into the GLB.
   This OUTRANKS draw-call reduction when they conflict.
2. **Draw calls are the web bottleneck** (polygons aren't ignored — no Nanite on web).
3. **Measure, don't eyeball.** Every quality claim is a measured number.
4. **Measurement and processing are SEPARATE** modules.
5. **Work phase-by-phase**, verify each step; don't change many things at once.
6. **Verify external tool/API flags** (`--help`, source) — never guess them.

## Layout
```
cadpipe/
  occ_common.py   OCCT/XCAF helpers (read STEP, names, mesh, triangulate, write GLB)
  glb_common.py   read-only GLB inspection (nodes, names, draw calls, triangles)
  convert.py      Phase 1: STEP->GLB, relative/absolute chord+angular deviation,
                  per-part meshing, class-A classification HOOK, name/hierarchy log
  measure.py      Phase 2: ultra-dense reference + bidirectional Hausdorff (PyMeshLab),
                  P50/P95/max mm, topology QA, deviation colormap .ply, JSON+report
  optimize.py     Phase 3: gltf-transform dedup/weld/(simplify)/(instance)/meshopt +
                  hierarchy-safe in-part face merge; re-measure; node toolchain resolver
  merge_faces.mjs Node helper: joinPrimitives PER-MESH only (no flatten)
run.py            Phase 4: convert->measure->optimize + timestamped archival + summary
tools/            make_sample_step.py (synthetic assembly), dump_glb_tree.py
cadpipe-run.spec  PyInstaller onedir spec ; build_exe.bat one-shot build
```

## Key technical decisions (and WHY)
- **OCP-first, not Mayo.** The converter is OCCT via `cadquery-ocp` (module `OCP`),
  not Mayo CLI. Reason: Mayo's glTF writer exposes naming/coords/format but **no
  chord/angular deviation control**, which the spec and the Phase-2 reference mesh
  require. `convert_with_mayo` is a refusing stub on purpose. OCP statics use a `_s`
  suffix (e.g. `XCAFApp_Application.GetApplication_s`).
- **Per-part meshing** so the class-A hook can give different deviation per part.
  `convert.classify_part()` is a **PLACEHOLDER** returning `"standard"` — wire the
  team's real exterior/class-A naming/layer rule there; routing is already live.
- **GLB is in METRES** (OCCT scales mm->m on glTF export). Multiply by 1000 for mm
  (`measure.GLB_UNIT_TO_MM`). If a future exporter changes this, fix that constant.
- **Topology QA must WELD first.** OCCT writes one unwelded primitive per BREP face,
  so raw GLB looks full of "holes". `measure._topology_qa` runs
  `meshing_merge_close_vertices` before measuring; "closed" = PyMeshLab
  `boundary_edges==0` (trimesh watertight is unreliable at metre scale).
- **CLI `join` is BANNED** — its implicit `flatten` destroys the hierarchy even with
  `--keepNamed` (verified). In-part face merge is done by `merge_faces.mjs`
  (joinPrimitives per mesh, no flatten): 22->7 draw calls, structure 100% intact.
- **instance is OFF by default**: it collapses repeated-part nodes (loses Bolt_1..4 /
  sub-assembly names). Structure (#1) outranks draw calls (#2). It's reported as
  `lost_names` when enabled.
- **Deviation re-measured on the PRE-meshopt geometry** (trimesh can't read meshopt;
  pygltflib can still read counts). meshopt position quantization is reported as a bound.

## Run / build
```bash
# dev
python run.py samples/DemoBracket.step --web --target 0.1
python -m cadpipe.measure out.glb model.step --target 0.1
# build the zero-install .exe (onedir)
build_exe.bat            # -> dist/cadpipe-run/cadpipe-run.exe
```
The `.exe` bundles OCCT (TK*.dll x47), PyMeshLab, a portable `node.exe`, and the
`gltf-transform` CLI (incl. meshoptimizer) — **nothing to install on the target PC**.
`optimize.py` prefers the bundled `_MEIPASS/node/node.exe` + `_MEIPASS/gltf-transform/
bin/cli.js`, falling back to PATH in a dev checkout, and skips optimize gracefully if
neither is found. PyInstaller spec EXCLUDES torch/scipy/pandas (they bloated it to >1GB
and shipped duplicate DLLs that segfaulted OCP's native import).

## Status
All phases (0-4) done and `.exe` verified on the **synthetic** sample (`DemoBracket.step`):
P95 0.006mm, draw calls 22->7, names 4/4, topology clean, archive reproducible.

## Remaining before production (mostly needs human input)
- Test on a **real CATIA STEP** (everything so far is synthetic). Real CAD may have
  dirty topology, non-ASCII names, large size.
- Replace the `classify_part` placeholder with the team's class-A rule.
- Decide defaults: `--web` simplify on/off, class-A targets (e.g. exterior 0.1mm /
  interior 0.3mm), draw-call budget.
- Run on the actual company PC (antivirus/SmartScreen may block the exe).
- Manual visual QA in Babylon Sandbox (open `final.glb`, confirm tree + names).

## Honest limitations (do not oversell)
- OCCT can flip normals / drop faces on dirty CAD → QA only FLAGS, no auto-repair.
- Reference mesh is OCCT ultra-dense — an approximation of the BREP surface, not exact.
- No cross-part mesh merging (out of scope; Simplygon territory) — only flagged if draw
  calls exceed budget.
