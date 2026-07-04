# Phage Display Device Mesh Worker Prompts

This file captures the shared worker prompt and the per-device ownership boundaries for
`Uni-Lab-OS/unilabos/devices/_phage_display`.

## Universal Prompt

```text
You are responsible for exactly one `_phage_display` device in Uni-Lab-OS. Use web search to
find a usable 3D representation for the instrument, then wire it into Uni-Lab-OS.

Goals
1. Inspect the target driver file and identify the decorated `@device(...)` class and device id.
2. Search the web for the exact instrument or the best representative physical model.
   - First preference: downloadable `.stl`
   - Second preference: downloadable `.glb` plus `.stl` conversion or paired assets
   - Third preference: an existing `.xacro` / URDF / CAD model we can adapt
   - Fallback: collect reliable external dimensions and visible opening / loading / tray / door
     access locations from manuals, brochures, product figures, or videos, then build a simple
     box-based `.stl` and `macro_device.xacro` from those measurements.
3. Add or update a device mesh package under
   `Uni-Lab-OS/unilabos/device_mesh/devices/<mesh_folder>/`.
4. Update the target driver's `@device(...)` decorator to include:
   model={
     "type": "device",
     "mesh": "<mesh_folder>",
   },
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
- If the driver is generic rather than vendor-specific, choose the closest representative
  instrument matching the class description and record that assumption

Implementation rules
- You are not alone in the codebase. Do not revert unrelated changes and do not edit files outside
  your ownership.
- Keep ownership to:
  - the target driver file
  - the new or existing device mesh folder for this device
- Use the existing `hamilton_vantage` package as the main reference
- Use the fallback STL helper when you need a box mesh:
  `/home/xzye/projects/DPTech/LeapLab/Uni-Lab-OS/unilabos/device_mesh/tools/generate_box_stl.py`
- Validate your edited Python file with `python3 -m py_compile <driver>`
- If you create a fallback STL, keep coordinates in meters and make the mesh loadable by the xacro

At the end, report:
- whether you found a real downloadable model or used fallback geometry
- the URLs you relied on
- the files you changed
- any uncertainty that still remains
```

## Per-Device Ownership

Use the universal prompt above, then substitute the target-specific values below.

| Device ID | Driver | Suggested Mesh Folder | Notes |
| --- | --- | --- | --- |
| `access2_backend` | `Uni-Lab-OS/unilabos/devices/_phage_display/access2_backend.py` | `access2_backend` | Generic plate centrifuge backend; choose closest representative automated plate centrifuge if exact model is unclear. |
| `bio_shake` | `Uni-Lab-OS/unilabos/devices/_phage_display/bio_shake.py` | `bio_shake` | Representative INHECO-style microplate thermoshaker is acceptable if needed. |
| `bio_tek_plate_reader_backend` | `Uni-Lab-OS/unilabos/devices/_phage_display/bio_tek_plate_reader_backend.py` | `bio_tek_plate_reader_backend` | Prefer a specific Agilent BioTek reader if research clearly identifies one. |
| `centrifuge` | `Uni-Lab-OS/unilabos/devices/_phage_display/centrifuge.py` | `centrifuge` | Generic front-end; choose a representative automated plate centrifuge. |
| `clari_ostar_backend` | `Uni-Lab-OS/unilabos/devices/_phage_display/clari_ostar_backend.py` | `clari_ostar_backend` | Prefer a BMG CLARIOstar family model if clearly supported. |
| `cytomat_backend` | `Uni-Lab-OS/unilabos/devices/_phage_display/cytomat_backend.py` | `cytomat_backend` | Prioritize loading-tray / transfer opening frames. |
| `agilent_biotek_406_fx` | `Uni-Lab-OS/unilabos/devices/_phage_display/guessed_agilent_biotek_406_fx.py` | `agilent_biotek_406_fx` | Target the Agilent BioTek 406 FX specifically. |
| `applied_biosystems_seqstudio_genetic_analyzer` | `Uni-Lab-OS/unilabos/devices/_phage_display/guessed_applied_biosystems_seqstudio_genetic_analyzer.py` | `applied_biosystems_seqstudio_genetic_analyzer` | Include cartridge / consumable access area if it can be estimated. |
| `bd_facsmelody` | `Uni-Lab-OS/unilabos/devices/_phage_display/guessed_bd_facsmelody.py` | `bd_facsmelody` | Estimate sample-loading / sort-output faces if needed. |
| `cytiva_akta_pure` | `Uni-Lab-OS/unilabos/devices/_phage_display/guessed_cytiva_akta_pure.py` | `cytiva_akta_pure` | Prioritize front service / fraction collector access faces. |
| `cytiva_biacore_8k_plus` | `Uni-Lab-OS/unilabos/devices/_phage_display/guessed_cytiva_biacore_8k_plus.py` | `cytiva_biacore_8k_plus` | Prioritize plate / sample loading area if available. |
| `eppendorf_centrifuge_5910_ri` | `Uni-Lab-OS/unilabos/devices/_phage_display/guessed_eppendorf_centrifuge_5910_ri.py` | `eppendorf_centrifuge_5910_ri` | Include front lid / top opening semantics if estimated. |
| `hettich_rotanta_460_robotic` | `Uni-Lab-OS/unilabos/devices/_phage_display/guessed_hettich_rotanta_460_robotic.py` | `hettich_rotanta_460_robotic` | Prioritize robotic loading interface / door location. |
| `molecular_devices_qpix_420` | `Uni-Lab-OS/unilabos/devices/_phage_display/guessed_molecular_devices_qpix_420.py` | `molecular_devices_qpix_420` | Include colony plate input / output access faces when known. |
| `qiagen_qiacube_connect` | `Uni-Lab-OS/unilabos/devices/_phage_display/guessed_qiagen_qiacube_connect.py` | `qiagen_qiacube_connect` | Include front door / rotor access if estimated. |
| `tecan_resolvex_a200` | `Uni-Lab-OS/unilabos/devices/_phage_display/guessed_tecan_resolvex_a200.py` | `tecan_resolvex_a200` | Prioritize front deck / filter plate access. |
| `telesis_bio_bioxp_3250` | `Uni-Lab-OS/unilabos/devices/_phage_display/guessed_telesis_bio_bioxp_3250.py` | `telesis_bio_bioxp_3250` | Capture the most likely consumable access face. |
| `incubator` | `Uni-Lab-OS/unilabos/devices/_phage_display/incubator.py` | `incubator` | Generic incubator front-end; choose representative microplate incubator with tray access. |
| `incubator_shaker_stack` | `Uni-Lab-OS/unilabos/devices/_phage_display/incubator_shaker_stack.py` | `incubator_shaker_stack` | Include per-unit tray access frames when practical. |
| `li_ha` | `Uni-Lab-OS/unilabos/devices/_phage_display/li_ha.py` | `li_ha` | Generic liquid handling workstation; choose representative deck-opening geometry. |
| `molecular_devices_backend` | `Uni-Lab-OS/unilabos/devices/_phage_display/molecular_devices_backend.py` | `molecular_devices_backend` | Choose representative multi-mode plate reader if exact model is unclear. |
| `peeler` | `Uni-Lab-OS/unilabos/devices/_phage_display/peeler.py` | `peeler` | Generic plate desealer; include front plate slot or door frame. |
| `plate_reader` | `Uni-Lab-OS/unilabos/devices/_phage_display/plate_reader.py` | `plate_reader` | Generic front-end; choose representative plate-reader drawer form factor. |
| `sealer` | `Uni-Lab-OS/unilabos/devices/_phage_display/sealer.py` | `sealer` | Generic plate sealer; include tray / plate insertion slot frame. |
| `star_backend` | `Uni-Lab-OS/unilabos/devices/_phage_display/star_backend.py` | `star_backend` | Hamilton STAR family deck and loading-tray access points matter most. |
| `v_spin_backend` | `Uni-Lab-OS/unilabos/devices/_phage_display/v_spin_backend.py` | `v_spin_backend` | Prefer exact vendor model if discoverable; otherwise representative robotic centrifuge. |
| `vantage_backend` | `Uni-Lab-OS/unilabos/devices/_phage_display/vantage_backend.py` | `hamilton_vantage` | Existing example already present; improve research provenance and add access-point frames if helpful. |

