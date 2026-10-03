# Text Monkey feature universe

An offline, interactive 3D product map with 183 source-backed feature records in 15 systems. It consolidates implementation, requirements, the original project brief and 16 accessible related chats. Ten explorable decision paths explain application behavior. Status labels distinguish code presence, partial or held work, requested capabilities and superseded directions. Code presence never certifies deployment or live delivery.

Open `index.html` directly in Chrome. The self-contained file includes its data, renderer, styles and brand icon, with no dependency downloads or network calls. Source-evidence links open GitHub or the corresponding Codex chat only when clicked. The explorer never contacts volunteers or operates the connected application.

Alternatively, serve only this folder:

```sh
python3 -m http.server 58139 --bind 127.0.0.1 --directory docs/feature-universe
```

Open `http://127.0.0.1:58139/` in Chrome.

## Explore

- Drag to orbit; scroll or use the + / − buttons to zoom.
- Choose **Fly**. Drag or use arrow keys to look, WASD to move, Q/E to descend/ascend, and Shift to boost. The + / − controls move forward/backward in flight mode.
- Click stars or use the searchable feature directory. The matrix exposes every feature with native buttons and keyboard access.
- Choose **Decision paths** and follow the outcome buttons. Restart fits the complete tree into view.
- Use **About this map** for status definitions, source-coverage limits and a downloadable feature inventory.
- Press `/` to search, `H` for the overview, or Escape to close details. Motion can be disabled; reduced-motion preferences are respected.

## Maintain and validate

`model.json` is the editable feature and decision inventory. `source-coverage.json` maps every one of the 347 entries in the three source audits to its consolidated feature. This establishes audit coverage, not certainty about unavailable or later conversations. Sources are pinned to the recorded repository snapshot.

Edit `model.json`, `explorer.js`, `styles.css` or `template.html`, then rebuild:

```sh
python3 docs/feature-universe/build.py
node --check docs/feature-universe/explorer.js
```

The build checks unique IDs, valid statuses, source evidence, related-feature targets, branch targets, reachable nodes and terminal outcomes. Commit regenerated `index.html` alongside its sources. All future requirements need an explicit inventory update; this is a snapshot, not a live integration.
