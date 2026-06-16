#!/usr/bin/env python3
"""
Scene-graph harvester — resumable Discogs credit harvest.
Design: docs/plans/2026-06-14-scene-resonance-graph-design.md

Spine = library (downloads). Seeds += labels. Bounded 1-hop from CORE artists only.
Everything is cached to scene_cache/ as JSONL/JSON; re-running resumes (only fetches
what's missing). Safe to kill at any time. Run in background; monitor scene_cache/progress.json.

Phases:
  1 resolve library tracks -> Discogs release ids (reuse 592 matched; search the rest)
  2 harvest seed-label discographies (+ sublabels) -> release ids
  3 fetch FULL credits for every gathered release (the part never fetched before)
  4 tally artist appearances -> CORE artists -> 1-hop their other releases' credits
  5 alias resolution for SIGNIFICANT artists (-> union-find later, in scoring step)
"""
import json, os, re, sys, time, random
import requests

ROOT  = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, 'scene_cache'); os.makedirs(CACHE, exist_ok=True)
BASE  = 'https://api.discogs.com'

SEED_LABELS = {578259: 'Music From Memory', 1798608: 'Animalia', 584947: 'A Colourful Storm'}

MIN_INTERVAL   = 1.15   # ~52 req/min, under the 60 authenticated cap
CORE_MIN_LABEL = 2      # artist is CORE if it appears on >=2 seed-label releases ...
                        # ... OR is present in the library (handled in code)
HOP_MIN_APPEAR = 3      # only HOP from core artists who recur >=3x in the corpus
                        # (bounds the 1-hop: 1893 core -> 189 recurring seeds)
SIG_MIN_APPEAR = 2      # resolve aliases only for artists appearing >=2x OR core/owned
SKIP_IDS       = {0}    # id 0 = "Various"/anonymous -> never a node
SKIP_NAMES     = {'various', 'no artist', 'unknown artist', '[no artist]'}
MAX_FETCHES    = 60000  # runaway backstop for one run

# ----------------------------------------------------------------------------- net
def _token():
    for l in open(os.path.join(ROOT, '.env')):
        if l.startswith('Discogs_Token='):
            return l.split('=', 1)[1].strip()
    sys.exit('no Discogs_Token in .env')

S = requests.Session()
S.headers.update({'Authorization': f'Discogs token={_token()}',
                  'User-Agent': 'resonance/0.1 (+https://github.com/DPWelsh/resonance)'})
_last = [0.0]; _fetches = [0]

def api_get(path, params=None, tries=0):
    dt = time.time() - _last[0]
    if dt < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - dt)
    _last[0] = time.time(); _fetches[0] += 1
    url = path if path.startswith('http') else BASE + path
    try:
        r = S.get(url, params=params, timeout=30)
    except requests.RequestException:
        if tries < 6:
            time.sleep(min(60, 2 ** tries) + random.random()); return api_get(path, params, tries + 1)
        return None
    rem = r.headers.get('X-Discogs-Ratelimit-Remaining', '')
    if rem.isdigit() and int(rem) <= 2:
        time.sleep(6)
    if r.status_code == 429:
        wait = int(r.headers.get('Retry-After', 0) or 0) or (min(60, 2 ** tries) + 5 + random.random())
        time.sleep(wait); return api_get(path, params, tries + 1) if tries < 10 else None
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        if tries < 6:
            time.sleep(min(60, 2 ** tries) + 1); return api_get(path, params, tries + 1)
        return None
    try:
        return r.json()
    except ValueError:
        return None

def paginate(path, params=None, key='releases'):
    params = dict(params or {}); params.setdefault('per_page', 100); params['page'] = 1
    while True:
        d = api_get(path, params)
        if not d:
            return
        for item in d.get(key, []):
            yield item
        pg = d.get('pagination', {})
        if params['page'] >= pg.get('pages', 1):
            return
        params['page'] += 1

# --------------------------------------------------------------------------- cache
class Jsonl:
    """Append-only JSONL cache keyed by an explicit string key."""
    def __init__(self, name):
        self.path = os.path.join(CACHE, name); self.data = {}
        if os.path.exists(self.path):
            for line in open(self.path):
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line); self.data[o['_k']] = o
                except Exception:
                    pass
        self.fh = open(self.path, 'a')
    def has(self, k):  return str(k) in self.data
    def get(self, k):  return self.data.get(str(k))
    def put(self, k, obj):
        o = dict(obj); o['_k'] = str(k); self.data[str(k)] = o
        self.fh.write(json.dumps(o, ensure_ascii=False) + '\n'); self.fh.flush()

def save_json(name, obj):
    with open(os.path.join(CACHE, name), 'w') as f:
        json.dump(obj, f)

def load_json(name, default):
    p = os.path.join(CACHE, name)
    return json.load(open(p)) if os.path.exists(p) else default

def progress(**kw):
    kw['fetches_this_run'] = _fetches[0]
    save_json('progress.json', kw)
    print('[progress]', json.dumps(kw), flush=True)

RELEASES = Jsonl('releases.jsonl')   # full release docs (with credits)
ARTISTS  = Jsonl('artists.jsonl')    # full artist docs (aliases/members/groups)
SEARCH   = Jsonl('search.jsonl')     # library-track -> resolved release id

def fetch_release(rid):
    if RELEASES.has(rid):
        return RELEASES.get(rid)
    if _fetches[0] >= MAX_FETCHES:
        return None
    d = api_get(f'/releases/{rid}')
    if d:
        RELEASES.put(rid, d)
    return d

def fetch_artist(aid):
    if ARTISTS.has(aid):
        return ARTISTS.get(aid)
    if _fetches[0] >= MAX_FETCHES:
        return None
    d = api_get(f'/artists/{aid}')
    if d:
        ARTISTS.put(aid, d)
    return d

# --------------------------------------------------------------------------- phases
def norm(s): return (s or '').strip().lower()

def phase1_library():
    """Resolve each library track to a Discogs release id; record owned releases."""
    lib = json.load(open(os.path.join(ROOT, 'library.json')))
    enr = json.load(open(os.path.join(ROOT, 'library_enriched.json')))
    by_pid = {r.get('persistent_id'): r for r in enr}
    owned = load_json('owned_releases.json', {})   # release_id -> #copies
    resolved = 0
    for i, t in enumerate(lib):
        pid = t.get('persistent_id')
        rid = None
        e = (by_pid.get(pid) or {}).get('enrichment') or {}
        if e.get('matched') and e.get('release_id'):
            rid = e['release_id']
        elif SEARCH.has(pid):
            rid = SEARCH.get(pid).get('release_id')
        else:
            artist = re.sub(r'\s*\(\d+\)$', '', t.get('artist') or '').split(',')[0].strip()
            params = {'type': 'release', 'artist': artist,
                      'track': t.get('name') or '', 'per_page': 5}
            d = api_get('/database/search', params)
            res = (d or {}).get('results') or []
            if not res:  # fallback: artist + album
                d = api_get('/database/search', {'type': 'release', 'artist': artist,
                                                 'release_title': t.get('album') or '', 'per_page': 5})
                res = (d or {}).get('results') or []
            rid = res[0]['id'] if res else None
            SEARCH.put(pid, {'release_id': rid, 'artist': artist, 'track': t.get('name')})
        if rid:
            owned[str(rid)] = owned.get(str(rid), 0) + 1
            resolved += 1
        if i % 50 == 0:
            save_json('owned_releases.json', owned)
            progress(phase=1, lib_done=i + 1, lib_total=len(lib), resolved=resolved)
    save_json('owned_releases.json', owned)
    progress(phase=1, lib_done=len(lib), lib_total=len(lib), resolved=resolved, done=True)
    return owned

def phase2_labels():
    """Collect release ids from each seed label + its sublabels."""
    label_rel = load_json('label_releases.json', {})   # release_id -> [label names]
    labels = dict(SEED_LABELS)
    for lid, lname in list(SEED_LABELS.items()):
        info = api_get(f'/labels/{lid}')
        for sub in (info or {}).get('sublabels', []):
            labels[sub['id']] = sub.get('name', f'sub{sub["id"]}')
    save_json('seed_labels_expanded.json', labels)
    for lid, lname in labels.items():
        n = 0
        for rel in paginate(f'/labels/{lid}/releases', key='releases'):
            rid = str(rel.get('id'))
            if rel.get('type') and rel.get('type') != 'release':
                rid = str(rel.get('main_release') or rel.get('id'))
            label_rel.setdefault(rid, [])
            if lname not in label_rel[rid]:
                label_rel[rid].append(lname)
            n += 1
        save_json('label_releases.json', label_rel)
        progress(phase=2, label=lname, label_id=lid, releases=n, total_label_releases=len(label_rel))
    progress(phase=2, done=True, total_label_releases=len(label_rel))
    return label_rel

def phase3_credits(owned, label_rel):
    """Fetch full credits for every gathered release."""
    ids = set(owned) | set(label_rel)
    todo = [r for r in ids if not RELEASES.has(r)]
    for i, rid in enumerate(todo):
        fetch_release(rid)
        if i % 25 == 0:
            progress(phase=3, fetched=i + 1, todo=len(todo), cached_releases=len(RELEASES.data))
        if _fetches[0] >= MAX_FETCHES:
            progress(phase=3, halted_maxfetch=True); return
    progress(phase=3, done=True, cached_releases=len(RELEASES.data))

def artist_id_name(a):
    return a.get('id'), a.get('name')

def collect_artists_from_release(rel):
    out = []
    for a in (rel.get('artists') or []) + (rel.get('extraartists') or []):
        out.append((a.get('id'), a.get('name'), a.get('role') or 'Main'))
    return out

def phase4_hop(owned, label_rel):
    """Tally appearances, pick CORE artists, 1-hop their other releases' credits."""
    appear = {}              # artist_id -> count across corpus
    label_appear = {}        # artist_id -> count on seed-label releases
    owned_artist = set()     # artists present on owned releases
    for rid, rel in RELEASES.data.items():
        on_label = rid in label_rel
        on_owned = rid in owned
        for aid, name, role in collect_artists_from_release(rel):
            if aid in SKIP_IDS or norm(name) in SKIP_NAMES:
                continue
            appear[aid] = appear.get(aid, 0) + 1
            if on_label:
                label_appear[aid] = label_appear.get(aid, 0) + 1
            if on_owned:
                owned_artist.add(aid)
    core = {aid for aid in appear
            if label_appear.get(aid, 0) >= CORE_MIN_LABEL or aid in owned_artist}
    save_json('core_artists.json', sorted(core))
    progress(phase=4, total_artists=len(appear), core_artists=len(core), step='identified')

    hopped = set(load_json('hopped_artists.json', []))
    core_list = [a for a in core if a not in hopped and a not in SKIP_IDS
                 and appear.get(a, 0) >= HOP_MIN_APPEAR]
    progress(phase=4, step='hop_scope', hop_seeds=len(core_list))
    for i, aid in enumerate(core_list):
        new_rids = set()
        for rel in paginate(f'/artists/{aid}/releases', key='releases'):
            if rel.get('role') and rel['role'] not in ('Main', 'TrackAppearance', 'Appearance', 'Remix', 'Producer', 'Co-producer'):
                pass  # keep all roles; role filtering happens in scoring
            rid = str(rel.get('main_release') or rel.get('id'))
            new_rids.add(rid)
        for rid in new_rids:
            fetch_release(rid)
            if _fetches[0] >= MAX_FETCHES:
                hopped.add(aid); save_json('hopped_artists.json', sorted(hopped))
                progress(phase=4, halted_maxfetch=True, hopped=len(hopped)); return
        hopped.add(aid)
        if i % 10 == 0:
            save_json('hopped_artists.json', sorted(hopped))
            progress(phase=4, step='hop', hopped=len(hopped), core_total=len(core),
                     cached_releases=len(RELEASES.data))
    save_json('hopped_artists.json', sorted(hopped))
    progress(phase=4, done=True, cached_releases=len(RELEASES.data))

def phase5_aliases(owned):
    """Resolve aliases for SIGNIFICANT artists only (nodes that will matter)."""
    appear = {}
    owned_artist = set()
    for rid, rel in RELEASES.data.items():
        on_owned = rid in owned
        for aid, name, role in collect_artists_from_release(rel):
            if aid in SKIP_IDS or norm(name) in SKIP_NAMES:
                continue
            appear[aid] = appear.get(aid, 0) + 1
            if on_owned:
                owned_artist.add(aid)
    core = set(load_json('core_artists.json', []))
    sig = {aid for aid in appear
           if appear[aid] >= SIG_MIN_APPEAR or aid in core or aid in owned_artist}
    todo = [a for a in sig if not ARTISTS.has(a) and a not in SKIP_IDS]
    for i, aid in enumerate(todo):
        fetch_artist(aid)
        if i % 25 == 0:
            progress(phase=5, fetched=i + 1, todo=len(todo), cached_artists=len(ARTISTS.data))
        if _fetches[0] >= MAX_FETCHES:
            progress(phase=5, halted_maxfetch=True); return
    progress(phase=5, done=True, cached_artists=len(ARTISTS.data), significant=len(sig))

def main():
    t0 = time.time()
    print('=== scene harvest start ===', flush=True)
    owned = phase1_library()
    label_rel = phase2_labels()
    phase3_credits(owned, label_rel)
    phase5_aliases(owned)            # merge identities for the current graph (quality first)
    phase4_hop(owned, label_rel)     # bounded 1-hop discovery
    phase5_aliases(owned)            # resolve aliases for hop-discovered artists (delta only)
    progress(phase='ALL', done=True,
             releases=len(RELEASES.data), artists=len(ARTISTS.data),
             minutes=round((time.time() - t0) / 60, 1))
    print('=== scene harvest complete ===', flush=True)

if __name__ == '__main__':
    main()
