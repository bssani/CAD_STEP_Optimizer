"""Print the node hierarchy + names + mesh assignments of a GLB/glTF file.

Read-only inspection used to confirm that STEP assembly structure and part
names survived conversion. Pure measurement -- it does not modify the file.
"""
from __future__ import annotations

import sys
from pathlib import Path

from pygltflib import GLTF2


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("out/DemoBracket.glb")
    g = GLTF2().load(str(path))

    child_ids = {c for n in g.nodes for c in (n.children or [])}
    roots = [i for i in range(len(g.nodes)) if i not in child_ids]

    mesh_prim_counts = [len(m.primitives) for m in g.meshes]
    total_prims = sum(mesh_prim_counts)

    print(f"file:           {path}")
    print(f"scenes:         {len(g.scenes)}   nodes: {len(g.nodes)}   meshes: {len(g.meshes)}")
    print(f"mesh primitives (= est. draw calls): {total_prims}")
    print(f"accessors: {len(g.accessors)}   buffers: {len(g.buffers)}")
    print("node hierarchy (name [mesh -> N prims]):")

    def walk(idx: int, depth: int) -> None:
        n = g.nodes[idx]
        tag = ""
        if n.mesh is not None:
            tag = f"  [mesh {n.mesh} -> {mesh_prim_counts[n.mesh]} prim]"
        name = n.name if n.name else "<unnamed>"
        print("  " + "  " * depth + f"+- {name}{tag}")
        for c in (n.children or []):
            walk(c, depth + 1)

    for r in roots:
        walk(r, 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
