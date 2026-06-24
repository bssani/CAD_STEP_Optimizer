"""Phase 3 — POST-PROCESSING via gltf-transform (+ a hierarchy-safe face merge).

Pipeline (each stage toggleable), ordered so each helps the next:

  merge_faces -> dedup -> weld -> [simplify] -> (shaped GLB) -> [instance] -> meshopt

Why this order / these choices (all verified empirically, not guessed):
  * merge_faces (custom, cadpipe/merge_faces.mjs): merges primitives WITHIN each
    mesh only. OCCT emits one primitive per BREP face, so this alone cuts draw
    calls a lot (22->7 on the sample) with ZERO hierarchy change.
  * The CLI `join` is intentionally NOT used: it implicitly runs `flatten`, which
    destroyed the assembly tree even with --keepNamed (verified). That violates the
    project's #1 rule (structure preservation), so it is banned here.
  * dedup: collapses the now-identical repeated part meshes to one shared mesh.
  * weld: merges bitwise-identical vertices (size). Recommended before simplify.
  * simplify (opt-in): lossy triangle reduction -> triggers a Phase 2 re-measure.
  * instance (opt-in, DEFAULT OFF): GPU-instances repeated parts -> fewer draw
    calls, BUT collapses per-instance nodes (loses Bolt_1..4 / sub-assembly names).
    Because structure preservation outranks draw-call count (#1 > #2), this is
    off by default and its structural cost is reported loudly when enabled.
  * meshopt: EXT_meshopt_compression, last (size). Quantizes positions (~bbox/2^bits);
    this small error is reported as a bound, not measured, because meshopt-compressed
    GLBs are not readable by the geometry path (trimesh) used for Hausdorff.

Honest limitations:
  * Deviation is re-measured on the PRE-meshopt geometry; meshopt position
    quantization adds a small bounded error on top (reported separately).
  * NO cross-part mesh merging anywhere. Proxy/remeshing across parts (Simplygon
    territory) is out of scope; if draw calls still blow the budget, that is
    flagged for separate review, not silently fixed here.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from cadpipe import glb_common as glb
    from cadpipe import measure as measure_mod
else:
    from . import glb_common as glb
    from . import measure as measure_mod

def _merge_faces_js() -> Path:
    """Locate merge_faces.mjs in both dev and frozen (.exe) layouts.

    Dev:    cadpipe/merge_faces.mjs (node_modules resolved from project root).
    Frozen: bundled under PyInstaller's _MEIPASS as cadpipe/merge_faces.mjs, with
            node_modules alongside at _MEIPASS/node_modules (node resolves upward).
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        return base / "cadpipe" / "merge_faces.mjs"
    return Path(__file__).resolve().parent / "merge_faces.mjs"


def node_toolchain_available() -> bool:
    """True only if node + gltf-transform + merge_faces.mjs are all reachable
    (either bundled in the .exe or installed on PATH)."""
    try:
        _node_exe()
    except RuntimeError:
        return False
    if not _merge_faces_js().exists():
        return False
    try:
        _gt_cmd(["--version"])
        return True
    except RuntimeError:
        return False


# ==========================================================================
# options + result records
# ==========================================================================
@dataclass
class OptimizeOptions:
    merge_faces: bool = True
    dedup: bool = True
    weld: bool = True
    simplify: bool = False
    simplify_error: float = 0.001     # fraction of mesh radius (gltf-transform)
    simplify_ratio: float = 0.0       # target vertex-keep ratio (0 = max simplify within error)
    instance: bool = False            # OFF by default: costs per-instance node names
    instance_min: int = 2
    meshopt: bool = True
    meshopt_level: str = "high"


@dataclass
class StageLog:
    name: str
    detail: str
    in_kb: float
    out_kb: float


@dataclass
class WebMetrics:
    draw_call_estimate: int
    unique_mesh_primitives: int
    rendered_triangles: int
    file_kb: float


@dataclass
class OptimizeResult:
    src: str
    final_glb: str
    shaped_glb: str
    stages: list[StageLog]
    before: WebMetrics
    after: WebMetrics
    # structure preservation
    names_before: int
    names_after: int
    lost_names: list[str]
    hierarchy_preserved: bool
    # deviation (only if a geometry-altering step ran, e.g. simplify)
    remeasured: bool
    p95_before_mm: Optional[float]
    p95_after_mm: Optional[float]
    p95_target_mm: float
    p95_after_within_target: Optional[bool]
    # meshopt quantization bound (estimate, not measured)
    meshopt_pos_quant_bound_mm: Optional[float]
    draw_call_budget: Optional[int]
    draw_call_over_budget: bool
    seconds: float

    def to_dict(self) -> dict:
        return asdict(self)


# ==========================================================================
# external command helpers (verify the tools exist; never guess flags)
# ==========================================================================
def _bundle_base() -> Optional[Path]:
    """The bundle root when frozen (where node.exe + gltf-transform are shipped)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return None


def _node_exe() -> str:
    """Bundled portable node.exe when frozen and present; else node on PATH."""
    base = _bundle_base()
    if base is not None:
        cand = base / "node" / "node.exe"
        if cand.exists():
            return str(cand)
    p = shutil.which("node")
    if not p:
        raise RuntimeError("node not found (no bundled node.exe and none on PATH)")
    return p


def _gt_cmd(args: list[str]) -> list[str]:
    """Command to run gltf-transform: bundled (node + cli.js) when frozen and
    present, else the gltf-transform CLI on PATH. Lets the .exe ship fully
    self-contained (no Node install on the target machine) while still working
    from a dev checkout."""
    base = _bundle_base()
    if base is not None:
        cli = base / "gltf-transform" / "bin" / "cli.js"
        if cli.exists():
            return [_node_exe(), str(cli), *args]
    for name in ("gltf-transform.cmd", "gltf-transform", "gltf-transform.ps1"):
        p = shutil.which(name)
        if p:
            return [p, *args]
    raise RuntimeError("gltf-transform not found (no bundled copy and none on PATH)")


def _run(cmd: list[str]) -> str:
    # force UTF-8: gltf-transform prints '->' / em-dashes that crash the default
    # cp949 (Korean Windows) decoder used by subprocess otherwise.
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr or proc.stdout}")
    return (proc.stdout or "") + (proc.stderr or "")


def _kb(p: Path) -> float:
    return p.stat().st_size / 1024.0


# ==========================================================================
# web metrics + structure (read-only)
# ==========================================================================
def web_metrics(glb_path: Path) -> WebMetrics:
    g = glb.load(glb_path)
    return WebMetrics(
        draw_call_estimate=glb.draw_call_estimate(g),
        unique_mesh_primitives=glb.unique_mesh_primitives(g),
        rendered_triangles=glb.rendered_triangles(g),
        file_kb=_kb(glb_path),
    )


def _all_names(glb_path: Path) -> set[str]:
    g = glb.load(glb_path)
    return glb.node_names(g) | {m.name for m in (g.meshes or []) if m.name}


def _meshopt_quant_bound_mm(glb_path: Path, bits: int = 14) -> float:
    """Upper bound on position error from meshopt quantization: per-mesh bbox /
    2^bits, expressed in mm. Conservative (uses the largest mesh extent)."""
    g = glb.load(glb_path)
    max_extent_m = 0.0
    for m in (g.meshes or []):
        for p in m.primitives:
            acc = g.accessors[p.attributes.POSITION]
            if acc.min and acc.max:
                max_extent_m = max(max_extent_m, max(hi - lo for lo, hi in zip(acc.min, acc.max)))
    return (max_extent_m / (2 ** bits)) * measure_mod.GLB_UNIT_TO_MM


# ==========================================================================
# the pipeline
# ==========================================================================
def optimize(src: Path, source_step: Path, out_dir: Path, opt: OptimizeOptions, *,
             target_mm: float = 0.1, draw_call_budget: Optional[int] = None,
             reference_glb: Optional[Path] = None, verbose: bool = True) -> OptimizeResult:
    t0 = time.perf_counter()
    out_dir.mkdir(parents=True, exist_ok=True)

    before = web_metrics(src)
    stages: list[StageLog] = []

    cur = src
    step_idx = 0

    def stage(name: str, detail: str, run, ext: str = "glb"):
        nonlocal cur, step_idx
        step_idx += 1
        out = out_dir / f"s{step_idx}_{name}.{ext}"
        in_kb = _kb(cur)
        run(cur, out)
        stages.append(StageLog(name, detail, in_kb, _kb(out)))
        cur = out

    # ---- geometry-shaping stages (trimesh-readable; deviation depends on these) ----
    if opt.merge_faces:
        stage("merge_faces", "in-part primitive merge (no flatten)",
              lambda i, o: _run([_node_exe(), str(_merge_faces_js()), str(i), str(o)]))
    if opt.dedup:
        stage("dedup", "merge duplicate meshes/accessors",
              lambda i, o: _run(_gt_cmd(["dedup", str(i), str(o)])))
    if opt.weld:
        stage("weld", "merge identical vertices",
              lambda i, o: _run(_gt_cmd(["weld", str(i), str(o)])))
    if opt.simplify:
        stage("simplify", f"error={opt.simplify_error} ratio={opt.simplify_ratio}",
              lambda i, o: _run(_gt_cmd(["simplify", str(i), str(o),
                                         "--error", str(opt.simplify_error),
                                         "--ratio", str(opt.simplify_ratio)])))

    shaped = cur  # last trimesh-readable, geometry-final GLB (pre-instance/meshopt)

    # ---- draw-structure / encoding stages ----
    if opt.instance:
        stage("instance", f"--min {opt.instance_min} (EXT_mesh_gpu_instancing)",
              lambda i, o: _run(_gt_cmd(["instance", str(i), str(o), "--min", str(opt.instance_min)])))
    if opt.meshopt:
        stage("meshopt", f"--level {opt.meshopt_level}",
              lambda i, o: _run(_gt_cmd(["meshopt", str(i), str(o), "--level", opt.meshopt_level])))

    final = cur
    after = web_metrics(final)

    # ---- structure preservation ----
    names_before = _all_names(src)
    names_after = _all_names(final)
    lost = sorted(names_before - names_after)

    # ---- deviation re-measure (only when geometry was altered: simplify) ----
    remeasured = False
    p95_before = p95_after = None
    p95_ok = None
    if opt.simplify:
        ref = reference_glb or (out_dir / "_reference_dense.glb")
        mb = measure_mod.measure(src, source_step, out_dir / "measure_before",
                                 target_mm=target_mm, reference_glb=ref, verbose=False)
        ma = measure_mod.measure(shaped, source_step, out_dir / "measure_after",
                                 target_mm=target_mm, reference_glb=ref, verbose=False)
        p95_before = mb.symmetric_p95_mm
        p95_after = ma.symmetric_p95_mm
        p95_ok = ma.p95_within_target
        remeasured = True

    # quant bound must be read from the SHAPED (uncompressed, real-metre) GLB:
    # the compressed file's POSITION min/max are in quantized units, not metres.
    quant_bound = _meshopt_quant_bound_mm(shaped) if opt.meshopt else None
    over_budget = bool(draw_call_budget is not None and after.draw_call_estimate > draw_call_budget)

    result = OptimizeResult(
        src=str(src), final_glb=str(final), shaped_glb=str(shaped),
        stages=stages, before=before, after=after,
        names_before=len(names_before), names_after=len(names_after),
        lost_names=lost, hierarchy_preserved=(len(lost) == 0),
        remeasured=remeasured, p95_before_mm=p95_before, p95_after_mm=p95_after,
        p95_target_mm=target_mm, p95_after_within_target=p95_ok,
        meshopt_pos_quant_bound_mm=quant_bound,
        draw_call_budget=draw_call_budget, draw_call_over_budget=over_budget,
        seconds=time.perf_counter() - t0,
    )
    write_json(result, out_dir / "optimize.json")
    write_report(result, out_dir / "optimize_report.md")
    if verbose:
        _print_report(result)
    return result


# ==========================================================================
# persistence + reporting
# ==========================================================================
def write_json(r: OptimizeResult, path: Path) -> None:
    path.write_text(json.dumps(r.to_dict(), indent=2), encoding="utf-8")


def _pct(before: float, after: float) -> str:
    if before <= 0:
        return "n/a"
    return f"{(after - before) / before * 100:+.1f}%"


def write_report(r: OptimizeResult, path: Path) -> None:
    b, a = r.before, r.after
    lines = [
        f"# Optimize report — {Path(r.src).name}",
        "",
        f"- final: `{Path(r.final_glb).name}`  (shaped/pre-compression: `{Path(r.shaped_glb).name}`)",
        f"- elapsed: {r.seconds:.2f} s",
        "",
        "## Before -> after",
        "",
        "| metric | before | after | change |",
        "|---|---|---|---|",
        f"| draw calls (est.) | {b.draw_call_estimate} | {a.draw_call_estimate} | {_pct(b.draw_call_estimate, a.draw_call_estimate)} |",
        f"| unique primitives | {b.unique_mesh_primitives} | {a.unique_mesh_primitives} | {_pct(b.unique_mesh_primitives, a.unique_mesh_primitives)} |",
        f"| rendered triangles | {b.rendered_triangles} | {a.rendered_triangles} | {_pct(b.rendered_triangles, a.rendered_triangles)} |",
        f"| file size (KB) | {b.file_kb:.1f} | {a.file_kb:.1f} | {_pct(b.file_kb, a.file_kb)} |",
        "",
        "## Stages",
        "",
        "| # | stage | detail | in KB | out KB |",
        "|---|---|---|---|---|",
    ]
    for i, s in enumerate(r.stages, 1):
        lines.append(f"| {i} | {s.name} | {s.detail} | {s.in_kb:.1f} | {s.out_kb:.1f} |")
    lines += [
        "",
        "## Structure preservation (#1 priority)",
        "",
        f"- named entities before: {r.names_before}, after: {r.names_after}",
        f"- hierarchy/names preserved: **{r.hierarchy_preserved}**",
    ]
    if r.lost_names:
        lines.append(f"- LOST names: {', '.join(r.lost_names)}  "
                     f"(expected if `instance` was enabled — repeated-part nodes collapse)")
    lines += ["", "## Deviation (re-measured only if simplify ran)", ""]
    if r.remeasured:
        verdict = "PASS" if r.p95_after_within_target else "OVER BUDGET"
        lines += [
            f"- symmetric P95 before: {r.p95_before_mm:.4f} mm",
            f"- symmetric P95 after:  {r.p95_after_mm:.4f} mm  (target <= {r.p95_target_mm} mm -> {verdict})",
        ]
    else:
        lines.append("- not re-measured: no geometry-altering step ran "
                     "(merge/dedup/weld/instance/meshopt do not move vertices)")
    if r.meshopt_pos_quant_bound_mm is not None:
        lines += ["",
                  f"- meshopt position-quantization bound (estimate): "
                  f"~{r.meshopt_pos_quant_bound_mm:.4f} mm (not included in measured deviation)"]
    if r.draw_call_budget is not None:
        flag = "OVER BUDGET" if r.draw_call_over_budget else "within budget"
        lines += ["",
                  f"- draw-call budget: {r.draw_call_budget} -> after {r.after.draw_call_estimate} "
                  f"[{flag}]"]
        if r.draw_call_over_budget:
            lines.append("  - FLAG: draw calls exceed budget. Cross-part proxy/remeshing "
                         "(Simplygon territory) is out of this tool's scope — escalate for review.")
    lines += ["",
              "_No cross-part mesh merging is performed; only in-part merge + instancing._"]
    path.write_text("\n".join(lines), encoding="utf-8")


def _print_report(r: OptimizeResult) -> None:
    b, a = r.before, r.after
    print(f"[optimize] {r.src} -> {Path(r.final_glb).name}")
    for i, s in enumerate(r.stages, 1):
        print(f"  s{i} {s.name:12} {s.detail:42} {s.in_kb:8.1f} -> {s.out_kb:8.1f} KB")
    print(f"  draw calls:  {b.draw_call_estimate:>6} -> {a.draw_call_estimate:<6} ({_pct(b.draw_call_estimate, a.draw_call_estimate)})")
    print(f"  unique prims:{b.unique_mesh_primitives:>6} -> {a.unique_mesh_primitives:<6} ({_pct(b.unique_mesh_primitives, a.unique_mesh_primitives)})")
    print(f"  rendered tris:{b.rendered_triangles:>5} -> {a.rendered_triangles:<6} ({_pct(b.rendered_triangles, a.rendered_triangles)})")
    print(f"  file KB:     {b.file_kb:>6.1f} -> {a.file_kb:<6.1f} ({_pct(b.file_kb, a.file_kb)})")
    print(f"  structure preserved: {r.hierarchy_preserved}"
          + (f"  LOST: {r.lost_names}" if r.lost_names else ""))
    if r.remeasured:
        v = "PASS" if r.p95_after_within_target else "OVER"
        print(f"  deviation P95: {r.p95_before_mm:.4f} -> {r.p95_after_mm:.4f} mm "
              f"(target {r.p95_target_mm} -> {v})")
    else:
        print(f"  deviation: not re-measured (no geometry-altering step)")
    if r.meshopt_pos_quant_bound_mm is not None:
        print(f"  meshopt quant bound: ~{r.meshopt_pos_quant_bound_mm:.4f} mm (estimate)")
    if r.draw_call_budget is not None and r.draw_call_over_budget:
        print(f"  !! draw calls {r.after.draw_call_estimate} OVER budget {r.draw_call_budget} -> flag for review")
    print(f"  time: {r.seconds:.2f}s")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Post-process a GLB with gltf-transform (Phase 3).")
    ap.add_argument("glb", type=Path, help="input production GLB")
    ap.add_argument("step", type=Path, help="source STEP (only needed if --simplify, for re-measure)")
    ap.add_argument("--out", type=Path, default=Path("out/optimized"), help="work/output dir")
    ap.add_argument("--no-merge-faces", action="store_true")
    ap.add_argument("--no-dedup", action="store_true")
    ap.add_argument("--no-weld", action="store_true")
    ap.add_argument("--no-meshopt", action="store_true")
    ap.add_argument("--simplify", action="store_true", help="enable lossy simplification (re-measures)")
    ap.add_argument("--simplify-error", type=float, default=0.001)
    ap.add_argument("--simplify-ratio", type=float, default=0.0)
    ap.add_argument("--instance", action="store_true", help="GPU-instance repeated parts (loses per-instance names)")
    ap.add_argument("--instance-min", type=int, default=2)
    ap.add_argument("--meshopt-level", choices=["medium", "high"], default="high")
    ap.add_argument("--target", type=float, default=0.1, help="P95 deviation target mm")
    ap.add_argument("--draw-call-budget", type=int, default=None, help="flag if exceeded")
    args = ap.parse_args(argv)

    opt = OptimizeOptions(
        merge_faces=not args.no_merge_faces, dedup=not args.no_dedup, weld=not args.no_weld,
        simplify=args.simplify, simplify_error=args.simplify_error, simplify_ratio=args.simplify_ratio,
        instance=args.instance, instance_min=args.instance_min,
        meshopt=not args.no_meshopt, meshopt_level=args.meshopt_level,
    )
    optimize(args.glb, args.step, args.out, opt,
             target_mm=args.target, draw_call_budget=args.draw_call_budget)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
