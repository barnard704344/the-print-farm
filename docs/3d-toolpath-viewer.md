# 3D Toolpath Viewer

The File Library viewer performs interactive toolpath inspection entirely in
the browser. It supports uploaded G-code and G-code embedded in compatible 3MF
archives.

## Capabilities

- Solid extrusion preview: every extrusion is drawn as a lit bead sized from
  the slicer's `LINE_WIDTH` and `LAYER_HEIGHT` comments, so prints read the way
  they do in OrcaSlicer's preview
- OrcaSlicer-style feature colours (outer/inner wall, infill, top/bottom
  surface, bridge, support, ironing, and more) with a legend to show or hide
  each feature
- Layer slider showing the print up to any layer, with the current Z height
- Printer bed and grid for scale, plus **Reset view** and **Top** buttons
- Left-drag rotation, right-drag (or Shift-drag) panning, wheel or pinch zoom,
  double-click to reset
- Accurate arcs (G2/G3) within 0.01 mm of the true curve
- No external rendering service or printer plugin

## Performance Notes

The server parses each file once and sends the viewer a compact binary
toolpath (`GET /api/library/files/<id>/toolpath?format=bin`), cached in memory
for recently viewed files; the JSON form without `format=bin` remains
available. The browser draws extrusions with GPU instancing and only redraws
when the view changes. Very large files are capped at 1.5 million extrusion
segments, and the viewer says so when a preview is incomplete. The toolpath viewer's 3D controls are separate from the printer-camera
rotation control described in [Printers and OrcaSlicer](printers-and-orcaslicer.md#camera-feeds).
