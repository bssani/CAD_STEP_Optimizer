"""Phase 0 smoke conversion: STEP -> GLB with the simplest possible options.

Goal of Phase 0 is only to prove the OCCT path works end to end and that the
assembly *hierarchy + part names* survive into the GLB. It is deliberately NOT
the production converter -- chord/angular deviation parameterisation, the
class-A classification hook, and the relative-deviation default all belong to
Phase 1 (`convert.py`). Keep this script dumb on purpose.

Pipeline: STEPCAFControl_Reader (names on) -> XCAF document
       -> BRepMesh_IncrementalMesh (fixed, simple params)
       -> RWGltf_CafWriter (binary = .glb)
"""
from __future__ import annotations

import sys
from pathlib import Path

from OCP.TDocStd import TDocStd_Document
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.TCollection import TCollection_ExtendedString, TCollection_AsciiString
from OCP.TDF import TDF_LabelSequence
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.BRep import BRep_Builder
from OCP.TopoDS import TopoDS_Compound
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.IMeshTools import IMeshTools_Parameters
from OCP.RWGltf import RWGltf_CafWriter
from OCP.TColStd import TColStd_IndexedDataMapOfStringString
from OCP.Message import Message_ProgressRange


def read_step(path: Path) -> TDocStd_Document:
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(doc)

    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)     # keep product/instance names
    reader.SetColorMode(True)
    reader.SetLayerMode(True)
    if not reader.Perform(str(path), doc):
        raise RuntimeError(f"STEP read failed: {path}")
    return doc


def mesh_document(doc: TDocStd_Document) -> None:
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    labels = TDF_LabelSequence()
    shape_tool.GetFreeShapes(labels)

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for i in range(1, labels.Length() + 1):
        builder.Add(compound, shape_tool.GetShape_s(labels.Value(i)))

    # Simplest sensible fixed tessellation for a smoke test.
    params = IMeshTools_Parameters()
    params.Deflection = 0.2    # mm (absolute) -- Phase 1 switches to relative
    params.Angle = 0.3         # rad (~17 deg)
    params.Relative = False
    params.InParallel = True
    BRepMesh_IncrementalMesh(compound, params)


def write_glb(doc: TDocStd_Document, out_path: Path) -> None:
    writer = RWGltf_CafWriter(TCollection_AsciiString(str(out_path)), True)  # binary -> .glb
    file_info = TColStd_IndexedDataMapOfStringString()
    if not writer.Perform(doc, file_info, Message_ProgressRange()):
        raise RuntimeError(f"GLB write failed: {out_path}")


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("samples/DemoBracket.step")
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("out/DemoBracket.glb")
    dst.parent.mkdir(parents=True, exist_ok=True)

    doc = read_step(src)
    mesh_document(doc)
    write_glb(doc, dst)

    size_kb = dst.stat().st_size / 1024.0
    print(f"OK  {src}  ->  {dst}  ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
