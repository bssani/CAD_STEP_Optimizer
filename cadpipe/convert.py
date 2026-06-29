"""Phase 1 — STEP -> GLB conversion with parameterized tessellation.

PROCESSING module (not measurement). It turns a STEP file into a GLB while:
  * defaulting to RELATIVE (size-proportional) deviation, with ABSOLUTE mm as
    an explicit fallback mode;
  * exposing chord + angular deviation as parameters
    (defaults: chord ~0.1 mm-equivalent, angular 20 deg);
  * routing each part through a class profile so "exterior / class-A" parts can
    get tighter deviation -- via a classification HOOK that is deliberately a
    placeholder (see `classify_part`);
  * logging whether hierarchy + part names survived into the GLB.

Backends are kept in SEPARATE functions:
  * `convert_with_ocp`   -- primary (OCCT/OCP, full tessellation control)
  * `convert_with_mayo`  -- optional cross-check stub (Mayo CLI), see its docstring

Honest limitations (do not gloss over):
  * OCCT can flip normals / drop faces on dirty CAD. This module does NOT detect
    or repair that -- that is Phase 2's topology QA, and even then it only flags.
  * RELATIVE deflection is an OCCT-internal per-face scaling. The "approx absolute
    chord" printed in logs (ratio x bbox-diagonal) is an INTERPRETATION aid, not
    the exact chord error OCCT used.
"""
from __future__ import annotations

import argparse
import math
import shutil
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional

if __package__ in (None, ""):  # allow `python cadpipe/convert.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from cadpipe import occ_common as occ
    from cadpipe import glb_common as glb
else:
    from . import occ_common as occ
    from . import glb_common as glb


# ==========================================================================
# tessellation parameters + class profiles
# ==========================================================================
@dataclass(frozen=True)
class Tessellation:
    """One tessellation setting.

    chord:        RELATIVE mode -> unitless ratio (of OCCT's per-face size);
                  ABSOLUTE mode -> millimetres.
    angular_deg:  max angular deviation between adjacent facet normals, degrees.
    relative:     True -> size-proportional (default); False -> absolute mm.
    """
    chord: float
    angular_deg: float
    relative: bool = True
    min_size_mm: float = 0.0
    in_parallel: bool = True

    @property
    def angular_rad(self) -> float:
        return math.radians(self.angular_deg)

    def to_occt(self):
        return occ.make_mesh_params(
            self.chord, self.angular_rad,
            relative=self.relative, in_parallel=self.in_parallel,
            min_size=self.min_size_mm,
        )


# Default starting values per the spec: chord ~0.1 mm-equivalent, angular 20 deg.
# In RELATIVE mode the chord ratio 0.001 ~= 0.1 mm for a ~100 mm part.
DEFAULT_STANDARD_RELATIVE = Tessellation(chord=0.001, angular_deg=20.0, relative=True)
DEFAULT_STANDARD_ABSOLUTE = Tessellation(chord=0.1, angular_deg=20.0, relative=False)


def build_profiles(base: Tessellation,
                   classa_chord_factor: float = 0.5,
                   classa_angular_deg: Optional[float] = 12.0) -> dict[str, Tessellation]:
    """Derive a class-profile table from a base (standard) tessellation.

    class-A parts get a tighter chord (x factor) and tighter angle. Keeping
    class-A as a derived delta means the team tunes ONE base value at the CLI and
    the class-A target tracks it automatically.
    """
    classa = replace(
        base,
        chord=base.chord * classa_chord_factor,
        angular_deg=classa_angular_deg if classa_angular_deg is not None else base.angular_deg,
    )
    return {"standard": base, "class_a": classa}


# ==========================================================================
# classification HOOK (placeholder on purpose)
# ==========================================================================
def classify_part(name: str, layer: Optional[str] = None) -> str:
    """Return the class profile key for a part: 'class_a' or 'standard'.

    >>> PLACEHOLDER <<<  Phase 1 routes per-class params through this hook, but
    the real exterior/class-A rule depends on the team's naming & layer
    conventions, which we do not encode yet. Everything is 'standard' for now.

    This is a PURE decision function (name/layer in, class key out) -- no meshing,
    no I/O -- honouring the measurement/processing separation. To go live, replace
    only this body; the pipeline already routes the result. Example of the shape a
    real rule would take (left disabled intentionally):

        import re
        if layer and layer.upper() in CLASS_A_LAYERS:
            return "class_a"
        if re.search(r"(SKIN|EXT(ERIOR)?|CLASS[_-]?A|VISIBLE|SHOW)", name, re.I):
            return "class_a"
    """
    return "standard"


# ==========================================================================
# result records
# ==========================================================================
@dataclass
class PartReport:
    name: str
    klass: str
    chord: float
    angular_deg: float
    relative: bool
    triangles: int
    bbox_diag_mm: float

    @property
    def approx_abs_chord_mm(self) -> float:
        # interpretation aid only (see module docstring)
        return self.chord * self.bbox_diag_mm if self.relative else self.chord

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["approx_abs_chord_mm"] = round(self.approx_abs_chord_mm, 4)
        return d


@dataclass
class ConvertResult:
    src: str
    dst: str
    backend: str
    mode: str                     # "relative" | "absolute"
    parts: list[PartReport]
    glb_node_count: int
    part_names_total: int
    part_names_preserved: int
    draw_call_estimate: int
    rendered_triangles: int
    unique_mesh_primitives: int
    file_kb: float
    seconds: float
    tree: list[str] = field(default_factory=list)

    @property
    def names_ok(self) -> bool:
        return self.part_names_total > 0 and self.part_names_preserved == self.part_names_total

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["parts"] = [p.to_dict() for p in self.parts]
        d["names_ok"] = self.names_ok
        return d


# ==========================================================================
# primary backend: OCCT / OCP
# ==========================================================================
def convert_with_ocp(src: Path, dst: Path,
                     profiles: dict[str, Tessellation],
                     classifier: Callable[[str, Optional[str]], str] = classify_part,
                     *, up_axis: str = "y", verbose: bool = True) -> ConvertResult:
    """STEP -> GLB via OCCT, meshing each part with its class profile."""
    t0 = time.perf_counter()
    dst.parent.mkdir(parents=True, exist_ok=True)

    doc = occ.read_step(src)
    st = occ.shape_tool(doc)

    parts: list[PartReport] = []
    any_relative = False
    for label, name, shape in occ.simple_parts(st):
        klass = classifier(name, None)
        tess = profiles.get(klass, profiles["standard"])
        any_relative = any_relative or tess.relative
        occ.mesh_shape(shape, tess.to_occt())
        parts.append(PartReport(
            name=name or occ.label_entry(label),
            klass=klass,
            chord=tess.chord,
            angular_deg=tess.angular_deg,
            relative=tess.relative,
            triangles=occ.count_triangles(shape),
            bbox_diag_mm=occ.bbox_diagonal(shape),
        ))

    occ.write_glb(doc, dst, up_axis=up_axis)

    # ---- verify structure survived (read the GLB back) -------------------
    g = glb.load(dst)
    glb_names = glb.node_names(g) | {m.name for m in (g.meshes or []) if m.name}
    expected = [p.name for p in parts]
    preserved = sum(1 for nm in expected if nm in glb_names)

    result = ConvertResult(
        src=str(src), dst=str(dst), backend="ocp",
        mode="relative" if any_relative else "absolute",
        parts=parts,
        glb_node_count=glb.node_count(g),
        part_names_total=len(expected),
        part_names_preserved=preserved,
        draw_call_estimate=glb.draw_call_estimate(g),
        rendered_triangles=glb.rendered_triangles(g),
        unique_mesh_primitives=glb.unique_mesh_primitives(g),
        file_kb=dst.stat().st_size / 1024.0,
        seconds=time.perf_counter() - t0,
        tree=glb.tree_lines(g),
    )
    if verbose:
        _print_report(result)
    return result


# ==========================================================================
# optional cross-check backend: Mayo CLI (kept separate, intentionally a stub)
# ==========================================================================
def convert_with_mayo(src: Path, dst: Path, **_) -> ConvertResult:
    """Convert via Mayo CLI -- OPTIONAL cross-check only, NOT the primary path.

    We chose OCP-first because Mayo's glTF writer exposes node/mesh naming,
    coordinates, format and `mergeFaces`, but NO chord/angular deviation control,
    and Phase 2's ultra-dense reference mesh strictly needs that control.

    This function is left as a verified-shape stub: it refuses clearly rather than
    guessing flags. If Mayo is ever wired in, its invocation belongs HERE so the
    backend stays isolated (do not let Mayo logic leak into convert_with_ocp).
    """
    mayo = shutil.which("mayo")
    if not mayo:
        raise RuntimeError(
            "Mayo CLI not on PATH. Mayo is an optional cross-check only; the primary "
            "backend is convert_with_ocp. Install Mayo and re-check `mayo --help` before "
            "wiring exact flags (do not guess them)."
        )
    raise NotImplementedError(
        "Mayo backend intentionally not implemented: it cannot set chord/angular "
        "deviation, so it cannot satisfy Phase 1/2 requirements. Use convert_with_ocp."
    )


# ==========================================================================
# reporting + CLI
# ==========================================================================
def _print_report(r: ConvertResult) -> None:
    print(f"[convert/{r.backend}] {r.src} -> {r.dst}")
    print(f"  mode={r.mode}  time={r.seconds:.2f}s  size={r.file_kb:.1f} KB")
    print(f"  parts ({len(r.parts)}):")
    print(f"    {'name':16} {'class':9} {'chord':>9} {'deg':>5} {'~abs mm':>8} {'tris':>7}")
    for p in r.parts:
        chord_s = f"{p.chord:.4g}{'(rel)' if p.relative else 'mm'}"
        print(f"    {p.name:16} {p.klass:9} {chord_s:>9} {p.angular_deg:>5.0f} "
              f"{p.approx_abs_chord_mm:>8.3f} {p.triangles:>7}")
    print(f"  hierarchy: {r.glb_node_count} GLB nodes")
    status = "PASS" if r.names_ok else "CHECK"
    print(f"  part-name preservation: {r.part_names_preserved}/{r.part_names_total}  [{status}]")
    print(f"  draw-call estimate (mesh primitives, instanced): {r.draw_call_estimate}")
    print(f"  rendered triangles: {r.rendered_triangles}  "
          f"(unique mesh primitives: {r.unique_mesh_primitives})")
    print("  GLB tree:")
    for line in r.tree:
        print("    " + line)


def _build_base_tess(args) -> Tessellation:
    if args.mode == "absolute":
        chord = args.chord if args.chord is not None else DEFAULT_STANDARD_ABSOLUTE.chord
        return Tessellation(chord=chord, angular_deg=args.angular, relative=False,
                            min_size_mm=args.min_size, in_parallel=not args.no_parallel)
    chord = args.chord if args.chord is not None else DEFAULT_STANDARD_RELATIVE.chord
    return Tessellation(chord=chord, angular_deg=args.angular, relative=True,
                        min_size_mm=args.min_size, in_parallel=not args.no_parallel)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="STEP -> GLB conversion (Phase 1).")
    ap.add_argument("src", type=Path, help="input STEP file")
    ap.add_argument("dst", type=Path, nargs="?", help="output GLB (default out/<name>.glb)")
    ap.add_argument("--mode", choices=["relative", "absolute"], default="relative",
                    help="deviation mode: relative (size-proportional, default) or absolute mm")
    ap.add_argument("--chord", type=float, default=None,
                    help="chord deviation: ratio in relative mode (def 0.001), mm in absolute (def 0.1)")
    ap.add_argument("--angular", type=float, default=20.0, help="angular deviation in degrees (def 20)")
    ap.add_argument("--min-size", type=float, default=0.0, help="OCCT MinSize in mm (0 = auto)")
    ap.add_argument("--classa-factor", type=float, default=0.5,
                    help="class-A chord tightening factor vs standard (def 0.5)")
    ap.add_argument("--classa-angular", type=float, default=12.0,
                    help="class-A angular deviation in degrees (def 12)")
    ap.add_argument("--no-parallel", action="store_true", help="disable parallel meshing")
    ap.add_argument("--up-axis", choices=["y", "z"], default="y",
                    help="glTF up-axis: y = glTF/Babylon standard (def), z = keep CAD")
    args = ap.parse_args(argv)

    dst = args.dst or Path("out") / (args.src.stem + ".glb")
    base = _build_base_tess(args)
    profiles = build_profiles(base, classa_chord_factor=args.classa_factor,
                              classa_angular_deg=args.classa_angular)
    convert_with_ocp(args.src, dst, profiles, up_axis=args.up_axis)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
