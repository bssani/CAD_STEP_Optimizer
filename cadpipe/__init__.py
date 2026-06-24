"""cadpipe — STEP -> GLB conversion + quality-verification pipeline.

Modules are split to keep MEASUREMENT and PROCESSING separate (a core project
principle):

  occ_common  : low-level OCCT/XCAF helpers (read STEP, names, triangulation)
  glb_common  : low-level glTF/GLB inspection helpers (read-only measurement)
  convert     : Phase 1 — processing: STEP -> GLB with parameterized tessellation
  measure     : Phase 2 — measurement only (Hausdorff, draw calls, topology QA)
  optimize    : Phase 3 — processing: gltf-transform post-processing
"""
