# PyInstaller spec for the CAD STEP->GLB pipeline.
# Build:  pyinstaller cadpipe-run.spec --noconfirm
# Output: dist/cadpipe-run/cadpipe-run.exe  (onedir — robust for big native deps)
#
# Notes:
#  * OCP (OCCT) + pymeshlab + vtk are large native packages; collect_all pulls
#    their DLLs/data so the frozen app can import them.
#  * The Node side (gltf-transform CLI + node.exe) is NOT bundled — it stays an
#    external dependency used only by the optimize phase, which skips gracefully
#    if Node is absent. We DO bundle merge_faces.mjs + node_modules so that, when
#    Node is present, the in-part face merge works without extra install.
from PyInstaller.utils.hooks import collect_all
from pathlib import Path

datas, binaries, hiddenimports = [], [], []
for pkg in ("OCP", "pymeshlab", "vtkmodules", "trimesh", "pygltflib"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# project data: Node helper + its deps + docs
datas += [("cadpipe/merge_faces.mjs", "cadpipe")]
if Path("node_modules").exists():
    datas += [("node_modules", "node_modules")]
for f in ("README.md", "requirements.txt", "사용법.md"):
    if Path(f).exists():
        datas += [(f, ".")]

# ---- bundle the Node toolchain so the optimize phase needs ZERO install ----
# Ship node.exe + the gltf-transform CLI inside the bundle. optimize.py prefers
# these (sys._MEIPASS/node/node.exe and _MEIPASS/gltf-transform/bin/cli.js) over PATH.
import shutil as _sh, subprocess as _sp
_node = _sh.which("node")
if _node and Path(_node).exists():
    datas += [(_node, "node")]
try:
    _gr = _sp.run(["npm", "root", "-g"], capture_output=True, text=True, shell=True).stdout.strip()
    _gt_cli = Path(_gr) / "@gltf-transform" / "cli"
    if _gt_cli.exists():
        datas += [(str(_gt_cli), "gltf-transform")]
except Exception:
    pass

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # NOTE: tkinter is intentionally NOT excluded — it powers the GUI (gui.py).
        "matplotlib", "PyQt5", "PySide2", "PySide6",
        # not used by this app; torch was pulled in transitively, bloats the
        # bundle to >1GB and ships duplicate DLLs that can segfault native imports.
        "torch", "torchvision", "torchaudio", "functorch", "torchgen",
        "scipy", "pandas", "IPython", "notebook",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="cadpipe-run",
    console=True,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    name="cadpipe-run",
)
