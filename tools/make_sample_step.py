"""Generate a synthetic STEP assembly for pipeline testing.

This produces a deterministic, named assembly so we can verify that node
hierarchy and part names survive STEP -> GLB conversion all the way to the
web viewer. It intentionally includes:

  * a 2-level hierarchy (root assembly -> sub-assembly -> parts),
  * an *instanced* part (4 bolts that share one geometry, used later to
    exercise gltf-transform `instance` in Phase 3),
  * *curved* surfaces (cylinders + a sphere) so that chord/angular
    tessellation deviation is actually non-zero and therefore measurable
    in Phase 2. (A box alone has flat faces and zero tessellation error,
    which would make the deviation pipeline look artificially perfect.)

Units are millimetres (OCCT default), matching CAD convention.

NOTE (honest limitation): this is clean, synthetic geometry. Real CATIA
exports can carry dirty topology (flipped normals, missing faces) that this
sample will *not* reproduce. The topology-QA path in Phase 2 exists for that
real-world case; do not treat a clean run here as proof the QA works on
dirty input.
"""
from __future__ import annotations

import sys
from pathlib import Path

from OCP.TDocStd import TDocStd_Document
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.TDataStd import TDataStd_Name
from OCP.TCollection import TCollection_ExtendedString, TCollection_AsciiString
from OCP.TDF import TDF_Label
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakeSphere,
)
from OCP.gp import gp_Trsf, gp_Vec, gp_Pnt, gp_Ax2, gp_Dir
from OCP.TopLoc import TopLoc_Location
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_StepModelType
from OCP.Interface import Interface_Static
from OCP.IFSelect import IFSelect_ReturnStatus


def _name(label: TDF_Label, text: str) -> None:
    TDataStd_Name.Set_s(label, TCollection_ExtendedString(text))


def _loc(x: float, y: float, z: float) -> TopLoc_Location:
    trsf = gp_Trsf()
    trsf.SetTranslation(gp_Vec(x, y, z))
    return TopLoc_Location(trsf)


def build_document():
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(doc)

    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    # Explicit naming only -- we set every label name ourselves.
    shape_tool.SetAutoNaming_s(False)

    # ---- leaf parts (added as non-assembly top-level shapes) -------------
    base = BRepPrimAPI_MakeBox(80.0, 60.0, 10.0).Shape()
    pillar = BRepPrimAPI_MakeCylinder(8.0, 35.0).Shape()
    bolt = BRepPrimAPI_MakeCylinder(4.0, 14.0).Shape()
    dome = BRepPrimAPI_MakeSphere(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 12.0).Shape()

    base_l = shape_tool.AddShape(base, False)
    _name(base_l, "BasePlate")
    pillar_l = shape_tool.AddShape(pillar, False)
    _name(pillar_l, "Pillar")
    bolt_l = shape_tool.AddShape(bolt, False)
    _name(bolt_l, "Bolt_M8")
    dome_l = shape_tool.AddShape(dome, False)
    _name(dome_l, "DomeCap")

    # ---- sub-assembly: BoltGroup (4 instances of the SAME bolt label) ----
    bolt_group_l = shape_tool.NewShape()
    _name(bolt_group_l, "BoltGroup_ASM")
    corners = [(5, 5, 10), (75, 5, 10), (5, 55, 10), (75, 55, 10)]
    for i, (x, y, z) in enumerate(corners, start=1):
        comp_l = shape_tool.AddComponent(bolt_group_l, bolt_l, _loc(x, y, z))
        _name(comp_l, f"Bolt_{i}")

    # ---- root assembly ---------------------------------------------------
    root_l = shape_tool.NewShape()
    _name(root_l, "DemoBracket_ASM")
    _name(shape_tool.AddComponent(root_l, base_l, _loc(0, 0, 0)), "BasePlate_1")
    _name(shape_tool.AddComponent(root_l, pillar_l, _loc(40, 30, 10)), "Pillar_1")
    _name(shape_tool.AddComponent(root_l, dome_l, _loc(40, 30, 45)), "DomeCap_1")
    _name(shape_tool.AddComponent(root_l, bolt_group_l, _loc(0, 0, 0)), "BoltGroup_1")

    shape_tool.UpdateAssemblies()
    return doc


def write_step(doc, out_path: Path) -> None:
    # AP242 carries assembly structure + names; keep names on write.
    Interface_Static.SetCVal_s("write.step.schema", "AP242DIS")
    Interface_Static.SetIVal_s("write.step.assembly", 1)

    writer = STEPCAFControl_Writer()
    if not writer.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs):
        raise RuntimeError("STEPCAFControl_Writer.Transfer failed")
    status = writer.Write(TCollection_AsciiString(str(out_path)).ToCString())
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise RuntimeError(f"STEP write failed with status {status}")


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("samples/DemoBracket.step")
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = build_document()
    write_step(doc, out)
    size_kb = out.stat().st_size / 1024.0
    print(f"Wrote {out}  ({size_kb:.1f} KB)")
    print("Expected hierarchy:")
    print("  DemoBracket_ASM")
    print("    +- BasePlate_1      -> BasePlate (box, flat faces)")
    print("    +- Pillar_1         -> Pillar    (cylinder, curved)")
    print("    +- DomeCap_1        -> DomeCap   (sphere, curved)")
    print("    +- BoltGroup_1      -> BoltGroup_ASM")
    print("         +- Bolt_1..4   -> Bolt_M8   (instanced x4, curved)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
