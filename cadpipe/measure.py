"""Phase 2 — MEASUREMENT ONLY. Does not modify the production GLB or "improve"
anything; it just measures and records numbers.

What it measures, given a production GLB + the source STEP:
  * Generates an ultra-dense "reference mesh" ONCE from the STEP (deviation
    cranked to an extreme) -- the stand-in for BREP ground truth.
  * Bidirectional Hausdorff distance (prod->ref AND ref->prod), because a single
    direction misses one side of the error: prod->ref catches surface that bulged
    out; ref->prod catches features the production under-tessellated or dropped.
  * P50 / P95 / max deviation (mm), triangle counts, estimated draw calls
    (= mesh primitives, instanced), file size.
  * A deviation colormap mesh exported to .ply for visual cross-check.
  * Topology QA: non-manifold edges, suspected missing faces (open boundaries
    AFTER welding), flipped-normal suspects. QA only FLAGS -- no auto-repair.
  * Everything saved as machine JSON + a human-readable report.

Honest limitations (do not oversell the numbers):
  * The reference is OCCT ultra-dense tessellation -- a fine APPROXIMATION of the
    true BREP surface, not the exact analytic surface. Reported deviations are
    "vs reference", i.e. slightly optimistic near tight curvature.
  * GLB length unit is metres (OCCT scales mm->m on glTF export); we multiply by
    1000 to report mm. If a future exporter changes that scaling, GLB_UNIT_TO_MM
    must change with it.
  * Flipped-normal "suspects" are counted relative to trimesh's coherent
    reorientation, a heuristic; treat as a flag to inspect, not a proof.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import numpy as np
import trimesh
import pymeshlab

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from cadpipe import occ_common as occ
    from cadpipe import glb_common as glb
else:
    from . import occ_common as occ
    from . import glb_common as glb

GLB_UNIT_TO_MM = 1000.0  # OCCT exports glTF in metres; 1 unit = 1 m = 1000 mm


# ==========================================================================
# data records
# ==========================================================================
@dataclass
class HausdorffStats:
    direction: str          # "prod->ref" | "ref->prod"
    n_samples: int
    p50_mm: float
    p95_mm: float
    max_mm: float
    mean_mm: float
    rms_mm: float


@dataclass
class TopologyQA:
    weld_threshold_pct: float
    connected_components: int
    boundary_edges: int            # >0 after welding => suspected missing faces / cracks
    suspected_missing_faces: int   # number_holes after welding
    non_manifold_edges: int
    non_manifold_vertices: int
    genus: int
    is_two_manifold: bool
    is_closed: bool                # no open boundary / non-manifold edges after welding
    winding_consistent: bool       # trimesh
    flipped_normal_suspects: int   # vs trimesh coherent reorientation


@dataclass
class MeasureResult:
    glb: str
    source_step: str
    reference_glb: str
    reference_chord_mm: float
    reference_angular_deg: float
    # geometry / web-cost metrics (measured off the production GLB)
    draw_call_estimate: int
    rendered_triangles: int
    unique_mesh_primitives: int
    file_kb: float
    # deviation
    forward: HausdorffStats
    backward: HausdorffStats
    symmetric_p95_mm: float
    symmetric_max_mm: float
    target_mm: float
    p95_within_target: bool
    # topology
    topology: TopologyQA
    colormap_ply: str
    seconds: float

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ==========================================================================
# reference mesh (ultra-dense, generated once)
# ==========================================================================
def generate_reference_glb(step: Path, out: Path,
                           chord_mm: float = 0.01, angular_deg: float = 5.0) -> None:
    """Ultra-dense tessellation of the STEP -> GLB, the ground-truth stand-in.

    Goes through the SAME glTF writer as production so both meshes share the
    metres/Y-up convention and align without any extra transform.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = occ.read_step(step)
    st = occ.shape_tool(doc)
    comp = occ.free_compound(st)
    params = occ.make_mesh_params(chord_mm, np.radians(angular_deg),
                                  relative=False, in_parallel=True)
    occ.mesh_shape(comp, params)
    occ.write_glb(doc, out)


# ==========================================================================
# helpers
# ==========================================================================
def _world_mesh_to_ply(glb_path: Path, ply_path: Path) -> trimesh.Trimesh:
    """Load a GLB, bake node/world transforms into one mesh, write PLY."""
    scene = trimesh.load(str(glb_path), force="scene")
    mesh = scene.to_geometry()  # concatenated, world-space
    ply_path.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(str(ply_path))
    return mesh


def _hausdorff(ms: pymeshlab.MeshSet, sampled_id: int, target_id: int,
               direction: str, samplenum: int) -> HausdorffStats:
    before = ms.mesh_number()
    res = ms.get_hausdorff_distance(
        sampledmesh=sampled_id, targetmesh=target_id,
        savesample=True, samplevert=True, sampleface=True, samplenum=samplenum,
    )
    # the per-sample distribution lives in the new "Hausdorff Sample Point" layer
    dist_mm: Optional[np.ndarray] = None
    for i in range(before, ms.mesh_number()):
        ms.set_current_mesh(i)
        if "Sample Point" in ms.current_mesh().label():
            dist_mm = np.asarray(ms.current_mesh().vertex_scalar_array()) * GLB_UNIT_TO_MM
            break
    if dist_mm is None or dist_mm.size == 0:
        # fall back to dict-only stats (no percentiles available)
        dist_mm = np.array([res["max"] * GLB_UNIT_TO_MM])
    return HausdorffStats(
        direction=direction,
        n_samples=int(res["n_samples"]),
        p50_mm=float(np.percentile(dist_mm, 50)),
        p95_mm=float(np.percentile(dist_mm, 95)),
        max_mm=float(res["max"] * GLB_UNIT_TO_MM),
        mean_mm=float(res["mean"] * GLB_UNIT_TO_MM),
        rms_mm=float(res["RMS"] * GLB_UNIT_TO_MM),
    )


def _topology_qa(prod_ply: Path, weld_pct: float) -> TopologyQA:
    # --- PyMeshLab: weld coincident verts, THEN measure (raw GLB is unwelded) ---
    ms = pymeshlab.MeshSet()
    ms.load_new_mesh(str(prod_ply))
    ms.meshing_merge_close_vertices(threshold=pymeshlab.PercentageValue(weld_pct))
    t = ms.get_topological_measures()

    # "closed" is judged from the PyMeshLab POSITION-weld (authoritative): a solid
    # with no open boundary and no non-manifold edges is closed. (trimesh's
    # watertight check is tolerance-sensitive at metre scale and gives false
    # negatives on OCCT's per-face output, so we don't use it for closedness.)
    is_closed = (int(t["boundary_edges"]) == 0 and int(t["non_two_manifold_edges"]) == 0)

    # --- trimesh: winding consistency + flipped-normal suspects only ---
    m = trimesh.load(str(prod_ply), force="mesh")
    m.merge_vertices(merge_norm=True, merge_tex=True)
    winding_ok = bool(m.is_winding_consistent)
    orig_n = m.face_normals.copy()
    try:
        trimesh.repair.fix_normals(m)
        flips = int(np.sum(np.einsum("ij,ij->i", orig_n, m.face_normals) < 0))
    except Exception:
        flips = -1  # could not evaluate

    return TopologyQA(
        weld_threshold_pct=weld_pct,
        connected_components=int(t["connected_components_number"]),
        boundary_edges=int(t["boundary_edges"]),
        suspected_missing_faces=int(t["number_holes"]),
        non_manifold_edges=int(t["non_two_manifold_edges"]),
        non_manifold_vertices=int(t["non_two_manifold_vertices"]),
        genus=int(t["genus"]),
        is_two_manifold=bool(t["is_mesh_two_manifold"]),
        is_closed=is_closed,
        winding_consistent=winding_ok,
        flipped_normal_suspects=flips,
    )


def _export_colormap(ms: pymeshlab.MeshSet, prod_id: int, out_ply: Path,
                     max_mm: float) -> None:
    """Colorize production vertices by their (already-computed) distance scalar
    and save as .ply for visual QA."""
    ms.set_current_mesh(prod_id)
    maxval = max(max_mm, 1e-9) / GLB_UNIT_TO_MM  # scalar units are metres
    ms.compute_color_from_scalar_per_vertex(minval=0.0, maxval=maxval, colormap="RGB")
    out_ply.parent.mkdir(parents=True, exist_ok=True)
    ms.save_current_mesh(str(out_ply))


# ==========================================================================
# main measurement entry point
# ==========================================================================
def measure(prod_glb: Path, source_step: Path, out_dir: Path, *,
            target_mm: float = 0.1,
            reference_glb: Optional[Path] = None,
            ref_chord_mm: float = 0.01, ref_angular_deg: float = 5.0,
            samplenum: int = 100000, weld_pct: float = 0.001,
            verbose: bool = True) -> MeasureResult:
    t0 = time.perf_counter()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) reference mesh (generate once, or reuse a provided one for re-measures)
    ref_glb = reference_glb or (out_dir / "_reference_dense.glb")
    if not ref_glb.exists():
        generate_reference_glb(source_step, ref_glb, ref_chord_mm, ref_angular_deg)

    # 2) web-cost metrics from the production GLB
    g = glb.load(prod_glb)
    draw_calls = glb.draw_call_estimate(g)
    rtris = glb.rendered_triangles(g)
    uprims = glb.unique_mesh_primitives(g)
    file_kb = prod_glb.stat().st_size / 1024.0

    # 3) bake both to world-space PLYs
    prod_ply = out_dir / "_prod_world.ply"
    ref_ply = out_dir / "_ref_world.ply"
    _world_mesh_to_ply(prod_glb, prod_ply)
    _world_mesh_to_ply(ref_glb, ref_ply)

    # 4) bidirectional Hausdorff
    ms = pymeshlab.MeshSet()
    ms.load_new_mesh(str(prod_ply))   # id 0 = production
    ms.load_new_mesh(str(ref_ply))    # id 1 = reference
    forward = _hausdorff(ms, 0, 1, "prod->ref", samplenum)
    backward = _hausdorff(ms, 1, 0, "ref->prod", samplenum)
    sym_p95 = max(forward.p95_mm, backward.p95_mm)
    sym_max = max(forward.max_mm, backward.max_mm)

    # 5) deviation colormap (.ply) from production vertex scalars (set in forward pass)
    colormap_ply = out_dir / "deviation_colormap.ply"
    _export_colormap(ms, 0, colormap_ply, max_mm=max(sym_p95, 1e-6))

    # 6) topology QA (separate, welded)
    topo = _topology_qa(prod_ply, weld_pct)

    result = MeasureResult(
        glb=str(prod_glb), source_step=str(source_step), reference_glb=str(ref_glb),
        reference_chord_mm=ref_chord_mm, reference_angular_deg=ref_angular_deg,
        draw_call_estimate=draw_calls, rendered_triangles=rtris,
        unique_mesh_primitives=uprims, file_kb=file_kb,
        forward=forward, backward=backward,
        symmetric_p95_mm=sym_p95, symmetric_max_mm=sym_max,
        target_mm=target_mm, p95_within_target=bool(sym_p95 <= target_mm),
        topology=topo, colormap_ply=str(colormap_ply),
        seconds=time.perf_counter() - t0,
    )

    # 7) persist: JSON + human report (measurement only -- never mutate inputs)
    write_json(result, out_dir / "measurement.json")
    write_report(result, out_dir / "measurement_report.md")
    if verbose:
        _print_report(result)
    return result


# ==========================================================================
# persistence + reporting
# ==========================================================================
def write_json(r: MeasureResult, path: Path) -> None:
    path.write_text(json.dumps(r.to_dict(), indent=2), encoding="utf-8")


def write_report(r: MeasureResult, path: Path) -> None:
    t = r.topology
    verdict = "PASS" if r.p95_within_target else "OVER BUDGET"
    lines = [
        f"# Measurement report — {Path(r.glb).name}",
        "",
        f"- source STEP: `{r.source_step}`",
        f"- reference (ground-truth approx): `{Path(r.reference_glb).name}` "
        f"(chord {r.reference_chord_mm} mm, angular {r.reference_angular_deg} deg)",
        f"- measured in: {r.seconds:.2f} s",
        "",
        "## Deviation (bidirectional Hausdorff, mm)",
        "",
        "| direction | P50 | P95 | max | mean | RMS | samples |",
        "|---|---|---|---|---|---|---|",
        f"| {r.forward.direction} | {r.forward.p50_mm:.4f} | {r.forward.p95_mm:.4f} "
        f"| {r.forward.max_mm:.4f} | {r.forward.mean_mm:.4f} | {r.forward.rms_mm:.4f} "
        f"| {r.forward.n_samples} |",
        f"| {r.backward.direction} | {r.backward.p50_mm:.4f} | {r.backward.p95_mm:.4f} "
        f"| {r.backward.max_mm:.4f} | {r.backward.mean_mm:.4f} | {r.backward.rms_mm:.4f} "
        f"| {r.backward.n_samples} |",
        "",
        f"- **symmetric P95 = {r.symmetric_p95_mm:.4f} mm**, symmetric max = {r.symmetric_max_mm:.4f} mm",
        f"- target P95 <= {r.target_mm} mm  ->  **{verdict}**",
        "",
        "## Web cost",
        "",
        f"- estimated draw calls (mesh primitives, instanced): **{r.draw_call_estimate}**",
        f"- rendered triangles: {r.rendered_triangles}  (unique primitives: {r.unique_mesh_primitives})",
        f"- file size: {r.file_kb:.1f} KB",
        "",
        "## Topology QA (welded; flags only, no auto-repair)",
        "",
        f"- connected components: {t.connected_components}",
        f"- non-manifold edges: {t.non_manifold_edges}, non-manifold vertices: {t.non_manifold_vertices}",
        f"- boundary edges (after weld): {t.boundary_edges}  "
        f"(>0 => suspected missing faces / cracks)",
        f"- suspected missing faces (holes): {t.suspected_missing_faces}",
        f"- flipped-normal suspects: {t.flipped_normal_suspects}",
        f"- closed (no open edges): {t.is_closed}, winding consistent: {t.winding_consistent}, "
        f"two-manifold: {t.is_two_manifold}, genus: {t.genus}",
        "",
        f"- deviation colormap: `{Path(r.colormap_ply).name}`",
        "",
        "_Reference is OCCT ultra-dense tessellation — an approximation of the BREP",
        "surface, not exact. Topology flags are heuristics to inspect, not proofs._",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _print_report(r: MeasureResult) -> None:
    print(f"[measure] {r.glb}")
    print(f"  reference: {Path(r.reference_glb).name} "
          f"(chord {r.reference_chord_mm}mm / {r.reference_angular_deg}deg)")
    print(f"  deviation (mm):")
    for h in (r.forward, r.backward):
        print(f"    {h.direction:10}  P50={h.p50_mm:.4f}  P95={h.p95_mm:.4f}  "
              f"max={h.max_mm:.4f}  mean={h.mean_mm:.4f}  (n={h.n_samples})")
    verdict = "PASS" if r.p95_within_target else "OVER BUDGET"
    print(f"    symmetric P95={r.symmetric_p95_mm:.4f}  max={r.symmetric_max_mm:.4f}  "
          f"| target<= {r.target_mm}mm -> {verdict}")
    print(f"  web cost: draw_calls={r.draw_call_estimate}  "
          f"rendered_tris={r.rendered_triangles}  file={r.file_kb:.1f}KB")
    t = r.topology
    print(f"  topology(welded): components={t.connected_components} "
          f"nonmanifold_edges={t.non_manifold_edges} boundary_edges={t.boundary_edges} "
          f"missing_face_suspect={t.suspected_missing_faces} flipped_normals={t.flipped_normal_suspects}")
    print(f"    closed={t.is_closed} winding_ok={t.winding_consistent}")
    print(f"  colormap: {r.colormap_ply}")
    print(f"  time: {r.seconds:.2f}s")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Measure a production GLB vs a dense reference (Phase 2).")
    ap.add_argument("glb", type=Path, help="production GLB to evaluate")
    ap.add_argument("step", type=Path, help="source STEP (for the reference mesh)")
    ap.add_argument("--out", type=Path, default=Path("reports/_measure"), help="output dir")
    ap.add_argument("--target", type=float, default=0.1, help="P95 target in mm (def 0.1)")
    ap.add_argument("--ref-chord", type=float, default=0.01, help="reference chord mm (def 0.01)")
    ap.add_argument("--ref-angular", type=float, default=5.0, help="reference angular deg (def 5)")
    ap.add_argument("--reference", type=Path, default=None, help="reuse an existing reference GLB")
    ap.add_argument("--samples", type=int, default=100000, help="Hausdorff sample count (def 100000)")
    args = ap.parse_args(argv)
    measure(args.glb, args.step, args.out, target_mm=args.target,
            reference_glb=args.reference, ref_chord_mm=args.ref_chord,
            ref_angular_deg=args.ref_angular, samplenum=args.samples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
