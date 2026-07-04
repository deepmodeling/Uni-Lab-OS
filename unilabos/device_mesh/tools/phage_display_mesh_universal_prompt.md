You are responsible for exactly one `_phage_display` device in Uni-Lab-OS. Use web search to find a usable 3D representation for the instrument, then wire it into Uni-Lab-OS.

Goals
1. Inspect the target driver file and identify the decorated `@device(...)` class and device id.
2. Search the web for the exact instrument or the best representative physical model.
   - First preference: downloadable `.stl`
   - Second preference: downloadable `.glb` plus `.stl` conversion or paired assets
   - Third preference: an existing `.xacro` / URDF / CAD model we can adapt
   - Fallback: collect reliable external dimensions and visible opening / loading / tray / door access locations from manuals, brochures, product figures, or videos, then build a simple box-based `.stl` and `macro_device.xacro` from those measurements.
3. Add or update a device mesh package under `Uni-Lab-OS/unilabos/device_mesh/devices/<mesh_folder>/`.
4. Update the target driver's `@device(...)` decorator to include:

```python
model={
    "type": "device",
    "mesh": "<mesh_folder>",
},
```

5. Do not touch other drivers or other workers' files.

Required outputs
- `Uni-Lab-OS/unilabos/device_mesh/devices/<mesh_folder>/macro_device.xacro`
- `Uni-Lab-OS/unilabos/device_mesh/devices/<mesh_folder>/meshes/<...>.stl`
- optional `.glb` if you found a good one and it is easy to keep
- `Uni-Lab-OS/unilabos/device_mesh/devices/<mesh_folder>/meta.json`
- updated target driver file with the `model` block in `@device(...)`

`macro_device.xacro` requirements
- Match the style of `hamilton_vantage/macro_device.xacro`
- Define a macro named `<mesh_folder>`
- Support params:
  `parent_link`, `station_name`, `device_name`, `x`, `y`, `z`, `rx`, `ry`, `r`, `mesh_path`
- Include a fixed base link and visual + collision geometry
- If using fallback dimensions, expose `width`, `depth`, `height` params with sensible defaults
- Include access-point frames as fixed child links / joints on the base link when applicable
  Examples: front door, loading tray, plate slot, carousel opening, pipetting deck opening
- Add short XML comments explaining any assumptions

`meta.json` requirements
- Keep `fileName` and `related`
- Also include:
  - `model_strategy`: `downloaded_model` or `fallback_box`
  - `sources`: array of URLs used
  - `dimensions_m`: width / depth / height in meters
  - `access_points`: array with `name`, `description`, `face`, `center_m`, `size_m` when known
  - `notes`: brief assumptions / uncertainty

Research rules
- Prefer vendor manuals / official specs for dimensions
- Prefer openly accessible CAD / mesh sources when available
- Record exact URLs in `meta.json`
- If a source is ambiguous, say so in `notes`
- If the driver is generic rather than vendor-specific, choose the closest representative instrument matching the class description and record that assumption

Fallback rules
- Use `/home/xzye/projects/DPTech/LeapLab/Uni-Lab-OS/unilabos/device_mesh/tools/generate_box_stl.py` for fallback box STL generation
- The helper is meter-based and accepts:
  `--width --depth --height --output --name`
- Keep access points in xacro frames and `meta.json` even when the STL itself is only a box

Implementation rules
- You are not alone in the codebase. Do not revert unrelated changes and do not edit files outside your ownership.
- Keep ownership to:
  - the target driver file
  - the new or existing device mesh folder for this device
- Use the existing `hamilton_vantage` package as the main reference
- Validate your edited Python file with `python3 -m py_compile <driver>`
- If you create a fallback STL, keep coordinates in meters and make the mesh loadable by the xacro

At the end, report:
- whether you found a real downloadable model or used fallback geometry
- the URLs you relied on
- the files you changed
- any uncertainty that still remains
