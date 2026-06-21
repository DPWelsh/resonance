# resonance

**A personalized, anti-algorithmic music discovery engine.**

Get off the Spotify recommendations train. Seed it with the music you already love, and `resonance` maps the *production-credit* scene around your taste — who actually made the records, with whom — then surfaces the underground artists, labels, and mastering engineers that algorithmic feeds will never show you.

It's built on Discogs **credits**, not collaborative filtering, and it scores by **resonance × obscurity**: how close an artist sits to *your* taste in the collaboration graph, deliberately weighted toward the genuinely unknown.

> **Why not just use Spotify's API?** Because Spotify [deprecated](https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api) `audio-features`, `recommendations`, and `related-artists` for all new apps in Nov 2024 — no waitlist, no way back. This project is the answer: bring your own playlist, and it does the digging they won't.

## How it works

1. **Ingest** — your library, or a Spotify playlist export (CSV) → matched to Discogs releases.
2. **Graph** — pull full release *credits* (producers, remixers, mastering…) and build a person↔person collaboration graph, collapsing each artist's many aliases into one node (union-find).
3. **Score** — personalized PageRank (**resonance**) seeded on what you own; Leiden communities (sub-scenes); betweenness (hubs). Credits are role-weighted, so producers outrank mastering engineers.
4. **Discover** — rank *unowned* artists by resonance × obscurity to surface hidden gems; split producers vs mastering engineers; find up-and-coming names and labels.

## Outputs

- `scene_map.html` — interactive WebGL map (node size = resonance, colour = sub-scene, ring = owned)
- `obsidian_vault/` — one note per artist (typed frontmatter + `[[wikilinks]]`) for Obsidian graph view + Dataview
- `scene.db` — queryable SQLite · `scene_graph.graphml` / `.json` — Gephi / Cytoscape exports
- `resonance.md` — ranked report + buy list

See [`GRAPH_GUIDE.md`](GRAPH_GUIDE.md) for how to read the graph and what *resonance* actually measures.

## Status

**Working today** (Discogs → graph → resonance → outputs):
- Resumable Discogs harvester (library + label discographies + bounded 1-hop from core artists)
- Alias resolution + role-weighted collaboration graph
- Personalized PageRank resonance, Leiden sub-scene clusters, betweenness hubs
- WebGL map, Obsidian vault, SQLite, GraphML/JSON, ranked report
- Producer vs mastering-engineer breakdown; up-and-coming artist/label discovery
- **Web app** (`app/`, FastAPI): paste a **DJ setlist** → a two-world **artist ↔ record-label** graph, genre-coloured, explorable three ways — a 2D sigma.js map, a spinning 3D force-graph globe, and a classic view. Click any node for a plain-English explainer of how it's related, plus **Discogs/Bandcamp links and inline track playback** (Discogs videos). Live build progress; preloaded example sets.

**Roadmap:**
- `discovery = resonance × obscurity × freshness` scoring (anti-popularity)
- **Bandcamp** layer — obscurity signal (supporter counts), fan-collection overlap graph, playable previews + buy links
- Alias resolution + bounded 1-hop in the web app's deep-scan path
- Deploy (Fly.io / Render) + a hosted public demo

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env          # add your Discogs token
python harvest_scene.py       # resumable; caches to scene_cache/
python build_graph.py         # scores -> scene.db + exports
python emit_outputs.py        # map + vault + report
open scene_map.html
```

Requires a free [Discogs personal access token](https://www.discogs.com/settings/developers). Edit `SEED_LABELS` in `harvest_scene.py` for your favourite labels. The CLI harvester expects a `library.json` (extracted from Apple Music); for Spotify playlists, use the web app below.

## Run the web app

Paste a **DJ setlist** (or click a preloaded set) → it matches each track on Discogs, builds the **artist ↔ record-label** graph, and you explore it three ways. Click any node for an explainer of how it's related, with Discogs/Bandcamp links and an inline player for its tracks.

```bash
pip install -r requirements.txt -r app/requirements.txt
cp .env.example .env          # add your Discogs token
python3 app/server.py         # from repo root
```

Then open:
- **3D globe** — http://127.0.0.1:8765/globe
- **2D map** — http://127.0.0.1:8765/sigma

Two sets come preloaded (a Panorama Bar techno set + a deep-house set). Paste your own as `Artist <tab> Title` lines (or `Artist - Title`), or upload a `.txt`/`.csv`. First build of a new set runs the Discogs harvest (rate-limited, a few minutes, with live progress); it's cached after, so rebuilds are instant.

## How it's different

Spotify's recsys optimizes for engagement → the popular and the safe. `resonance` is built on three things their recommendations ignore: **production credits** (creative lineage), **your own curation** (not "users like you"), and **anti-popularity** (it *upweights* the unknown).

## Prior art

A survey of ~30 open-source projects found none combining personalized PageRank + Discogs credits + alias resolution + sub-scene clustering + these outputs. Closest analogs and acknowledged borrowings:
- [MediumlySalted/music-collaboration-graph-mining-project](https://github.com/MediumlySalted/music-collaboration-graph-mining-project) — role-weighted credit graph + aliases + PageRank (MusicBrainz, global)
- [etcyl/discogs-recommender](https://github.com/etcyl/discogs-recommender) — Discogs role-weight taxonomy + personal collection (no graph)
- [SimplicityGuy/discogsography](https://github.com/SimplicityGuy/discogsography) — heavy-duty Discogs → Neo4j knowledge graph
- [JOJ0/discodos](https://github.com/JOJ0/discodos) — mature Discogs collection ingestion for DJs

## License

[MIT](LICENSE).
