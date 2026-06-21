#!/usr/bin/env python3
"""Pre-build the deep Discogs cache for the preloaded presets so the chips
build instantly in the app. Safe to re-run (skips already-cached presets)."""
import sys, os, hashlib
APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(APP))
import setlist_graph as slg

for rel in ['presets/moopie.txt', 'presets/klymax.txt']:
    path = os.path.join(APP, rel)
    content = open(path, encoding='utf-8').read().strip()         # mirrors what the app hashes
    out_name = 'app_' + hashlib.md5(content.encode()).hexdigest()[:10]
    sig = hashlib.md5(out_name.encode()).hexdigest()[:8]
    if (os.path.exists(os.path.join(slg.ROOT, 'setlist_cache', sig + '_artists2.jsonl'))
            and os.path.exists(os.path.join(slg.ROOT, 'setlist_cache', sig + '_v4.jsonl'))):
        print('already cached:', rel, flush=True)
        continue
    print('building deep cache for', rel, '...', flush=True)
    rows = slg.read_table(path)
    ppl, edges, tm = slg.build_from_setlist(rows)
    slg.enrich(rows, tm, ppl, edges, out_name)
    slg.deep_enrich(rows, tm, ppl, edges, out_name)
    slg.assign_genres(ppl, edges, tm, out_name)
    print('seeded:', rel, flush=True)
print('done')
