#!/usr/bin/env python3
"""
Scene-graph scorer — turns the harvested cache into a scored graph.
Design: docs/plans/2026-06-14-scene-resonance-graph-design.md

Reads scene_cache/ (releases.jsonl, artists.jsonl, owned_releases.json, label_releases.json),
then:
  1 alias union-find  (union ONLY on `aliases`; drop Various/id 0; fold namevariations)
  2 build person<->person weighted collab graph (master_id dedup, role-closeness weights)
  3 score: personalized PageRank (resonance), Leiden (clusters), betweenness (hubs)
  4 write scene.db (SQLite) + scene_graph.graphml + scene_graph.json

Re-runnable in seconds once the cache is populated; safe to run on a PARTIAL cache.
"""
import json, os, re, math, sqlite3, sys
import networkx as nx
import igraph as ig
import leidenalg as la

ROOT  = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, 'scene_cache')

DAMPING        = 0.82
LEIDEN_RES     = 1.0
LABEL_CORE_BONUS = 3.0   # seed boost for being a seed-label core artist
SKIP_IDS       = {0}
SKIP_NAMES     = {'various', 'no artist', 'unknown artist', '[no artist]', ''}

# role -> closeness weight (how "musical"/tight the collaboration is)
ROLE_RULES = [
    (r'remix',                                   0.65),
    (r'produc',                                  1.00),
    (r'written|compos|songwriter|lyric',         0.90),
    (r'feat\.?|featuring',                        0.85),
    (r'vocal|sung|voice',                         0.80),
    (r'performer|instrument|synth|guitar|bass|drum|piano|keyboard|sax|percussion|horn|string', 0.80),
    (r'arrang',                                   0.70),
    (r'engineer|mix|record|dub',                 0.40),
    (r'compiled|curat|selector|dj mix',          0.35),
    (r'master|lacquer|cut',                       0.20),
    (r'design|photo|artwork|illustrat|layout|sleeve', 0.10),
]
def role_weight(role):
    if not role:
        return 1.0  # main artist
    r = role.lower()
    best = 0.0
    for pat, w in ROLE_RULES:
        if re.search(pat, r):
            best = max(best, w)
    return best or 0.5  # unknown credited role

def load_jsonl(name):
    p = os.path.join(CACHE, name); out = {}
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line); out[o['_k']] = o
            except Exception:
                pass
    return out

def load_json(name, default):
    p = os.path.join(CACHE, name)
    return json.load(open(p)) if os.path.exists(p) else default

# --------------------------------------------------------------------------- load
RELEASES = load_jsonl('releases.jsonl')
ARTISTS  = load_jsonl('artists.jsonl')
OWNED    = load_json('owned_releases.json', {})        # release_id -> copies
LABELREL = load_json('label_releases.json', {})        # release_id -> [labels]
if not RELEASES:
    sys.exit('no releases cached yet — let the harvester run first')

def aname(a): return (a.get('name') or '').strip()
def valid(aid, name):
    return aid and aid not in SKIP_IDS and (name or '').strip().lower() not in SKIP_NAMES

# ----------------------------------------------------------------- 1. alias union-find
parent = {}
def find(x):
    parent.setdefault(x, x)
    while parent[x] != x:
        parent[x] = parent[parent[x]]; x = parent[x]
    return x
def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb:
        parent[max(ra, rb)] = min(ra, rb)   # canonical = smallest id (stable)

names, namevars = {}, {}
for aid_s, doc in ARTISTS.items():
    aid = doc.get('id')
    if not aid:
        continue
    names[aid] = aname(doc) or names.get(aid)
    for nv in doc.get('namevariations') or []:
        namevars.setdefault(aid, set()).add(nv)
    for al in doc.get('aliases') or []:           # UNION ONLY ON ALIASES
        if al.get('id'):
            union(aid, al['id'])
            names.setdefault(al['id'], aname(al))

def canon(aid):
    return find(aid) if aid in parent else aid

# ------------------------------------------------------- 2. nodes + collab edges
# master_id dedup: keep one release per master
seen_master, kept = set(), {}
for rid, rel in RELEASES.items():
    mid = rel.get('master_id') or 0
    if mid and mid in seen_master:
        continue
    if mid:
        seen_master.add(mid)
    kept[rid] = rel

nodes = {}          # canon_id -> attrs
def touch(aid, name):
    c = canon(aid)
    n = nodes.setdefault(c, {'id': c, 'name': names.get(c) or name or f'artist{c}',
                             'aliases': set(), 'owned': 0, 'appearances': 0,
                             'label_appearances': 0, 'in_library': False,
                             'owned_seed': 0.0, 'label_seed': 0.0, 'best_role': 0.0})
    if name and name != n['name']:
        n['aliases'].add(name)
    for nv in namevars.get(aid, []):
        n['aliases'].add(nv)
    if name:                                   # vote for display name by credit frequency
        nv = n.setdefault('name_votes', {})
        nv[name] = nv.get(name, 0) + 1
    return c

from collections import defaultdict
edges = defaultdict(float)   # (min,max) -> weight

for rid, rel in kept.items():
    on_owned = rid in OWNED
    on_label = rid in LABELREL
    contribs = []   # (canon_id, role_weight)
    for a in (rel.get('artists') or []):
        if valid(a.get('id'), aname(a)):
            c = touch(a['id'], aname(a)); contribs.append((c, 1.0))
    for a in (rel.get('extraartists') or []):
        if valid(a.get('id'), aname(a)):
            c = touch(a['id'], aname(a)); contribs.append((c, role_weight(a.get('role'))))
    # per-release node tallies (dedup canon within release)
    rel_canons = {}
    for c, w in contribs:
        rel_canons[c] = max(rel_canons.get(c, 0), w)
    for c, w in rel_canons.items():
        n = nodes[c]
        n['appearances'] += 1
        n['best_role'] = max(n['best_role'], w)
        if on_owned:
            copies = OWNED.get(rid, 1)
            n['owned'] += copies               # raw count (any role) — for display
            n['owned_seed'] += copies * w      # role-weighted — for the seed
            n['in_library'] = True
        if on_label:
            n['label_appearances'] += 1
            n['label_seed'] += w               # role-weighted label presence
    # edges: every pair on the release, weighted by min role-closeness
    cs = list(rel_canons.items())
    for i in range(len(cs)):
        for j in range(i + 1, len(cs)):
            a, wa = cs[i]; b, wb = cs[j]
            if a == b:
                continue
            edges[(min(a, b), max(a, b))] += min(wa, wb)

print(f'nodes={len(nodes)}  edges={len(edges)}  releases_kept={len(kept)}/{len(RELEASES)}')

# --------------------------------------------------------------- 3. score (igraph)
idx = {nid: i for i, nid in enumerate(nodes)}
G = ig.Graph(n=len(nodes), directed=False)
G.vs['name'] = list(nodes.keys())
elist = [(idx[a], idx[b]) for (a, b) in edges]
ew    = [edges[(a, b)] for (a, b) in edges]
G.add_edges(elist); G.es['weight'] = ew

# seed vector: log1p(owned) + label-core bonus  (recency layered in a later pass)
seed = []
for nid in nodes:
    n = nodes[nid]
    s = math.log1p(n['owned_seed'])        # role-weighted: mastering/design barely seed
    if n['label_seed'] >= 1.5:             # role-weighted label-core, not raw appearances
        s += LABEL_CORE_BONUS
    seed.append(s)
if sum(seed) == 0:
    seed = [1.0] * len(nodes)   # fallback: uniform if nothing owned yet

ppr = G.personalized_pagerank(reset=seed, weights='weight', damping=DAMPING)
# betweenness on inverse-weight distances (strong tie = short distance)
dist = [1.0 / w if w > 0 else 1.0 for w in ew]
try:
    btw = G.betweenness(weights=dist)
except Exception:
    btw = G.betweenness()
part = la.find_partition(G, la.RBConfigurationVertexPartition,
                         weights='weight', resolution_parameter=LEIDEN_RES, seed=42)
membership = part.membership

mx = max(ppr) or 1.0
for nid in nodes:
    i = idx[nid]
    nodes[nid]['resonance'] = round(100 * ppr[i] / mx, 3)
    nodes[nid]['betweenness'] = round(btw[i], 3)
    nodes[nid]['cluster'] = membership[i]
mb = max(btw) or 1.0
hub_cut = sorted(btw, reverse=True)[max(0, int(len(btw) * 0.05) - 1)] if btw else 0
for nid in nodes:
    nodes[nid]['hub'] = nodes[nid]['betweenness'] >= hub_cut and nodes[nid]['betweenness'] > 0
    nodes[nid]['technical'] = nodes[nid]['best_role'] <= 0.4   # only ever mastering/eng/design
    nv = nodes[nid].get('name_votes')                         # display = most-credited name
    if nv:
        best = max(nv, key=nv.get)
        if best and best != nodes[nid]['name']:
            nodes[nid]['aliases'].add(nodes[nid]['name'])
            nodes[nid]['name'] = best
        nodes[nid]['aliases'].discard(best)

# --------------------------------------------------------------- 4. write outputs
def write_sqlite():
    db = os.path.join(ROOT, 'scene.db')
    if os.path.exists(db):
        os.remove(db)
    con = sqlite3.connect(db); cur = con.cursor()
    cur.execute('''CREATE TABLE person(id INT PRIMARY KEY, name TEXT, aliases TEXT,
                   owned INT, appearances INT, label_appearances INT, in_library INT,
                   resonance REAL, betweenness REAL, cluster INT, hub INT, technical INT)''')
    cur.execute('CREATE TABLE collab(a INT, b INT, weight REAL)')
    cur.execute('CREATE TABLE alias_map(artist_id INT, canonical_id INT)')
    for nid, n in nodes.items():
        cur.execute('INSERT INTO person VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (nid, n['name'], ' | '.join(sorted(n['aliases'])), n['owned'],
                     n['appearances'], n['label_appearances'], int(n['in_library']),
                     n['resonance'], n['betweenness'], n['cluster'], int(n['hub']),
                     int(n['technical'])))
    cur.executemany('INSERT INTO collab VALUES (?,?,?)',
                    [(a, b, w) for (a, b), w in edges.items()])
    cur.executemany('INSERT INTO alias_map VALUES (?,?)',
                    [(aid, find(aid)) for aid in parent])
    cur.execute('CREATE INDEX ix_res ON person(resonance DESC)')
    con.commit(); con.close()
    print('wrote scene.db')

def write_graph_exports():
    g = nx.Graph()
    for nid, n in nodes.items():
        g.add_node(nid, label=n['name'], resonance=n['resonance'], cluster=n['cluster'],
                   owned=int(n['owned']), in_library=int(n['in_library']),
                   betweenness=n['betweenness'], hub=int(n['hub']),
                   technical=int(n['technical']),
                   aliases=' | '.join(sorted(n['aliases'])))
    for (a, b), w in edges.items():
        g.add_edge(a, b, weight=round(w, 3))
    nx.write_graphml(g, os.path.join(ROOT, 'scene_graph.graphml'))
    from networkx.readwrite import json_graph
    with open(os.path.join(ROOT, 'scene_graph.json'), 'w') as f:
        json.dump(json_graph.node_link_data(g), f)
    print('wrote scene_graph.graphml + scene_graph.json')

if __name__ == '__main__':
    write_sqlite()
    write_graph_exports()
    musical = sorted((n for n in nodes.values() if not n['technical']),
                     key=lambda n: -n['resonance'])[:25]
    print('\n=== top 25 by resonance (musical roles) ===')
    for n in musical:
        flag = '👑' if n['hub'] else '  '
        own = f"[{n['owned']}]" if n['owned'] else '   '
        print(f"  {flag} {n['resonance']:6.2f} {own} c{n['cluster']:<3} {n['name']}")
    tech = sorted((n for n in nodes.values() if n['technical']),
                  key=lambda n: -n['resonance'])[:5]
    print('  -- top technical (mastering/eng/design), shown for contrast --')
    for n in tech:
        print(f"     🔧 {n['resonance']:6.2f}     {n['name']}")
