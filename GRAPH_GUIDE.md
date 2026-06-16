# How to read the scene resonance graph

Open `scene_map.html` in a browser. Each **node is a person** (producer, remixer, engineer…); each **edge is a shared release credit** between two people.

## Visual encoding
| What you see | What it means |
|---|---|
| **Node size** | **Resonance** — how central that person is to *your* taste (0–100) |
| **Node colour** | **Cluster** — a sub-scene the algorithm found (e.g. Dutch-MFM vs Australian-club) |
| **White ring** | You **own** records by them |
| **No ring** | **Discovery** — in your scene but not in your collection yet |
| **Line thickness** | Strength of collaboration (more shared releases / closer roles) |
| **Hub** | A connector between clusters (high betweenness) — the scene's bridges |

## What "resonance" actually is
A **personalised PageRank**: a random walk that starts from the records you own/recently added, hops along collaborations, and periodically restarts from your collection. **Resonance = where the walk lands most often.**

- It's **personal**, not global fame — the walk always restarts from *your* records, so it measures proximity to *your* taste.
- You can score high **without being owned** — that's the discovery signal (e.g. Jonny Nash, Gigi Masin rank high on the MFM web despite zero owned).
- Ownership pulls harder but is **log-scaled** (no single prolific name dominates) and **role-weighted** (a producer counts far more than a mastering engineer who only *touched* the record — see the `technical` flag).

## How to drive the map
- **Scroll** zoom · **drag** pan · **click a node** → info panel (resonance, cluster, owned count, neighbours) · **search box** → jump to any name.
- Big bright nodes = your taste centres. Big *un-ringed* nodes = the artists to investigate next.

## The data behind it
- `scene.db` — queryable SQLite (`person`, `collab`, `alias_map`, scores). Example: `SELECT name,resonance FROM person WHERE owned=0 AND technical=0 ORDER BY resonance DESC`.
- `obsidian_vault/` — one note per person (typed frontmatter + `[[wikilinks]]`) for Obsidian graph view + Dataview.
- `resonance.md` — ranked report · `scene_graph.graphml`/`.json` — exports for Gephi etc.

Scores are recomputed by `build_graph.py`; the map/vault/report by `emit_outputs.py`.
