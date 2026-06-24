// Hierarchy-safe in-part face merge.
//
// The gltf-transform CLI `join` implicitly runs `flatten`, which destroys the
// assembly node hierarchy (verified empirically). That is forbidden by the
// project's #1 rule (structure preservation). This helper instead merges
// primitives ONLY WITHIN each mesh, using joinPrimitives. It never touches
// nodes, never flattens, and never merges across parts -- exactly the
// "한 부품 내부 면 병합" the spec allows, and nothing more.
//
// OCCT writes one primitive per BREP face; merging the compatible primitives of
// a single mesh collapses N face-primitives into 1, cutting draw calls without
// changing the scene graph or part identity.
//
// Usage:  node merge_faces.mjs <input.glb> <output.glb>
// Prints a JSON summary (primitives before/after) to stdout.

import { NodeIO } from '@gltf-transform/core';
import { joinPrimitives } from '@gltf-transform/functions';

const [, , input, output] = process.argv;
if (!input || !output) {
  console.error('usage: node merge_faces.mjs <input.glb> <output.glb>');
  process.exit(2);
}

const io = new NodeIO();
const doc = await io.read(input);
const root = doc.getRoot();
const materials = root.listMaterials();

let primsBefore = 0;
let primsAfter = 0;

for (const mesh of root.listMeshes()) {
  const prims = mesh.listPrimitives();
  primsBefore += prims.length;
  if (prims.length <= 1) { primsAfter += prims.length; continue; }

  // group by compatibility: same mode, material, attribute set, index presence
  const groups = new Map();
  for (const p of prims) {
    const mat = p.getMaterial();
    const key = [
      p.getMode(),
      mat ? materials.indexOf(mat) : 'none',
      p.listSemantics().slice().sort().join(','),
      p.getIndices() ? 'idx' : 'noidx',
    ].join('|');
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(p);
  }

  for (const group of groups.values()) {
    if (group.length <= 1) { primsAfter += 1; continue; }
    const merged = joinPrimitives(group);     // concatenate within this mesh only
    merged.setMaterial(group[0].getMaterial());
    for (const p of group) { mesh.removePrimitive(p); p.dispose(); }
    mesh.addPrimitive(merged);
    primsAfter += 1;
  }
}

await io.write(output, doc);
console.log(JSON.stringify({
  meshes: root.listMeshes().length,
  unique_primitives_before: primsBefore,
  unique_primitives_after: primsAfter,
}));
