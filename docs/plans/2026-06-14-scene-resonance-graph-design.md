# Scene Resonance Graph — Design

**Date:** 2026-06-14
**Goal:** Build a personalized graph of the artists/scene around the music I collect, and compute a **resonance score** per artist — *how much does this person matter to my taste* — with aliases collapsed to one human per node. Primary anchor is **my downloaded library**; record labels are extensible seeds I can add over time.

---

## 1. What it does

- Anchors on my **library** (`library.json`, 894 downloaded tracks, ~4.5 months of collecting) plus seed labels: **Music From Memory (578259)**, **Animalia (1798608)**, **A Colourful Storm**.
- Resolves every track/release to Discogs, pulls **full credits** (producers, remixers, featured, compiled-by, mastering, studios), and builds a **person ↔ person collaboration graph**.
- Computes a **resonance score** (personalized PageRank seeded on my taste), **clusters** (Leiden sub-scenes), and **hub flags** (betweenness).
- Output: a standalone interactive **map**, an **Obsidian** report/browse vault, a queryable **DB**, and standard **exports**.

## 2. Decisions (locked)

| Decision | Choice |
|---|---|
| Scoring stance | **Map-first** — score everyone, mark owned, hubs visible |
| Spine | **Library-first**; labels are appendable seeds (`labels` list → re-run, cache fetches only new) |
| Scope | Library + seed labels + **1 hop**, bounded to **core artists only** (≥2 label releases OR in library) |
| Seed signal | **Curation-based**: ownership + `date_added` recency; weak boost for the ~174 locally-played; negative for skip/dislike. NOT plays/ratings (empirically too sparse — streaming/SoundCloud listener). Use the **real** `date_added` from the JXA pull, not `library.json`'s uniform extraction date. |
| Map home | **Standalone ipysigma WebGL HTML** (node size = resonance, color = cluster) |
| Report/browse | **Obsidian vault + Dataview** (generated from the DB, never hand-edited) |

## 3. Stack

- **Harvest:** raw `requests` + personal token, proactive token-bucket **~55 req/min** (read `X-Discogs-Ratelimit-Remaining`, exp-backoff+jitter on 429), **JSONL cache / resumable** (extend `release_cache.jsonl`). Dedupe by **`master_id`** (fetch master's main_release, not every pressing). Fetch `GET /releases/{id}` per release — the label `/releases` list has **no credits**. Fallback to monthly **XML data dumps** only if 1-hop blows past ~10–15k calls.
- **Store (source of truth):** **SQLite** (`scene.db`: `person`, `release`, `credit`, `collab`, `alias_map`, `scores`) + **DuckDB** for heavy credit/library joins to build the weighted edge list.
- **Scoring:** **python-igraph** + **leidenalg** (arm64 wheels, clean install on Python 3.9). `personalized_pagerank(reset=seed, weights, directed=False, damping≈0.82)`; `leidenalg.find_partition(RBConfiguration)`; `betweenness()`. **NetworkX** for graph build, alias-merge (`contracted_nodes`), and GraphML/JSON export.
- **Map:** **ipysigma** `Sigma.write_html` → one standalone WebGL file. Precompute ForceAtlas2 positions in Python; set embed height explicitly.
- **Report/browse:** vault generator → one `.md` per canonical artist, **strictly-typed YAML** frontmatter (`resonance: float`, `cluster: int`, `owned: bool`, `betweenness: float`, `resonance_bucket: 1-5`), `[[wikilinks]]` to collaborators. Sanitize filenames (strip `/ \ : * ? " < > |`, leading/trailing dots; real name in `aliases:`).
- **Exports:** GraphML + node-link JSON.

## 4. Pipeline

1. **Resolve library → Discogs releases.** 592/894 already matched; re-match the 302 via search. Capture `release_id` + `master_id`.
2. **Harvest credits** for every library release + seed-label discographies (dedupe by master).
3. **Identify core artists** (≥2 label releases OR in library) → **1-hop** fetch their discographies + those releases' credits.
4. **Alias resolution (make-or-break):** per artist fetch `/artists/{id}`; union-find collapse — **union ONLY on `aliases`** (never members/groups → avoids merging a group with its members); **drop id 0 / "Various" / "[no artist]"** (false mega-hub); fold `namevariations` into the canonical node; guard self-loops.
5. **Build weighted edges:** person↔person sharing a release, weight = co-occurrence × **role closeness** (co-production 1.0, remix 0.5–0.7, same-label 0.2–0.3, shared-VA-comp 0.1–0.2).
6. **Seed vector:** `seed(artist) = log1p(owned_copies) + recency(date_added) − skip/dislike_penalty + label_core_bonus`.
7. **Score:** personalized PageRank → resonance; Leiden → clusters; betweenness → hubs. Write back to DB.
8. **Emit:** ipysigma HTML, Obsidian vault, GraphML/JSON, `resonance.md` ranked report (+ buy list = high-resonance, owned=0).

## 5. Key risks / guardrails

- **Harvest cost** — master_id dedup + caching keep it tractable; XML dumps as escape hatch.
- **Alias collapsing** — wrong unions = garbage scores; union on `aliases` only, drop Various/id 0.
- **Resonance "feel"** — log1p ownership (stop prolific artists swamping), role-weighted edges, damping ~0.82; tune on real data.
- **Dataview typing** — generator must emit consistently-typed frontmatter or vault-wide sorts break.
- **Drift** — SQLite/DuckDB is source of truth; **regenerate the vault every run**, never hand-edit scored notes.
- **Env** — system Python 3.9.6 → NetworkX 3.2.x (fine); igraph/leidenalg arm64 wheels OK; avoid graph-tool.

## 6. Deferred / tune-later

- Exact damping (0.80 local ↔ 0.85 spread), role weights, label-core bonus size.
- Leiden resolution (sub-scene granularity) — human-in-the-loop once real data loads.
- Optional vasturiano 3D "hero" view.
