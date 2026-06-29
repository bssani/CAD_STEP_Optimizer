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

import numpy as np
from pygltflib import GLTF2

# glTF componentType -> numpy dtype, and accessor type -> component count
_COMP = {5120: np.int8, 5121: np.uint8, 5122: np.int16,
         5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_NUM = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}


def _accessor_array(g: GLTF2, blob: bytes, idx: int) -> np.ndarray:
    """Decode a glTF accessor into an (count, ncomp) numpy array. Handles
    interleaved buffers via byteStride. (Uncompressed GLB only — meshopt files
    must be decoded first.)"""
    acc = g.accessors[idx]
    bv = g.bufferViews[acc.bufferView]
    dt = np.dtype(_COMP[acc.componentType]).newbyteorder("<")
    ncomp = _NUM[acc.type]
    itemsize = dt.itemsize * ncomp
    stride = bv.byteStride or itemsize
    base = (bv.byteOffset or 0) + (acc.byteOffset or 0)
    # exact span: the LAST element occupies only `itemsize` bytes, not a full
    # `stride`. Using stride*count over-reads past the buffer end on interleaved,
    # tightly-packed exports (-> "buffer is smaller than requested size").
    span = (acc.count - 1) * stride + itemsize
    raw = np.frombuffer(blob, dtype=np.uint8, count=span, offset=base)
    if stride == itemsize:
        return raw.view(dt).reshape(acc.count, ncomp)
    # interleaved: gather each element's itemsize bytes at stride intervals
    rows = np.arange(acc.count)[:, None] * stride + np.arange(itemsize)[None, :]
    return np.ascontiguousarray(raw[rows]).view(dt).reshape(acc.count, ncomp)


def iter_part_meshes(g: GLTF2):
    """Yield (mesh_name, vertices(N,3) float64, faces(M,3) int64) per glTF MESH.

    One glTF mesh == one part. Primitives of a mesh are concatenated so that a
    part split into many BREP-face primitives is measured as a single solid.
    Geometry is in LOCAL mesh coords (topology is transform-invariant)."""
    blob = g.binary_blob()
    for m in (g.meshes or []):
        try:
            verts, faces, voff = [], [], 0
            for p in m.primitives:
                if p.mode not in (None, 4):       # TRIANGLES only
                    continue
                if p.attributes.POSITION is None:
                    continue
                pos = _accessor_array(g, blob, p.attributes.POSITION).astype(np.float64)
                if p.indices is not None:
                    idx = _accessor_array(g, blob, p.indices).reshape(-1).astype(np.int64)
                else:
                    idx = np.arange(len(pos), dtype=np.int64)
                verts.append(pos)
                faces.append(idx.reshape(-1, 3) + voff)
                voff += len(pos)
        except Exception:
            continue  # skip a part we can't decode rather than abort the whole QA
        if verts:
            yield (m.name or "<unnamed>", np.vstack(verts), np.vstack(faces))


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
