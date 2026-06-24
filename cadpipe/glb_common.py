"""Read-only glTF/GLB inspection helpers (measurement, never mutation).

These functions answer "what is actually in the GLB?" -- node tree, names,
draw calls, triangle counts. They are used both to *verify* conversion
(Phase 1) and to *measure* it (Phase 2/3). They never write the file.

Draw-call note: the web bottleneck is draw calls, estimated here as the number
of mesh primitives **as instantiated by nodes** -- i.e. a mesh referenced by N
nodes counts as N (each is a separate GPU draw) unless GPU instancing
(EXT_mesh_gpu_instancing) is applied, which Phase 3 may add.
"""
from __future__ import annotations

from pathlib import Path

from pygltflib import GLTF2


def load(path: Path | str) -> GLTF2:
    return GLTF2().load(str(path))


def _roots(g: GLTF2) -> list[int]:
    child_ids = {c for n in g.nodes for c in (n.children or [])}
    return [i for i in range(len(g.nodes)) if i not in child_ids]


def node_names(g: GLTF2) -> set[str]:
    return {n.name for n in g.nodes if n.name}


def node_count(g: GLTF2) -> int:
    return len(g.nodes)


def _prim_triangles(g: GLTF2, prim) -> int:
    """Triangle count of a single primitive (assumes TRIANGLES mode=4/None)."""
    if prim.indices is not None:
        return g.accessors[prim.indices].count // 3
    pos = prim.attributes.POSITION
    if pos is not None:
        return g.accessors[pos].count // 3
    return 0


def draw_call_estimate(g: GLTF2) -> int:
    """Estimated draw calls = sum over nodes of (#primitives in node's mesh).

    Counts instances: a mesh used by 4 nodes contributes 4x. This is the number
    the web budget actually cares about.
    """
    prim_counts = [len(m.primitives) for m in (g.meshes or [])]
    total = 0
    for n in g.nodes:
        if n.mesh is not None:
            total += prim_counts[n.mesh]
    return total


def unique_mesh_primitives(g: GLTF2) -> int:
    """Number of primitives across distinct meshes (ignores instancing)."""
    return sum(len(m.primitives) for m in (g.meshes or []))


def rendered_triangles(g: GLTF2) -> int:
    """Total triangles drawn per frame, counting node instancing."""
    per_mesh = [sum(_prim_triangles(g, p) for p in m.primitives) for m in (g.meshes or [])]
    total = 0
    for n in g.nodes:
        if n.mesh is not None:
            total += per_mesh[n.mesh]
    return total


def unique_triangles(g: GLTF2) -> int:
    """Triangles stored in distinct meshes (geometry size, ignores instancing)."""
    return sum(_prim_triangles(g, p) for m in (g.meshes or []) for p in m.primitives)


def tree_lines(g: GLTF2) -> list[str]:
    prim_counts = [len(m.primitives) for m in (g.meshes or [])]
    out: list[str] = []

    def walk(idx: int, depth: int) -> None:
        n = g.nodes[idx]
        tag = f"  [mesh {n.mesh} -> {prim_counts[n.mesh]} prim]" if n.mesh is not None else ""
        out.append("  " * depth + f"+- {n.name or '<unnamed>'}{tag}")
        for c in (n.children or []):
            walk(c, depth + 1)

    for r in _roots(g):
        walk(r, 0)
    return out
