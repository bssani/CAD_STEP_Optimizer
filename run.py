"""Phase 4 — orchestration + archival ("박제").

Takes ONE STEP path and runs Phase 1 (convert) -> Phase 2 (measure) ->
Phase 3 (optimize), then archives everything needed to reproduce and audit the
run into a timestamped folder:

    reports/run_<UTC timestamp>/
        production.glb              # Phase 1 output
        final.glb                  # Phase 3 deliverable (optimized)
        measure/                   # Phase 2 artifacts (json, report, colormap .ply, reference)
        optimize/                  # Phase 3 artifacts (json, report, stage GLBs)
        screenshots/               # (you drop Babylon Sandbox captures here — manual QA)
        manifest.json              # machine-readable: params, versions, hashes, all metrics
        run_summary.md             # one-page human summary + acceptance verdict

Everything is recorded so a result can be reproduced: input file SHA-256, exact
parameters, and tool versions.

Honest scope note: visual QA (Babylon Sandbox) stays MANUAL — this tool measures
numbers, it does not eyeball renders. The screenshots/ folder + summary just give
that manual step a home and a checklist.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as _md
import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cadpipe import convert as convert_mod
from cadpipe import measure as measure_mod
from cadpipe import optimize as optimize_mod
from cadpipe import glb_common as glb


# ==========================================================================
# provenance helpers
# ==========================================================================
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pkg(name: str) -> str:
    try:
        return _md.version(name)
    except Exception:
        return "unknown"


def _tool_versions(production_glb: Optional[Path]) -> dict:
    gt = "unknown"
    try:
        out = subprocess.run(["gltf-transform", "--version"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", shell=True)
        for line in (out.stdout or "").splitlines():
            line = "".join(c for c in line if c.isprintable()).strip()
            if line and line[0].isdigit():
                gt = line.split()[0]
                break
    except Exception:
        pass
    occt = "unknown"
    if production_glb and production_glb.exists():
        try:
            occt = glb.load(production_glb).asset.generator
        except Exception:
            pass
    return {
        "python": sys.version.split()[0],
        "cadquery-ocp": _pkg("cadquery-ocp"),
        "occt_generator": occt,
        "pymeshlab": _pkg("pymeshlab"),
        "trimesh": _pkg("trimesh"),
        "pygltflib": _pkg("pygltflib"),
        "gltf-transform": gt,
    }


# ==========================================================================
# orchestration
# ==========================================================================
def run(step: Path, out_root: Path, *,
        mode: str = "relative", chord: Optional[float] = None, angular: float = 20.0,
        classa_factor: float = 0.5, classa_angular: float = 12.0,
        target_mm: float = 0.1, samples: int = 100000, ref_factor: float = 10.0,
        up_axis: str = "y",
        opt_options: Optional[optimize_mod.OptimizeOptions] = None,
        draw_call_budget: Optional[int] = None,
        argv: Optional[list[str]] = None) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    archive = out_root / f"run_{ts}"
    (archive / "screenshots").mkdir(parents=True, exist_ok=True)
    opt_options = opt_options or optimize_mod.OptimizeOptions()

    # ---- Phase 1: convert ----
    base = (convert_mod.DEFAULT_STANDARD_ABSOLUTE if mode == "absolute"
            else convert_mod.DEFAULT_STANDARD_RELATIVE)
    if chord is not None:
        base = convert_mod.Tessellation(chord=chord, angular_deg=angular,
                                        relative=(mode != "absolute"))
    else:
        base = convert_mod.Tessellation(chord=base.chord, angular_deg=angular,
                                        relative=(mode != "absolute"))
    profiles = convert_mod.build_profiles(base, classa_chord_factor=classa_factor,
                                          classa_angular_deg=classa_angular)
    production = archive / "production.glb"
    conv = convert_mod.convert_with_ocp(step, production, profiles, up_axis=up_axis, verbose=False)

    prod_metrics = optimize_mod.web_metrics(production)
    final = archive / "final.glb"

    # ---- Phase 2: measure production ----
    # Reference must be FINER than production everywhere, so derive it from the
    # production tessellation (10x finer, same mode) instead of a fixed absolute
    # value (which reads ~0 deviation on large models).
    # If measurement fails, the conversion still succeeded -> deliver the GLB and
    # report the failure rather than crashing the whole run.
    meas_dir = archive / "measure"
    try:
        meas = measure_mod.measure(production, step, meas_dir, target_mm=target_mm,
                                   ref_chord=base.chord / max(ref_factor, 1.0),
                                   ref_relative=base.relative, up_axis=up_axis,
                                   samplenum=samples, verbose=False)
        reference = Path(meas.reference_glb)  # reuse the SAME reference downstream
    except Exception as exc:
        import traceback
        shutil.copy(production, final)
        return _write_convert_only(archive, step, production, final, conv,
                                   prod_metrics, str(exc), traceback.format_exc(), argv)

    # ---- Phase 3: optimize (skipped gracefully if Node toolchain absent) ----
    opt = None
    skip_reason = None
    if optimize_mod.node_toolchain_available():
        opt = optimize_mod.optimize(production, step, archive / "optimize", opt_options,
                                    target_mm=target_mm, draw_call_budget=draw_call_budget,
                                    reference_glb=reference, verbose=False)
        shutil.copy(opt.final_glb, final)
    else:
        skip_reason = ("node / gltf-transform not found on PATH — optimize phase skipped. "
                       "convert + measure completed normally.")
        shutil.copy(production, final)

    final_metrics = opt.after if opt else prod_metrics
    final_p95 = (opt.p95_after_mm if (opt and opt.remeasured) else meas.symmetric_p95_mm)
    final_p95_ok = (final_p95 <= target_mm)
    structure_preserved = opt.hierarchy_preserved if opt else True  # no optimize => unchanged
    lost_names = opt.lost_names if opt else []

    # ---- acceptance verdict ----
    acceptance = {
        "p95_within_target": bool(final_p95_ok),
        "structure_preserved": bool(structure_preserved),
        "names_preserved_in_production": bool(conv.names_ok),
        # only render-breaking defects fail acceptance; open boundaries are often
        # legitimate sheet/surface bodies (common in real CAD) and render fine.
        "topology_clean": bool(meas.topology.parts_non_manifold == 0
                               and meas.topology.parts_flipped == 0),
        "draw_call_within_budget": (None if (draw_call_budget is None or opt is None)
                                    else not opt.draw_call_over_budget),
    }
    overall_pass = all(v for v in acceptance.values() if v is not None)

    # ---- manifest (machine-readable, reproducible) ----
    manifest = {
        "run_id": ts,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "input": {"step": str(step), "sha256": _sha256(step), "size_bytes": step.stat().st_size},
        "parameters": {
            "mode": mode, "chord": base.chord, "angular_deg": angular,
            "relative": base.relative, "target_mm": target_mm,
            "classa_chord_factor": classa_factor, "classa_angular_deg": classa_angular,
            "optimize": asdict(opt_options), "samples": samples,
            "draw_call_budget": draw_call_budget,
        },
        "tool_versions": _tool_versions(production),
        "reproduce_cmd": "run " + " ".join(argv or []),
        "convert": conv.to_dict(),
        "measure": meas.to_dict(),
        "optimize": (opt.to_dict() if opt else {"skipped": True, "reason": skip_reason}),
        "result": {
            "production": asdict(prod_metrics),
            "final": asdict(final_metrics),
            "p95_production_mm": meas.symmetric_p95_mm,
            "p95_final_mm": final_p95,
            "optimize_skipped": opt is None,
            "optimize_skip_reason": skip_reason,
            "lost_names": lost_names,
            "remeasured": bool(opt and opt.remeasured),
            "meshopt_quant_bound_mm": (opt.meshopt_pos_quant_bound_mm if opt else None),
        },
        "final": {"glb": str(final), "p95_mm": final_p95, "p95_within_target": final_p95_ok},
        "acceptance": acceptance,
        "overall_pass": overall_pass,
    }
    (archive / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _write_summary(archive / "run_summary.md", manifest, conv, meas)

    _print_summary(manifest, overall_pass, archive)
    return archive


def _write_convert_only(archive: Path, step: Path, production: Path, final: Path,
                        conv, prod_metrics, error: str, tb: str,
                        argv: Optional[list[str]]) -> Path:
    """Convert succeeded but measurement/optimize crashed: still deliver the GLB
    and report the failure, instead of aborting the whole run."""
    man = {
        "run_id": archive.name.replace("run_", ""),
        "input": {"step": str(step), "sha256": _sha256(step), "size_bytes": step.stat().st_size},
        "tool_versions": _tool_versions(production),
        "convert": conv.to_dict(),
        "web_metrics": asdict(prod_metrics),
        "measurement_error": error,
        "overall_pass": False,
        "note": "convert succeeded; measurement/optimization failed — GLB still produced",
    }
    (archive / "manifest.json").write_text(json.dumps(man, indent=2), encoding="utf-8")
    lines = [
        f"# Run summary — {archive.name}",
        "",
        "**Overall: ⚠️ 변환 성공 / 측정 실패**",
        "",
        "STEP → GLB **변환은 정상**입니다. `production.glb` / `final.glb` 를 쓸 수 있어요.",
        "측정(편차·토폴로지) 단계에서 오류가 나서 그 수치만 없습니다.",
        "",
        "## Input",
        f"- STEP: `{step}`",
        "",
        "## 변환 결과 (사용 가능)",
        f"- 부품 이름 보존: {conv.part_names_preserved}/{conv.part_names_total}",
        f"- draw calls: {prod_metrics.draw_call_estimate}  ·  triangles: {prod_metrics.rendered_triangles}"
        f"  ·  file: {prod_metrics.file_kb:.1f} KB",
        "- 산출물: `production.glb`, `final.glb`",
        "",
        "## 측정 오류 (개발자 전달용 — 이 부분을 캡쳐해서 공유해 주세요)",
        "```",
        tb.strip(),
        "```",
    ]
    (archive / "run_summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[run {archive.name}] 변환 성공 / 측정 실패")
    print(f"  GLB는 정상 생성됨: {final}")
    print(f"  측정 단계 오류: {error}")
    print(f"  자세한 내용: {archive / 'run_summary.md'}")
    return archive


def _write_summary(path: Path, man: dict, conv, meas) -> None:
    a = man["acceptance"]
    r = man["result"]
    p, fin = r["production"], r["final"]
    ok = man["overall_pass"]
    def mark(v): return "✅" if v else ("—" if v is None else "❌")
    lines = [
        f"# Run summary — {man['run_id']}",
        "",
        f"**Overall: {'✅ PASS' if ok else '❌ NEEDS REVIEW'}**",
    ]
    if r["optimize_skipped"]:
        lines += ["", f"> ⚠️ optimize phase skipped: {r['optimize_skip_reason']}"]
    lines += [
        "",
        "## Input",
        f"- STEP: `{man['input']['step']}`",
        f"- SHA-256: `{man['input']['sha256'][:16]}…`  ({man['input']['size_bytes']/1024:.1f} KB)",
        "",
        "## Parameters",
        f"- tessellation: mode=`{man['parameters']['mode']}`, chord={man['parameters']['chord']}"
        f"{'(rel)' if man['parameters']['relative'] else 'mm'}, angular={man['parameters']['angular_deg']}°",
        f"- target P95 ≤ {man['parameters']['target_mm']} mm",
        f"- optimize: {man['parameters']['optimize']}",
        "",
        "## Result (production → final)",
        "",
        "| metric | production | final |",
        "|---|---|---|",
        f"| draw calls (est.) | {p['draw_call_estimate']} | {fin['draw_call_estimate']} |",
        f"| rendered triangles | {p['rendered_triangles']} | {fin['rendered_triangles']} |",
        f"| file size (KB) | {p['file_kb']:.1f} | {fin['file_kb']:.1f} |",
        f"| symmetric P95 (mm) | {r['p95_production_mm']:.4f} | {r['p95_final_mm']:.4f} |",
        "",
        "## Acceptance",
        f"- {mark(a['p95_within_target'])} P95 deviation ≤ target ({r['p95_final_mm']:.4f} ≤ {man['parameters']['target_mm']} mm)",
        f"- {mark(a['names_preserved_in_production'])} part names preserved (production): "
        f"{conv.part_names_preserved}/{conv.part_names_total}",
        f"- {mark(a['structure_preserved'])} hierarchy preserved"
        + (f" (LOST: {', '.join(r['lost_names'])})" if r["lost_names"] else ""),
        f"- {mark(a['topology_clean'])} topology (render-affecting) — {meas.topology.parts_checked} parts: "
        f"non-manifold={meas.topology.parts_non_manifold}, flipped={meas.topology.parts_flipped}",
        f"- ℹ️ open-surface parts: {meas.topology.parts_open} "
        f"(보통 시트/트림 같은 정상 sheet body — 웹 표시엔 문제 없음, 참고용)",
        f"- {mark(a['draw_call_within_budget'])} draw-call budget"
        + ("" if a['draw_call_within_budget'] is not None else " (no budget set / optimize skipped)"),
        "",
        "## Manual QA (not automated)",
        "- [ ] open `final.glb` in Babylon Sandbox; confirm tree + names look right",
        "- [ ] drop screenshots into `screenshots/`",
        "",
        "## Artifacts",
        "- production: `production.glb`  ·  final: `final.glb`",
        f"- deviation colormap: `measure/{Path(meas.colormap_ply).name}`",
        "- measurement: `measure/measurement_report.md`"
        + ("" if r["optimize_skipped"] else "  ·  optimize: `optimize/optimize_report.md`"),
    ]
    if r["meshopt_quant_bound_mm"] is not None:
        lines += ["",
                  f"_Note: meshopt position-quantization adds ≤ ~{r['meshopt_quant_bound_mm']:.4f} mm,",
                  "not included in the measured P95. Reference is OCCT ultra-dense (approx, not exact)._"]
    path.write_text("\n".join(lines), encoding="utf-8")


def _print_summary(man: dict, ok: bool, archive: Path) -> None:
    f = man["final"]
    print(f"[run {man['run_id']}] {'PASS' if ok else 'NEEDS REVIEW'}")
    print(f"  archive: {archive}")
    if man["result"]["optimize_skipped"]:
        print(f"  NOTE: {man['result']['optimize_skip_reason']}")
    print(f"  final P95: {f['p95_mm']:.4f} mm (target {man['parameters']['target_mm']}) "
          f"-> {'within' if f['p95_within_target'] else 'OVER'}")
    print(f"  acceptance: {man['acceptance']}")
    print(f"  summary: {archive / 'run_summary.md'}")


def _pause(frozen: bool) -> None:
    """Keep the console open after a double-click, but never crash when stdin is
    not a tty (piped / CI / called from another program)."""
    if frozen and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\nPress Enter to close...")
        except (EOFError, KeyboardInterrupt):
            pass


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    frozen = getattr(sys, "frozen", False)
    # Double-click (no args) or explicit --gui opens the window. Drag-and-drop a
    # STEP onto the .exe still runs the CLI directly (fast path).
    if (frozen and not argv) or ("--gui" in argv):
        init = next((Path(a) for a in argv
                     if a.lower().endswith((".step", ".stp")) and Path(a).exists()), None)
        try:
            from cadpipe import gui
            gui.launch(run, initial_step=init)
        except Exception as exc:
            print(f"GUI를 열 수 없습니다: {exc}\n터미널에서 'cadpipe-run <model.step>' 로 실행하세요.")
            _pause(frozen)
        return 0
    ap = argparse.ArgumentParser(description="Full pipeline: STEP -> measured, optimized GLB (Phase 4).")
    ap.add_argument("step", type=Path, help="input STEP file")
    ap.add_argument("--out", type=Path, default=None,
                    help="archive root (default: a 'cadpipe_reports' folder next to the STEP file)")
    ap.add_argument("--mode", choices=["relative", "absolute"], default="relative")
    ap.add_argument("--chord", type=float, default=None)
    ap.add_argument("--angular", type=float, default=20.0)
    ap.add_argument("--target", type=float, default=0.1, help="P95 deviation target mm")
    ap.add_argument("--samples", type=int, default=100000)
    ap.add_argument("--ref-factor", type=float, default=10.0,
                    help="reference mesh fineness vs production (def 10x finer; lower if too slow on huge models)")
    ap.add_argument("--up-axis", choices=["y", "z"], default="y",
                    help="glTF up-axis: y = glTF/Babylon standard (def), z = keep CAD Z-up")
    ap.add_argument("--draw-call-budget", type=int, default=None)
    # optimize toggles
    ap.add_argument("--no-merge-faces", action="store_true")
    ap.add_argument("--no-dedup", action="store_true")
    ap.add_argument("--no-weld", action="store_true")
    ap.add_argument("--no-meshopt", action="store_true")
    ap.add_argument("--instance", action="store_true", help="GPU-instance repeats (loses per-instance names)")
    ap.add_argument("--simplify", action="store_true", help="lossy triangle reduction (re-measures)")
    ap.add_argument("--simplify-error", type=float, default=0.001)
    ap.add_argument("--web", action="store_true",
                    help="web-delivery preset: enables simplify (triangle reduction) within the deviation target")
    args = ap.parse_args(argv)

    opt_options = optimize_mod.OptimizeOptions(
        merge_faces=not args.no_merge_faces, dedup=not args.no_dedup, weld=not args.no_weld,
        simplify=args.simplify or args.web, simplify_error=args.simplify_error,
        instance=args.instance, meshopt=not args.no_meshopt,
    )
    # default: drop results right next to the input STEP, so drag-and-drop users
    # always find them in an obvious place (not the unpredictable working dir).
    out_root = args.out or (args.step.resolve().parent / "cadpipe_reports")
    print(f"Input : {args.step}")
    print(f"Output: {out_root}\n")
    try:
        run(args.step, out_root, mode=args.mode, chord=args.chord, angular=args.angular,
            target_mm=args.target, samples=args.samples, ref_factor=args.ref_factor,
            up_axis=args.up_axis, opt_options=opt_options,
            draw_call_budget=args.draw_call_budget, argv=argv)
        rc = 0
    except Exception as exc:  # keep the window open on error when double-clicked
        print(f"\nERROR: {exc}")
        rc = 1
    _pause(frozen)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
