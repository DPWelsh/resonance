#!/usr/bin/env python3
"""
Scene-graph emitters — turn the scored graph into the deliverables.
Design: docs/plans/2026-06-14-scene-resonance-graph-design.md

Reads scene_graph.json (written by build_graph.py) and emits:
  - scene_map.html      standalone WebGL map (ipysigma): size=resonance, color=cluster, ring=owned
  - obsidian_vault/     one note per canonical artist (typed YAML + [[wikilinks]]) for Obsidian+Dataview
  - resonance.md        top-100 ranked report + buy list (high resonance, owned=0)
"""
import json, os, re, html
from collections import defaultdict
import networkx as nx
from networkx.readwrite import json_graph
from ipysigma import Sigma

ROOT = os.path.dirname(os.path.abspath(__file__))
GRAPH_JSON = os.path.join(ROOT, 'scene_graph.json')
VAULT = os.path.join(ROOT, 'obsidian_vault')
TOP_COLLAB = 12     # wikilinks per note
REPORT_N   = 100

def load_graph():
    with open(GRAPH_JSON) as f:
        return json_graph.node_link_graph(json.load(f))

def safe_filename(name):
    s = re.sub(r'[/\\:*?"<>|]', '-', name).strip().strip('.')
    s = re.sub(r'\s+', ' ', s)
    return (s or 'unnamed')[:120]

def bucket(resonance):       # 1..5 for Obsidian color grouping / Extended Graph
    return min(5, max(1, int(resonance // 20) + 1)) if resonance > 0 else 1

# ----------------------------------------------------------------- interactive map
def emit_map(g):
    # ensure clean attribute names/types on a copy for ipysigma
    for n, d in g.nodes(data=True):
        d['owned_flag'] = 'owned' if int(d.get('owned', 0)) > 0 else 'new'
        d['label'] = d.get('label') or str(n)
    out = os.path.join(ROOT, 'scene_map.html')
    Sigma.write_html(
        g, out,
        fullscreen=True,
        height=820,
        node_size='resonance', node_size_range=(3, 30), node_size_scale='lin',
        node_color='cluster',
        node_label='label',
        node_border_color='owned_flag',
        node_border_color_palette={'owned': '#ffffff', 'new': '#22222200'},
        default_node_border_ratio=0.18,
        edge_weight='weight',
        edge_size_range=(0.2, 3),
        default_edge_color='#cccccc40',
        start_layout=10,                  # run ForceAtlas2 ~10s in-browser
        hide_edges_on_move=True,
        label_density=2,
        node_metrics=[],
    )
    print('wrote scene_map.html')

# ------------------------------------------------------------------ obsidian vault
def emit_vault(g):
    os.makedirs(VAULT, exist_ok=True)
    # top collaborators per node
    collab = defaultdict(list)
    for a, b, d in g.edges(data=True):
        w = d.get('weight', 0)
        collab[a].append((b, w)); collab[b].append((a, w))
    label = {n: (d.get('label') or str(n)) for n, d in g.nodes(data=True)}
    written = 0
    for n, d in g.nodes(data=True):
        name = label[n]
        fn = safe_filename(name)
        res = float(d.get('resonance', 0))
        aliases = [a for a in (d.get('aliases') or '').split(' | ') if a and a != name]
        tops = sorted(collab[n], key=lambda x: -x[1])[:TOP_COLLAB]
        fm = [
            '---',
            f'resonance: {res}',
            f'cluster: {int(d.get("cluster", 0))}',
            f'owned: {str(int(d.get("owned", 0)) > 0).lower()}',
            f'owned_count: {int(d.get("owned", 0))}',
            f'betweenness: {float(d.get("betweenness", 0))}',
            f'hub: {str(bool(int(d.get("hub", 0)))).lower()}',
            f'resonance_bucket: {bucket(res)}',
            f'discogs_id: {n}',
        ]
        if aliases:
            fm.append('aliases: [' + ', '.join('"' + a.replace('"', "'") + '"' for a in aliases) + ']')
        fm.append('---')
        body = [f'# {name}', '']
        meta = f'**Resonance** {res:.1f} · **Cluster** {int(d.get("cluster",0))} · **Owned** {int(d.get("owned",0))}'
        if int(d.get('hub', 0)):
            meta += ' · 👑 hub'
        body += [meta, '']
        if aliases:
            body += ['*aka ' + ', '.join(aliases) + '*', '']
        body += ['## Top collaborators', '']
        for b, w in tops:
            body.append(f'- [[{safe_filename(label[b])}]]  ·  {w:.2f}')
        with open(os.path.join(VAULT, fn + '.md'), 'w') as f:
            f.write('\n'.join(fm) + '\n\n' + '\n'.join(body) + '\n')
        written += 1
    print(f'wrote obsidian_vault/ ({written} notes)')

# ------------------------------------------------------------------------- report
def emit_report(g):
    ns = sorted((d for _, d in g.nodes(data=True)), key=lambda d: -float(d.get('resonance', 0)))
    def row(d):
        flag = '👑' if int(d.get('hub', 0)) else ''
        return (f"| {d.get('label','?')} | {float(d.get('resonance',0)):.1f} | "
                f"{int(d.get('owned',0))} | c{int(d.get('cluster',0))} | {flag} |")
    lines = ['# Scene Resonance — ranked', '',
             f'_{g.number_of_nodes()} people · {g.number_of_edges()} collaborations_', '',
             '## Top by resonance', '',
             '| Artist | Resonance | Owned | Cluster | Hub |', '|---|--:|--:|---|:-:|']
    lines += [row(d) for d in ns[:REPORT_N]]
    buy = [d for d in ns if int(d.get('owned', 0)) == 0][:50]
    lines += ['', '## Buy list — high resonance, not owned', '',
              '| Artist | Resonance | Cluster | Hub |', '|---|--:|---|:-:|']
    for d in buy:
        flag = '👑' if int(d.get('hub', 0)) else ''
        lines.append(f"| {d.get('label','?')} | {float(d.get('resonance',0)):.1f} | c{int(d.get('cluster',0))} | {flag} |")
    with open(os.path.join(ROOT, 'resonance.md'), 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print('wrote resonance.md')

if __name__ == '__main__':
    g = load_graph()
    emit_map(g)
    emit_vault(g)
    emit_report(g)
