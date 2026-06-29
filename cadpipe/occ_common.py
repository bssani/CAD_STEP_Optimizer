"""Low-level OCCT/XCAF helpers shared across the pipeline.

Pure plumbing: read a STEP into an XCAF document, walk part labels/names,
mesh shapes, count triangles, write GLB. No policy decisions here (no class-A
rules, no measurement thresholds) -- those live in convert.py / measure.py.

Honest limitation (keep in mind, do not paper over it): OCCT's STEP import +
BRepMesh can produce flipped normals or drop faces on *dirty* CAD. Nothing in
this module repairs that; it only meshes what it is given. Detecting such
defects is Phase 2's topology-QA job, and even that only *flags* — it does not
auto-heal.
"""
from __future__ import annotations

from pathlib import Path

from OCP.TDocStd import TDocStd_Document
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ShapeTool
from OCP.TCollection import TCollection_ExtendedString, TCollection_AsciiString
from OCP.TDF import TDF_LabelSequence, TDF_Tool
from OCP.TDataStd import TDataStd_Name
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.TopoDS import TopoDS_Compound, TopoDS
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopLoc import TopLoc_Location
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.IMeshTools import IMeshTools_Parameters
from OCP.RWGltf import RWGltf_CafWriter
from OCP.RWMesh import RWMesh_CoordinateSystemConverter, RWMesh_CoordinateSystem
from OCP.TColStd import TColStd_IndexedDataMapOfStringString
from OCP.Message import Message_ProgressRange
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib


# --------------------------------------------------------------------------
# document / labels
# --------------------------------------------------------------------------
def new_document() -> TDocStd_Document:
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(doc)
    return doc


def read_step(path: Path | str, *, names: bool = True,
              colors: bool = True, layers: bool = True) -> TDocStd_Document:
    """Read a STEP file into a fresh XCAF document, preserving names by default."""
    doc = new_document()
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(names)
    reader.SetColorMode(colors)
    reader.SetLayerMode(layers)
    if not reader.Perform(str(path), doc):
        raise RuntimeError(f"STEP read failed: {path}")
    return doc


def shape_tool(doc: TDocStd_Document) -> XCAFDoc_ShapeTool:
    return XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())


def label_name(label) -> str:
    attr = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
        return attr.Get().ToExtString()
    return ""


def label_entry(label) -> str:
    s = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, s)
    return s.ToCString()


def simple_parts(st: XCAFDoc_ShapeTool):
    """Yield (label, name, shape) for every leaf part (real geometry).

    Leaf parts are the shapes we mesh. Assembly/component labels are skipped
    because they only carry structure + locations; meshing the referenced part
    once covers all of its instances.
    """
    labels = TDF_LabelSequence()
    st.GetShapes(labels)
    for i in range(1, labels.Length() + 1):
        L = labels.Value(i)
        if st.IsSimpleShape_s(L):
            yield L, label_name(L), st.GetShape_s(L)


# --------------------------------------------------------------------------
# meshing / geometry stats
# --------------------------------------------------------------------------
def make_mesh_params(chord: float, angular_rad: float, *, relative: bool,
                     in_parallel: bool = True, min_size: float = 0.0) -> IMeshTools_Parameters:
    p = IMeshTools_Parameters()
    p.Deflection = chord          # relative=True -> unitless ratio; else mm
    p.Angle = angular_rad
    p.Relative = relative
    p.InParallel = in_parallel
    if min_size > 0:
        p.MinSize = min_size
    return p


def mesh_shape(shape, params: IMeshTools_Parameters) -> None:
    """Tessellate `shape` in place. Triangulation is stored on the shape's faces."""
    BRepMesh_IncrementalMesh(shape, params)


def count_triangles(shape) -> int:
    total = 0
    loc = TopLoc_Location()
    exp = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            total += tri.NbTriangles()
        exp.Next()
    return total


def bbox_diagonal(shape) -> float:
    """Bounding-box diagonal in mm -- used to relate relative deflection to an
    approximate absolute chord error so logs are interpretable."""
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, True)
    if box.IsVoid():
        return 0.0
    return box.CornerMin().Distance(box.CornerMax())


def free_compound(st: XCAFDoc_ShapeTool) -> TopoDS_Compound:
    labels = TDF_LabelSequence()
    st.GetFreeShapes(labels)
    builder = BRep_Builder()
    comp = TopoDS_Compound()
    builder.MakeCompound(comp)
    for i in range(1, labels.Length() + 1):
        builder.Add(comp, st.GetShape_s(labels.Value(i)))
    return comp


# --------------------------------------------------------------------------
# GLB output
# --------------------------------------------------------------------------
def write_glb(doc: TDocStd_Document, out_path: Path | str, *, up_axis: str = "y") -> None:
    """Write the XCAF document to a binary glTF (.glb). Triangulation must
    already be computed on the shapes (call mesh_shape first).

    up_axis="y" (default): convert OCCT Z-up (CAD) -> glTF Y-up so the model
    stands upright in glTF/Babylon viewers. This is a pure ROTATION, so it does
    NOT flip face winding / normals. up_axis="z" keeps the CAD Z-up convention.
    Length stays metres in both cases (mm->m), so measure.GLB_UNIT_TO_MM is
    unaffected.
    """
    writer = RWGltf_CafWriter(TCollection_AsciiString(str(out_path)), True)  # binary
    if up_axis.lower() == "y":
        conv = RWMesh_CoordinateSystemConverter()
        conv.SetInputCoordinateSystem(RWMesh_CoordinateSystem.RWMesh_CoordinateSystem_Zup)
        conv.SetOutputCoordinateSystem(RWMesh_CoordinateSystem.RWMesh_CoordinateSystem_glTF)
        conv.SetInputLengthUnit(0.001)   # STEP is millimetres
        conv.SetOutputLengthUnit(1.0)    # glTF is metres
        writer.SetCoordinateSystemConverter(conv)
    file_info = TColStd_IndexedDataMapOfStringString()
    if not writer.Perform(doc, file_info, Message_ProgressRange()):
        raise RuntimeError(f"GLB write failed: {out_path}")
