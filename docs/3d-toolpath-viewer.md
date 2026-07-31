# 3D Toolpath Viewer

The File Library viewer performs interactive toolpath inspection entirely in
the browser. It supports uploaded G-code and G-code embedded in compatible 3MF
archives.

## Capabilities

- Left-drag rotation, right-drag panning, and wheel/pinch zoom
- Feature-based colours (walls, bridges, ironing, supports, interfaces)
- Staged loading/progress feedback for large files
- High-fidelity arc interpolation and move density
- No external rendering service or printer plugin

## Performance Notes

Large files can take longer to parse and render because the work occurs on the
viewer device. Current defaults prioritise detail and visual quality over raw
speed. The toolpath viewer's 3D controls are separate from the printer-camera
rotation control described in [Printers and OrcaSlicer](printers-and-orcaslicer.md#camera-feeds).
