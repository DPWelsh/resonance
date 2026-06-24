#!/usr/bin/env python3
"""
setlist_graph.py — turn ANY DJ setlist (exported track list) into an artists /
discovery graph, rendered as a standalone ipysigma HTML (same style as scene_map.html).

Usage:
  python3 setlist_graph.py "<setlist.txt>" [--out NAME] [--enrich]

Layer 1 (instant, offline): parse the list, extract artists + remixers + features +
  co-artists + shared releases, build a people graph, score + render.
Layer 2 (--enrich): for each track, hit Discogs (search -> release -> credits + label),
  adding real co-credit and shared-label edges + DISCOVERED people not in the set.
  Cached + rate-limited; safe to re-run (resumes).
"""
import sys, os, re, csv, io, json, time, math, unicodedata, hashlib, argparse
from collections import defaultdict
import networkx as nx
import igraph as ig
import leidenalg as la
from ipysigma import Sigma

ROOT = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------- parsing
def read_table(path):
    raw = open(path, 'rb').read()
    txt = None
    for enc in ('utf-16', 'utf-16-le', 'utf-8-sig', 'utf-8', 'latin-1'):
        try:
            t = raw.decode(enc)
            if '\t' in t or ',' in t:
                txt = t; break
        except Exception:
            continue
    if txt is None:
        sys.exit('could not decode ' + path)
    delim = '\t' if '\t' in txt.splitlines()[0] else ','
    rows = [r for r in csv.reader(io.StringIO(txt), delimiter=delim) if any(c.strip() for c in r)]
    hdr = [c.strip().lower() for c in rows[0]]
    def col(*names):
        for nm in names:
            if nm in hdr:
                return hdr.index(nm)
        return None
    ci = {
        'title': col('track title', 'title', 'name', 'track'),
        'artist': col('artist', 'artists'),
        'album': col('album', 'release'),
        'genre': col('genre', 'genres'),
        'date': col('date added', 'added', 'date'),
    }
    if ci['artist'] is None:
        sys.exit('no Artist column found; header=' + str(hdr))
    out = []
    for r in rows[1:]:
        def g(k):
            i = ci[k]
            return r[i].strip() if (i is not None and i < len(r)) else ''
        if g('artist') or g('title'):
            out.append({k: g(k) for k in ci})
    return out

# ----------------------------------------------------------------- name handling
def norm_key(name):
    s = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().lower()
    s = re.sub(r"[^a-z0-9 ]", ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

GENERIC = {
    'original', 'club', 'dub', 'extended', 'instrumental', 'radio', 'vocal', 'beats',
    'going blind', 'rock bottom', 'hot n spicy', 'cumulonimbus', 'zoned', 'casa moda',
    'mike matty show', 'downtown', 'freesole', 'dubberama', 'lost dancefloor', 'reprise',
    'edit', 'version', 'mix', 'rework', 'bonus', 'live', 'acapella', 'intro', 'outro',
    'rock bottom mix', 'bea',
}
# Split collaborators on comma and explicit b2b/vs only — '&' usually denotes ONE act
# (e.g. "Fresh & Low", "Command D & Zara"), so we keep those whole to avoid phantom nodes.
SPLIT_RE = re.compile(r'\s*(?:,|\bb2b\b|\bvs\.?\b)\s*', re.I)

def split_artists(field):
    field = field.strip()
    rel = 'collab'
    # presents / feat split: first token is the main act
    for kw in (' presents ', ' pres. ', ' pres ', ' featuring ', ' feat. ', ' feat ', ' ft. ', ' ft '):
        if kw in field.lower():
            i = field.lower().index(kw)
            head, tail = field[:i], field[i + len(kw):]
            return ([n for n in [head.strip()] if n] +
                    [n for n in SPLIT_RE.split(tail) if n.strip()], 'feat')
    parts = [p.strip() for p in SPLIT_RE.split(field) if p.strip()]
    # strip trailing junk like 'fea'
    parts = [re.sub(r'\b(fea|feat|ft|presents|pres)\b\.?$', '', p, flags=re.I).strip() for p in parts]
    parts = [p for p in parts if p]
    return parts, rel

REMIX_RE = re.compile(r'\(([^)]*)\)')
def extract_remixers(title):
    rem = []
    for inside in REMIX_RE.findall(title):
        s = inside.strip()
        # possessive: "X's ..." -> X
        m = re.match(r"^(.+?)'s\b", s)
        if m:
            cand = m.group(1).strip()
        else:
            # "X Remix / Rmx / Rework" (require strong remix keyword, not bare mix/version/edit)
            m2 = re.match(r'^(.+?)\s+(?:re-?mix|rmx|rework|re-edit|refix|re-rub)\b', s, re.I)
            if not m2:
                continue
            cand = m2.group(1).strip()
        cand = re.sub(r'\b(19|20)\d{2}\b', '', cand).strip()       # drop years
        cand = re.sub(r'\s+', ' ', cand)
        if norm_key(cand) and norm_key(cand) not in GENERIC and len(norm_key(cand)) > 2:
            rem.append(cand)
    return rem

def genre_bucket(g):
    g = (g or '').lower()
    for kw, lab in [('deep house', 'Deep House'), ('tech house', 'Tech House'),
                    ('acid house', 'Acid'), ('acid', 'Acid'), ('dub techno', 'Dub Techno'),
                    ('minimal', 'Minimal'), ('detroit', 'Techno'), ('techno', 'Techno'),
                    ('disco', 'Disco'), ('garage house', 'Garage'), ('garage', 'Garage'),
                    ('drum', 'D&B/Jungle'), ('jungle', 'D&B/Jungle'),
                    ('broken beat', 'Broken Beat'), ('breakbeat', 'Breaks'), ('breaks', 'Breaks'),
                    ('ambient', 'Ambient'), ('leftfield', 'Leftfield'), ('progressive', 'Progressive'),
                    ('tribal', 'Tribal'), ('electro ', 'Electro'), ('house', 'House'),
                    ('electro', 'Electro'), ('electronic', 'Electronic'), ('dance', 'Electronic')]:
        if kw in g:
            return lab
    return 'Other'

GENRE_PALETTE = {
    'Deep House': '#35C9C0', 'House': '#5BC8FF', 'Tech House': '#4C8BE0', 'Techno': '#FF7A59',
    'Dub Techno': '#2E8B9E', 'Minimal': '#9B8BFF', 'Acid': '#B6E04C', 'Electro': '#F2C14E',
    'Disco': '#F25FB0', 'Ambient': '#7FE08A', 'Breaks': '#FF5D5D', 'D&B/Jungle': '#FF8C42',
    'Garage': '#B07FE0', 'Tribal': '#E0A05F', 'Progressive': '#6FA8FF', 'Broken Beat': '#E0C04C',
    'Leftfield': '#9AD0C2', 'Electronic': '#88B0C8', 'Other': '#8893A8',
}

def style_bucket(styles, genres):
    for s in list(styles or []) + list(genres or []):
        b = genre_bucket(s)
        if b != 'Other':
            return b
    sg = list(styles or []) + list(genres or [])
    return sg[0] if sg else 'Other'

# ----------------------------------------------------------------- people registry
class People:
    def __init__(self):
        self.by_key = {}     # norm_key -> attrs
    def touch(self, name, in_set=True, kind='person'):
        k = ('label::' + norm_key(name)) if kind == 'label' else norm_key(name)
        if not k or k == 'label::':
            return None
        p = self.by_key.get(k)
        if not p:
            p = self.by_key[k] = {'key': k, 'name': name, 'appearances': 0,
                                  'is_remixer': False, 'in_set': in_set, 'genres': set(),
                                  'kind': kind, 'discogs_id': None,
                                  'name_votes': defaultdict(int)}
        p['name_votes'][name] += 1
        if in_set and kind == 'person':
            p['in_set'] = True
        return k

def build_from_setlist(rows):
    ppl = People()
    edges = defaultdict(float)
    album_artists = defaultdict(set)   # album -> set(keys)
    track_meta = []
    def add_edge(a, b, w):
        if a and b and a != b:
            edges[(min(a, b), max(a, b))] = max(edges[(min(a, b), max(a, b))], w)
    for r in rows:
        arts, relkind = split_artists(r['artist'])
        gb = genre_bucket(r['genre'])
        keys = []
        for i, a in enumerate(arts):
            k = ppl.touch(a, in_set=True)
            if not k:
                continue
            keys.append(k)
            ppl.by_key[k]['genres'].add(gb)
            if i == 0:
                ppl.by_key[k]['appearances'] += 1
        # co-artist / feat edges
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                add_edge(keys[i], keys[j], 0.85 if relkind == 'feat' else 1.0)
        # remixers from title
        for rm in extract_remixers(r['title']):
            rk = ppl.touch(rm, in_set=True)
            if rk:
                ppl.by_key[rk]['is_remixer'] = True
                ppl.by_key[rk]['genres'].add(gb)
                for k in keys:
                    add_edge(rk, k, 0.65)
        # DJ Sprinkles compilation: "... by DJ Sprinkles"
        if 'by dj sprinkles' in r['album'].lower():
            rk = ppl.touch('DJ Sprinkles', in_set=True)
            ppl.by_key[rk]['is_remixer'] = True
            for k in keys:
                add_edge(rk, k, 0.65)
        # shared release/comp
        alb = r['album'].strip()
        if alb and not re.match(r'^(www\.|http|\[)', alb.lower()):
            for k in keys:
                album_artists[norm_key(alb)].add(k)
        track_meta.append({'title': r['title'], 'keys': keys, 'genre': gb, 'album': alb,
                           'artist_raw': r['artist'], 'date': r.get('date', '')})
    # shared-release edges (only multi-artist comps)
    for alb, ks in album_artists.items():
        ks = list(ks)
        if 2 <= len(ks) <= 8:
            for i in range(len(ks)):
                for j in range(i + 1, len(ks)):
                    add_edge(ks[i], ks[j], 0.5)
    return ppl, edges, track_meta

# ----------------------------------------------------------------- Discogs enrichment
ROLE_RULES = [(r'remix', 0.65), (r'produc', 1.0), (r'written|compos', 0.9),
              (r'feat|featuring', 0.85), (r'vocal|voice', 0.8),
              (r'guitar|bass|drum|synth|piano|keyboard|sax|perc|horn|string|instrument', 0.8),
              (r'arrang', 0.7), (r'engineer|mix|record|dub', 0.4),
              (r'master|lacquer|cut', 0.2), (r'design|photo|artwork|sleeve', 0.1)]
def role_w(role):
    if not role:
        return 1.0
    r = role.lower(); best = 0.0
    for pat, w in ROLE_RULES:
        if re.search(pat, r):
            best = max(best, w)
    return best or 0.5

def load_token():
    env = {}
    for line in open(os.path.join(ROOT, '.env')):
        if '=' in line and not line.strip().startswith('#'):
            k, v = line.split('=', 1); env[k.strip()] = v.strip()
    return env.get('Discogs_Token') or os.environ.get('DISCOGS_TOKEN')

def enrich(rows, track_meta, ppl, edges, out_name, progress=None):
    import requests
    token = load_token()
    if not token:
        print('[enrich] no Discogs token; skipping enrichment'); return
    UA = 'SetlistGraph/1.0 +daniel.welsh@routiq.ai'
    H = {'User-Agent': UA, 'Authorization': f'Discogs token={token}'}
    cache_dir = os.path.join(ROOT, 'setlist_cache'); os.makedirs(cache_dir, exist_ok=True)
    sig = hashlib.md5(out_name.encode()).hexdigest()[:8]
    cache_path = os.path.join(cache_dir, f'{sig}_v4.jsonl')   # v3: + videos + release urls
    done = {}
    if os.path.exists(cache_path):
        for line in open(cache_path):
            try:
                o = json.loads(line); done[o['i']] = o
            except Exception:
                pass
    fh = open(cache_path, 'a')

    def api(url, params=None):
        for _ in range(4):
            r = requests.get(url, headers=H, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(int(r.headers.get('Retry-After', '60'))); continue
            if r.status_code >= 500:
                time.sleep(4); continue
            if r.status_code == 404:
                return None
            r.raise_for_status(); return r.json()
        return None

    for i, tm in enumerate(track_meta):
        if progress:
            progress('Matching tracks on Discogs', i + 1, len(track_meta))
        if i in done:
            continue
        primary = ppl.by_key[tm['keys'][0]]['name'] if tm['keys'] else tm['artist_raw']
        rec = {'i': i, 'label': None, 'genres': [], 'styles': [], 'release_id': None,
               'release_url': None, 'release_title': None, 'videos': [], 'credits': []}
        try:
            time.sleep(1.1)
            data = api('https://api.discogs.com/database/search',
                       {'artist': primary, 'track': tm['title'], 'type': 'release', 'per_page': 5})
            res = (data or {}).get('results') or []
            best = None
            pl = primary.lower()
            cand = [x for x in res if pl in (x.get('title') or '').lower()] or res
            cand.sort(key=lambda x: -(x.get('community') or {}).get('have', 0))
            best = cand[0] if cand else None
            if best:
                time.sleep(1.1)
                rel = api(f"https://api.discogs.com/releases/{best['id']}")
                if rel:
                    labs = [l.get('name') for l in (rel.get('labels') or []) if l.get('name')]
                    rec['label'] = labs[0] if labs else None
                    rec['genres'] = rel.get('genres') or []
                    rec['styles'] = rel.get('styles') or []
                    rec['release_id'] = best['id']
                    rec['release_url'] = f"https://www.discogs.com/release/{best['id']}"
                    rec['release_title'] = rel.get('title')
                    rec['year'] = rel.get('year')
                    rec['videos'] = [{'uri': v.get('uri'), 'title': v.get('title')}
                                     for v in (rel.get('videos') or []) if v.get('uri')][:5]
                    for a in (rel.get('extraartists') or []):
                        nm = (a.get('name') or '').strip()
                        if nm and nm.lower() not in ('various',):
                            rec['credits'].append({'name': re.sub(r'\s*\(\d+\)$', '', nm),
                                                   'role': a.get('role') or ''})
        except Exception as e:
            rec['error'] = f'{type(e).__name__}: {e}'
        fh.write(json.dumps(rec, ensure_ascii=False) + '\n'); fh.flush()
        done[i] = rec
        if (i + 1) % 10 == 0:
            print(f'[enrich] {i + 1}/{len(track_meta)}', flush=True)
    fh.close()

    # fold cache into graph
    label_artists = defaultdict(set)
    matched = 0
    for i, tm in enumerate(track_meta):
        o = done.get(i)
        if not o:
            continue
        if o.get('label') or o.get('credits'):
            matched += 1
        base_keys = list(tm['keys'])
        for c in o.get('credits', []):
            ck = ppl.touch(c['name'], in_set=False)
            if not ck:
                continue
            for bk in base_keys:
                edges[(min(bk, ck), max(bk, ck))] = max(edges[(min(bk, ck), max(bk, ck))], role_w(c['role']))
            base_keys.append(ck) if ck not in base_keys else None
        if o.get('label'):
            for k in base_keys:
                label_artists[norm_key(o['label'])].add(k)
    for lab, ks in label_artists.items():
        ks = list(ks)
        if 2 <= len(ks) <= 25:
            for i in range(len(ks)):
                for j in range(i + 1, len(ks)):
                    a, b = ks[i], ks[j]
                    edges[(min(a, b), max(a, b))] = max(edges[(min(a, b), max(a, b))], 0.45)
    print(f'[enrich] matched {matched}/{len(track_meta)} tracks; people now {len(ppl.by_key)}')

GENERIC_LABELS = {'not on label', 'white label', 'self-released', 'self released', 'unknown'}
# media / promo / store entries Discogs files under "label" but which aren't record labels
LABEL_BLOCK = {'resident advisor', 'fact magazine', 'fact', 'tsugi', 'cd pool', 'mixmag',
               'dj mag', 'clash', 'the wire', 'xlr8r', 'crack magazine', 'boiler room',
               'beatport', 'traxsource', 'juno', 'juno download', 'bandcamp', 'soundcloud',
               'spotify', 'youtube', 'discogs', 'nrk', 'bbc radio 1'}
def real_label(name):
    n = (name or '').strip().lower()
    if not n or n in GENERIC_LABELS or n in LABEL_BLOCK:
        return False
    if n.startswith('not on label'):
        return False
    if any(b in n for b in ('magazine', 'self-rel', 'promo only', 'white label', 'bootleg',
                            'podcast', 'radio show', 'mixtape', 'mixcloud')):
        return False
    return True
def deep_enrich(rows, track_meta, ppl, edges, out_name, progress=None):
    """Per-ARTIST harvest: resolve each set artist's Discogs discography, connect
    artists through shared LABELS (+ promote connective labels to hub nodes) and
    shared collaborators. This is what turns disconnected islands into a scene."""
    import requests
    token = load_token()
    if not token:
        print('[deep] no Discogs token; skipping'); return
    H = {'User-Agent': 'SetlistGraph/1.0 +daniel.welsh@routiq.ai',
         'Authorization': f'Discogs token={token}'}
    cache_dir = os.path.join(ROOT, 'setlist_cache'); os.makedirs(cache_dir, exist_ok=True)
    sig = hashlib.md5(out_name.encode()).hexdigest()[:8]
    cpath = os.path.join(cache_dir, f'{sig}_artists2.jsonl')   # artists2: + debut/latest year
    done = {}
    if os.path.exists(cpath):
        for line in open(cpath):
            try:
                o = json.loads(line); done[o['key']] = o
            except Exception:
                pass
    fh = open(cpath, 'a')

    def api(url, params=None):
        for _ in range(4):
            r = requests.get(url, headers=H, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(int(r.headers.get('Retry-After', '60'))); continue
            if r.status_code >= 500:
                time.sleep(4); continue
            if r.status_code == 404:
                return None
            r.raise_for_status(); return r.json()
        return None

    targets = [(k, p['name']) for k, p in list(ppl.by_key.items())
               if p.get('kind', 'person') == 'person' and p['in_set']]
    for n, (k, name) in enumerate(targets, 1):
        if progress:
            progress('Harvesting artist discographies', n, len(targets))
        if k in done:
            continue
        rec = {'key': k, 'name': name, 'id': None, 'labels': [], 'collabs': []}
        try:
            time.sleep(1.1)
            sr = api('https://api.discogs.com/database/search', {'q': name, 'type': 'artist', 'per_page': 5})
            res = (sr or {}).get('results') or []
            nk = norm_key(name)
            pick = next((x for x in res if norm_key(x.get('title', '')) == nk), None) or (res[0] if res else None)
            if pick and pick.get('id'):
                rec['id'] = pick['id']
                time.sleep(1.1)
                rel = api(f"https://api.discogs.com/artists/{pick['id']}/releases",
                          {'per_page': 100, 'sort': 'year', 'sort_order': 'desc'})
                labs, collabs, years = {}, {}, []
                for it in ((rel or {}).get('releases') or [])[:100]:
                    y = it.get('year')
                    if isinstance(y, int) and y > 1900:
                        years.append(y)
                    lab = (it.get('label') or '').strip()
                    if lab and real_label(lab):
                        labs[lab] = labs.get(lab, 0) + 1
                    art = (it.get('artist') or '').strip()
                    if art and 'various' not in art.lower():
                        for part in re.split(r'\s*(?:,|&|/|\bfeat\.?\b|\bft\.?\b|\bvs\.?\b)\s*', art):
                            part = part.strip()
                            if norm_key(part) and norm_key(part) != nk and len(norm_key(part)) > 2:
                                collabs[part] = collabs.get(part, 0) + 1
                rec['labels'] = sorted(labs, key=lambda x: -labs[x])[:14]
                rec['collabs'] = sorted(collabs, key=lambda x: -collabs[x])[:8]
                rec['debut'] = min(years) if years else None
                rec['latest'] = max(years) if years else None
        except Exception as e:
            rec['error'] = f'{type(e).__name__}: {e}'
        fh.write(json.dumps(rec, ensure_ascii=False) + '\n'); fh.flush()
        done[k] = rec
        if n % 10 == 0:
            print(f'[deep] {n}/{len(targets)} artists harvested', flush=True)
    fh.close()

    # shared LABELS -> hub nodes + artist<->artist edges
    label_artists = defaultdict(set)
    for k, _ in targets:
        o = done.get(k)
        if not o:
            continue
        ppl.by_key[k]['discogs_id'] = o.get('id')
        ppl.by_key[k]['debut'] = o.get('debut')
        ppl.by_key[k]['latest'] = o.get('latest')
        for lab in o.get('labels', []):
            if real_label(lab):
                label_artists[norm_key(lab)].add((k, lab))
    labels_kept = 0
    for lk, pairs in label_artists.items():
        arts = sorted({a for a, _ in pairs})
        if not (2 <= len(arts) <= 40):
            continue
        disp = sorted({lab for _, lab in pairs}, key=len)[0]
        lnode = ppl.touch(disp, in_set=False, kind='label')
        if not lnode:
            continue
        ppl.by_key[lnode]['appearances'] = len(arts)
        labels_kept += 1
        for a in arts:
            e = (min(a, lnode), max(a, lnode)); edges[e] = max(edges[e], 0.6)
        for i in range(len(arts)):
            for j in range(i + 1, len(arts)):
                e = (min(arts[i], arts[j]), max(arts[i], arts[j])); edges[e] = max(edges[e], 0.3)

    # collaborators shared by >=2 set artists -> bridge discovery nodes
    collab_artists = defaultdict(set)
    for k, _ in targets:
        o = done.get(k)
        if not o:
            continue
        for c in o.get('collabs', []):
            collab_artists[norm_key(c)].add((k, c))
    in_set_keys = {a for a, _ in targets}
    bridges = 0
    for ck, pairs in collab_artists.items():
        arts = sorted({a for a, _ in pairs})
        if len(arts) < 2 or ck in {norm_key(ppl.by_key[a]['name']) for a in arts}:
            continue
        disp = sorted({c for _, c in pairs}, key=len)[0]
        cnode = ppl.touch(disp, in_set=False, kind='person')
        if not cnode or cnode in in_set_keys:
            continue
        bridges += 1
        for a in arts:
            e = (min(a, cnode), max(a, cnode)); edges[e] = max(edges[e], 0.5)
    print(f'[deep] label hubs={labels_kept}  bridge collaborators={bridges}  '
          f'people={len(ppl.by_key)}  edges={len(edges)}')

def enrich_festival_tracks(ppl, edges, out_name, per_artist=3, progress=None):
    """Artist-only (festival) playlist harvest. For each in-set artist, pull their
    top `per_artist` releases (by community 'have') WITH videos, fold the release
    credits into the graph as edges, and synthesize the track_meta + the v4 release
    cache so the existing build_track_links / build_setlist / assign_genres all work
    unchanged. Returns the synthesized track_meta list (ordered to match v4 cache i).

    Resumable per ARTIST (a separate fest-cache); the v4 cache is rebuilt fresh each
    run from that, so track indices stay stable across rebuilds."""
    import requests
    token = load_token()
    cache_dir = os.path.join(ROOT, 'setlist_cache'); os.makedirs(cache_dir, exist_ok=True)
    sig = hashlib.md5(out_name.encode()).hexdigest()[:8]
    fest_path = os.path.join(cache_dir, f'{sig}_fest_v1.jsonl')   # per-artist chosen releases
    done = {}
    if os.path.exists(fest_path):
        for line in open(fest_path):
            try:
                o = json.loads(line); done[o['key']] = o
            except Exception:
                pass

    if token:
        H = {'User-Agent': 'SetlistGraph/1.0 +daniel.welsh@routiq.ai',
             'Authorization': f'Discogs token={token}'}

        def api(url, params=None):
            for _ in range(4):
                r = requests.get(url, headers=H, params=params, timeout=30)
                if r.status_code == 429:
                    time.sleep(int(r.headers.get('Retry-After', '60'))); continue
                if r.status_code >= 500:
                    time.sleep(4); continue
                if r.status_code == 404:
                    return None
                r.raise_for_status(); return r.json()
            return None

        targets = [(k, p['name']) for k, p in list(ppl.by_key.items())
                   if p.get('kind', 'person') == 'person' and p['in_set']]
        fh = open(fest_path, 'a')
        for n, (k, name) in enumerate(targets, 1):
            if progress:
                progress('Digging top tracks per artist', n, len(targets))
            if k in done:
                continue
            rec = {'key': k, 'name': name, 'releases': []}
            try:
                # prefer the artist id deep_enrich already resolved (robust to casing
                # like 'DJRUM'); the name-filtered release search is brittle.
                aid = ppl.by_key[k].get('discogs_id')
                if not aid:
                    time.sleep(1.1)
                    sr = api('https://api.discogs.com/database/search',
                             {'q': name, 'type': 'artist', 'per_page': 5})
                    sres = (sr or {}).get('results') or []
                    nk = norm_key(name)
                    pk = next((x for x in sres if norm_key(x.get('title', '')) == nk), None) \
                        or (sres[0] if sres else None)
                    aid = pk.get('id') if pk else None

                picks, seen_title = [], set()      # release ids to fetch; de-dupe by title
                if aid:
                    time.sleep(1.1)
                    al = api(f"https://api.discogs.com/artists/{aid}/releases",
                             {'per_page': 75, 'sort': 'year', 'sort_order': 'desc'})
                    items = [it for it in ((al or {}).get('releases') or [])
                             if (it.get('role') or '') == 'Main' and it.get('title')]

                    def pop(it):  # popularity proxy: community collection count, else recency
                        return ((it.get('stats') or {}).get('community') or {}).get('in_collection', 0) or 0
                    items.sort(key=lambda it: (-pop(it), -(it.get('year') or 0)))
                    for it in items:
                        tk = norm_key(it.get('title', ''))
                        if tk in seen_title:
                            continue
                        seen_title.add(tk)
                        picks.append(it.get('main_release') or it.get('id'))  # master -> main release
                        if len(picks) >= per_artist:
                            break
                else:   # fallback: free-text release search, most-collected first
                    time.sleep(1.1)
                    data = api('https://api.discogs.com/database/search',
                               {'q': name, 'type': 'release', 'per_page': 30})
                    res = [x for x in ((data or {}).get('results') or []) if x.get('id')]
                    res.sort(key=lambda x: -((x.get('community') or {}).get('have', 0)))
                    for x in res:
                        tk = norm_key(x.get('title', ''))
                        if tk in seen_title:
                            continue
                        seen_title.add(tk); picks.append(x['id'])
                        if len(picks) >= per_artist:
                            break

                for rid in picks:
                    time.sleep(1.1)
                    rel = api(f"https://api.discogs.com/releases/{rid}")
                    if not rel:
                        continue
                    labs = [l.get('name') for l in (rel.get('labels') or []) if l.get('name')]
                    credits = []
                    for a in (rel.get('extraartists') or []):
                        nm = (a.get('name') or '').strip()
                        if nm and nm.lower() not in ('various',):
                            credits.append({'name': re.sub(r'\s*\(\d+\)$', '', nm),
                                            'role': a.get('role') or ''})
                    rec['releases'].append({
                        'release_id': rid,
                        'release_url': f"https://www.discogs.com/release/{rid}",
                        'release_title': rel.get('title'),
                        'year': rel.get('year'),
                        'label': labs[0] if labs else None,
                        'genres': rel.get('genres') or [],
                        'styles': rel.get('styles') or [],
                        'videos': [{'uri': v.get('uri'), 'title': v.get('title')}
                                   for v in (rel.get('videos') or []) if v.get('uri')][:5],
                        'credits': credits,
                    })
            except Exception as e:
                rec['error'] = f'{type(e).__name__}: {e}'
            fh.write(json.dumps(rec, ensure_ascii=False) + '\n'); fh.flush()
            done[k] = rec
            if n % 10 == 0:
                print(f'[fest] {n}/{len(targets)} artists dug', flush=True)
        fh.close()
    else:
        print('[fest] no Discogs token; playlist will be empty')

    # synthesize track_meta + the v4 release cache (rebuilt fresh, stable artist order)
    v4_path = os.path.join(cache_dir, f'{sig}_v4.jsonl')
    track_meta = []
    # snapshot the artist set BEFORE the loop — credit folding adds new nodes to ppl
    artist_keys = [(k, p) for k, p in ppl.by_key.items()
                   if p.get('kind', 'person') == 'person' and p['in_set']]
    with open(v4_path, 'w') as fh:
        i = 0
        for k, p in artist_keys:
            o = done.get(k) or {}
            for rel in o.get('releases', []):
                track_meta.append({'title': rel.get('release_title') or '', 'keys': [k],
                                   'genre': 'Other', 'album': '',
                                   'artist_raw': p['name'], 'date': str(rel.get('year') or '')})
                fh.write(json.dumps({'i': i, **rel, 'genres': rel.get('genres', []),
                                     'styles': rel.get('styles', [])}, ensure_ascii=False) + '\n')
                i += 1
                # fold the release's real co-credits into the graph as edges
                for c in rel.get('credits', []):
                    ck = ppl.touch(c['name'], in_set=False)
                    if ck and ck != k:
                        e = (min(k, ck), max(k, ck))
                        edges[e] = max(edges[e], role_w(c['role']))
    print(f'[fest] synthesized {len(track_meta)} playlist tracks from '
          f'{sum(1 for _ in done)} artists')
    return track_meta

# ----------------------------------------------------------------- genres + layout
def assign_genres(ppl, edges, track_meta, out_name):
    """Assign each artist a Discogs style (from matched releases); labels & discovered
    nodes inherit the dominant genre of their connected in-set artists."""
    from collections import Counter
    cache_dir = os.path.join(ROOT, 'setlist_cache')
    sig = hashlib.md5(out_name.encode()).hexdigest()[:8]
    cpath = os.path.join(cache_dir, f'{sig}_v4.jsonl')
    if not os.path.exists(cpath):
        return
    by_i = {}
    for line in open(cpath):
        try:
            o = json.loads(line); by_i[o['i']] = o
        except Exception:
            pass
    votes = defaultdict(Counter)
    for i, tm in enumerate(track_meta):
        o = by_i.get(i)
        if not o:
            continue
        st = style_bucket(o.get('styles'), o.get('genres'))
        if st == 'Other':
            continue
        for k in tm['keys']:
            votes[k][st] += 1
    for k, c in votes.items():
        if k in ppl.by_key and c:
            ppl.by_key[k]['genres'] = {c.most_common(1)[0][0]}
    nbr = defaultdict(list)
    for (a, b) in edges:
        nbr[a].append(b); nbr[b].append(a)
    def dominant(k):
        cc = Counter()
        for m in nbr.get(k, []):
            p = ppl.by_key.get(m)
            if p and p.get('in_set') and p.get('kind', 'person') == 'person' and p['genres']:
                g = sorted(p['genres'])[0]
                if g and g != 'Other':
                    cc[g] += 1
        return cc.most_common(1)[0][0] if cc else None
    for k, p in ppl.by_key.items():
        if p.get('kind') == 'label' or not p.get('in_set'):
            g = dominant(k)
            if g:
                p['genres'] = {g}

def compute_layout(g):
    """Two worlds: ARTISTS (left) force-laid; LABELS (right) ranked beside their artists.
    The cross-world edges form the bridge between artist-world and label-world."""
    art = [n for n, d in g.nodes(data=True) if d.get('role') != 'label']
    lab = [n for n, d in g.nodes(data=True) if d.get('role') == 'label']
    sub = g.subgraph(art)
    try:
        pos = nx.spring_layout(sub, weight='weight', seed=42,
                               k=2.6 / (max(len(art), 1) ** 0.5), iterations=170)
    except Exception:
        pos = nx.spring_layout(sub, seed=42)
    xs = [p[0] for p in pos.values()] or [0.0]
    ys = [p[1] for p in pos.values()] or [0.0]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    sx = (maxx - minx) or 1.0
    sy = (maxy - miny) or 1.0
    S = 400.0   # ipysigma wants {node: {'x':,'y':}}; scale up for clean spacing
    L = {}
    for n in art:
        x, y = pos[n]
        L[n] = {'x': S * (-1.75 + 1.50 * (x - minx) / sx),   # wide artist world
                'y': S * (-1.15 + 2.30 * (y - miny) / sy)}
    lab_y = {}
    for n in lab:
        ns = [m for m in g.neighbors(n) if m in L]
        lab_y[n] = sum(L[m]['y'] for m in ns) / len(ns) if ns else 0.0
    order = sorted(lab, key=lambda n: lab_y[n])
    m = len(order)
    for i, n in enumerate(order):
        yy = -1.08 + (2.16 * i / (m - 1) if m > 1 else 0.0)   # taller label column
        xx = 0.55 + (0.26 if i % 2 else 0.0)                  # label world, clear gap to the right
        L[n] = {'x': S * xx, 'y': S * yy}
    return L

def build_track_links(track_meta, ppl, out_name):
    """Map each node -> its tracks in the set, with Discogs release URL + a YouTube uri
    (from Discogs' community videos) so the UI can link out and play."""
    cache_dir = os.path.join(ROOT, 'setlist_cache')
    sig = hashlib.md5(out_name.encode()).hexdigest()[:8]
    cpath = os.path.join(cache_dir, f'{sig}_v4.jsonl')
    by_i = {}
    if os.path.exists(cpath):
        for line in open(cpath):
            try:
                o = json.loads(line); by_i[o['i']] = o
            except Exception:
                pass
    out = defaultdict(list)
    label_tracks = defaultdict(list)
    for i, tm in enumerate(track_meta):
        o = by_i.get(i) or {}
        yt = next((v['uri'] for v in (o.get('videos') or []) if v.get('uri')), None)
        entry = {'title': tm['title'], 'artist': tm.get('artist_raw', ''),
                 'discogs': o.get('release_url'), 'youtube': yt,
                 'has_video': bool(yt)}
        for k in tm['keys']:
            out[k].append(entry)
        if o.get('label'):
            label_tracks[norm_key(o['label'])].append(entry)
    for key, p in ppl.by_key.items():
        if p.get('kind') == 'label':
            lt = label_tracks.get(norm_key(p['name']))
            if lt:
                out[key] = lt
    return out


def build_setlist(track_meta, out_name):
    """The whole set as an ordered playlist: [{title, artist, youtube, discogs, node}]."""
    cache_dir = os.path.join(ROOT, 'setlist_cache')
    sig = hashlib.md5(out_name.encode()).hexdigest()[:8]
    cpath = os.path.join(cache_dir, f'{sig}_v4.jsonl')
    by_i = {}
    if os.path.exists(cpath):
        for line in open(cpath):
            try:
                o = json.loads(line); by_i[o['i']] = o
            except Exception:
                pass
    out = []
    for i, tm in enumerate(track_meta):
        o = by_i.get(i) or {}
        yt = next((v['uri'] for v in (o.get('videos') or []) if v.get('uri')), None)
        out.append({'title': tm['title'], 'artist': tm.get('artist_raw', ''), 'youtube': yt,
                    'discogs': o.get('release_url'), 'node': (tm['keys'][0] if tm.get('keys') else None),
                    'year': o.get('year') or None, 'genre': style_bucket(o.get('styles'), o.get('genres'))})
    return out


# ----------------------------------------------------------------- scoring + render
def score_and_render(ppl, edges, out_name, render=True):
    # finalize display names
    for p in ppl.by_key.values():
        if p['name_votes']:
            p['name'] = max(p['name_votes'], key=p['name_votes'].get)
        p['genre'] = sorted(p['genres'])[0] if p['genres'] else 'Other'
        if p.get('kind') == 'label':
            p['role'] = 'label'
        elif not p['in_set']:
            p['role'] = 'discovered'
        elif p['is_remixer'] and not p['appearances']:
            p['role'] = 'remixer'
        else:
            p['role'] = 'artist'
    keys = list(ppl.by_key)
    idx = {k: i for i, k in enumerate(keys)}
    G = ig.Graph(n=len(keys), directed=False)
    elist = [(idx[a], idx[b]) for (a, b) in edges]
    G.add_edges(elist); G.es['weight'] = [edges[e] for e in edges]
    # pagerank seed: set artists anchor resonance; labels/discovery downweighted
    def seed_val(p):
        if p.get('kind') == 'label':
            return 0.1
        if not p['in_set']:
            return 0.3
        return 1.0 + 2.0 * p['appearances']
    seed = [seed_val(ppl.by_key[k]) for k in keys]
    ppr = G.personalized_pagerank(reset=seed, weights='weight', damping=0.82) if G.ecount() else seed
    try:
        part = la.find_partition(G, la.RBConfigurationVertexPartition, weights='weight',
                                 resolution_parameter=1.0, seed=42)
        member = part.membership
    except Exception:
        member = list(range(len(keys)))
    mx = max(ppr) or 1.0
    g = nx.Graph()
    for k in keys:
        p = ppl.by_key[k]; i = idx[k]
        g.add_node(k, label=p['name'], resonance=round(100 * ppr[i] / mx, 2),
                   appearances=p['appearances'], cluster=int(member[i]),
                   genre=p['genre'], role=p['role'], discogs_id=p.get('discogs_id'),
                   debut=p.get('debut'), latest=p.get('latest'),
                   size=2 + 3 * p['appearances'] + (4 if p['role'] == 'artist' else (3 if p['role'] == 'label' else 0)))
    for (a, b), w in edges.items():
        g.add_edge(a, b, weight=round(w, 3))

    for n, d in g.nodes(data=True):
        d['resonance'] = max(d['resonance'], 1.0)
    if render:
        positions = compute_layout(g)
        out_html = os.path.join(ROOT, f'{out_name}.html')
        Sigma.write_html(
            g, out_html, fullscreen=True, height=900,
            layout=positions, start_layout=0,
            node_size='size', node_size_range=(4, 30), node_size_scale='lin',
            node_color='genre', node_color_palette=GENRE_PALETTE, node_label='label',
            node_border_color='role',
            node_border_color_palette={'artist': '#ffffff', 'remixer': '#f2c14e',
                                       'discovered': '#7aa2f7', 'label': '#ff5da2'},
            default_node_border_ratio=0.22,
            edge_weight='weight', edge_size_range=(0.3, 3), default_edge_color='#cccccc33',
            hide_edges_on_move=True, label_density=2, node_metrics=[],
        )
        with open(os.path.join(ROOT, f'{out_name}.json'), 'w') as f:
            from networkx.readwrite import json_graph
            json.dump(json_graph.node_link_data(g), f)
    return g

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('setlist')
    ap.add_argument('--out', default='setlist_graph')
    ap.add_argument('--enrich', action='store_true')
    ap.add_argument('--deep', action='store_true', help='per-artist label+collab harvest (denser)')
    a = ap.parse_args()
    rows = read_table(a.setlist)
    ppl, edges, tm = build_from_setlist(rows)
    print(f'parsed {len(rows)} tracks -> {len(ppl.by_key)} people, {len(edges)} edges (set-internal)')
    if a.enrich or a.deep:
        enrich(rows, tm, ppl, edges, a.out)
    if a.deep:
        deep_enrich(rows, tm, ppl, edges, a.out)
    if a.enrich or a.deep:
        assign_genres(ppl, edges, tm, a.out)
    g = score_and_render(ppl, edges, a.out)
    print(f'rendered {a.out}.html  ({g.number_of_nodes()} nodes, {g.number_of_edges()} edges)')
    top = sorted(g.nodes(data=True), key=lambda x: -x[1]['resonance'])[:15]
    print('\n=== top by resonance ===')
    for n, d in top:
        tag = {'artist': '  ', 'remixer': '🎛 ', 'discovered': '✨', 'label': '🏷 '}[d['role']]
        print(f"  {tag} {d['resonance']:6.1f}  c{d['cluster']:<2} {d['label']}  ({d['appearances']}x, {d['genre']})")
    disc = [d for _, d in g.nodes(data=True) if d['role'] == 'discovered']
    if disc:
        print(f"\n=== discovery (not in set), top 10 ===")
        for d in sorted(disc, key=lambda x: -x['resonance'])[:10]:
            print(f"  ✨ {d['resonance']:6.1f}  {d['label']}")

if __name__ == '__main__':
    main()
