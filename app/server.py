#!/usr/bin/env python3
"""Setlist -> graph web app. Wraps setlist_graph.py in a FastAPI server with
live (SSE) progress. Run:  python3 app/server.py   then open http://127.0.0.1:8765
"""
import os, sys, json, uuid, time, threading, tempfile, traceback, hashlib, subprocess
from collections import defaultdict
APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(APP_DIR))          # import setlist_graph from parent
import setlist_graph as slg
from tracklist_resolve import resolve_url
from lineup_parse import parse_lineup_image, acts_to_setlist
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from pydantic import BaseModel
from typing import Optional, List

app = FastAPI(title="Setlist Graph")
JOBS = {}


def load_env_key(name):
    """Read a key from the repo .env (falls back to the process env)."""
    path = os.path.join(os.path.dirname(APP_DIR), '.env')
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            if '=' in line and not line.strip().startswith('#'):
                k, v = line.split('=', 1)
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")  # tolerate quoted values
    return os.environ.get(name)

ROLE_BORDER = {'artist': '#ffffff', 'remixer': '#f2c14e', 'discovered': '#7aa2f7', 'label': '#ff5da2'}
PRESETS = [
    {'id': 'klymax', 'name': 'Klymax 01 (yours)', 'file': 'presets/klymax.txt'},
    {'id': 'moopie', 'name': 'moopie · Panorama Bar', 'file': 'presets/moopie.txt'},
]


def normalize_setlist(text):
    """Accept a header'd TSV, or loose 'Artist <tab> Title' / 'Artist - Title' lines."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return ''
    first = lines[0].lower()
    if 'artist' in first and ('title' in first or 'track' in first):
        return text
    out = ['Artist\tTrack Title']
    for l in lines:
        if '\t' in l:
            a, _, t = l.partition('\t')
        elif ' - ' in l:
            a, _, t = l.partition(' - ')
        elif ' – ' in l:
            a, _, t = l.partition(' – ')
        else:
            a, t = l, ''
        out.append(f'{a.strip()}\t{t.strip()}')
    return '\n'.join(out)


def serialize(g, positions, track_links=None, tracklist=None):
    track_links = track_links or {}
    nodes, edges = [], []
    for n, d in g.nodes(data=True):
        p = positions.get(n, {'x': 0, 'y': 0})
        nodes.append({
            'id': str(n), 'label': d.get('label', str(n)),
            'x': round(p['x'], 1), 'y': round(p['y'], 1),
            'size': d.get('size', 4), 'genre': d.get('genre', 'Other'),
            'role': d.get('role', 'artist'), 'resonance': d.get('resonance', 1.0),
            'appearances': d.get('appearances', 0), 'cluster': d.get('cluster', 0),
            'discogs_id': d.get('discogs_id'), 'debut': d.get('debut'), 'latest': d.get('latest'),
            'tracks': track_links.get(n, []),
            'color': slg.GENRE_PALETTE.get(d.get('genre', 'Other'), '#8893a8'),
            'border': ROLE_BORDER.get(d.get('role', 'artist'), '#ffffff'),
        })
    for a, b, dd in g.edges(data=True):
        edges.append({'source': str(a), 'target': str(b), 'weight': dd.get('weight', 0.3)})

    def top(pred, k=14):
        items = sorted((d for _, d in g.nodes(data=True) if pred(d)),
                       key=lambda d: -d.get('resonance', 0))
        return [{'label': d['label'], 'genre': d['genre'],
                 'resonance': d['resonance'], 'role': d['role']} for d in items[:k]]

    summary = {
        'artists': top(lambda d: d['role'] in ('artist', 'remixer') and d.get('appearances', 0) > 0),
        'discoveries': top(lambda d: d['role'] == 'discovered'),
        'labels': top(lambda d: d['role'] == 'label'),
    }
    return {'nodes': nodes, 'edges': edges, 'summary': summary,
            'palette': slg.GENRE_PALETTE, 'tracklist': tracklist or [],
            'stats': {'nodes': g.number_of_nodes(), 'edges': g.number_of_edges()}}


def run_build(job_id, text, deep):
    job = JOBS[job_id]

    def progress(stage, i, total):
        job['progress'] = {'stage': stage, 'i': i, 'total': total}

    try:
        progress('Parsing setlist', 0, 1)
        norm = normalize_setlist(text)
        tf = tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False, encoding='utf-8')
        tf.write(norm); tf.close()
        rows = slg.read_table(tf.name)
        os.unlink(tf.name)
        if not rows:
            raise ValueError('No tracks parsed. Use one track per line: Artist <tab> Title.')
        ppl, edges, tm = slg.build_from_setlist(rows)
        out_name = 'app_' + hashlib.md5(norm.encode()).hexdigest()[:10]  # content-addressed cache
        if deep:
            slg.enrich(rows, tm, ppl, edges, out_name, progress=progress)
            slg.deep_enrich(rows, tm, ppl, edges, out_name, progress=progress)
            slg.assign_genres(ppl, edges, tm, out_name)
        progress('Scoring & laying out the two worlds', 1, 1)
        g = slg.score_and_render(ppl, edges, out_name, render=False)
        positions = slg.compute_layout(g)
        track_links = slg.build_track_links(tm, ppl, out_name)
        tracklist = slg.build_setlist(tm, out_name)
        job['graph'] = serialize(g, positions, track_links, tracklist)
        job['tracks'] = len(rows)
        job['status'] = 'done'
    except Exception as e:
        traceback.print_exc()
        job['status'] = 'error'
        job['error'] = f'{type(e).__name__}: {e}'


def run_festival_build(job_id, acts):
    """Festival lineup -> artist graph + top-3-per-artist playlist. Each act is one
    artist (no track titles), so the graph leans on the per-artist label+collab
    harvest, and the playlist on each artist's most-collected releases."""
    job = JOBS[job_id]

    def progress(stage, i, total):
        job['progress'] = {'stage': stage, 'i': i, 'total': total}

    try:
        progress('Reading lineup', 0, 1)
        names, seen = [], set()
        for a in acts:
            nm = (a.get('artist') or '').strip()
            if nm and nm.lower() not in seen:
                seen.add(nm.lower()); names.append(nm)
        if not names:
            raise ValueError('No artists found in the lineup.')
        rows = [{'artist': nm, 'title': '', 'album': '', 'genre': '', 'date': ''} for nm in names]
        ppl, edges, _tm = slg.build_from_setlist(rows)
        key = '||'.join(sorted(n.lower() for n in names))
        out_name = 'fest_' + hashlib.md5(key.encode()).hexdigest()[:10]   # content-addressed cache
        slg.deep_enrich(rows, _tm, ppl, edges, out_name, progress=progress)
        track_meta = slg.enrich_festival_tracks(ppl, edges, out_name, per_artist=3, progress=progress)
        slg.assign_genres(ppl, edges, track_meta, out_name)
        progress('Scoring & laying out the lineup', 1, 1)
        g = slg.score_and_render(ppl, edges, out_name, render=False)
        positions = slg.compute_layout(g)
        track_links = slg.build_track_links(track_meta, ppl, out_name)
        tracklist = slg.build_setlist(track_meta, out_name)
        data = serialize(g, positions, track_links, tracklist)

        # stamp each node with its festival slot(s): stage / day / time / live
        slots = defaultdict(list)
        for a in acts:
            parts, _ = slg.split_artists(a.get('artist') or '')
            for part in parts:
                slots[slg.norm_key(part)].append(
                    {'stage': a.get('stage'), 'day': a.get('day'), 'start': a.get('start'),
                     'end': a.get('end'), 'live': bool(a.get('live'))})
        for nd in data['nodes']:
            s = slots.get(slg.norm_key(nd['label']))
            if s:
                nd['slots'] = s
        data['festival'] = True
        job['graph'] = data
        job['tracks'] = len(names)
        job['status'] = 'done'
    except Exception as e:
        traceback.print_exc()
        job['status'] = 'error'
        job['error'] = f'{type(e).__name__}: {e}'


# ---- Shazam a mix (isolated venv: shazamio needs pydantic v1, FastAPI needs v2) ----
SHAZAM_PY = os.path.join(APP_DIR, '.venv_shazam', 'bin', 'python')
SHAZAM_SCRIPT = os.path.join(APP_DIR, 'shazam_mix.py')


def _shazam_subprocess(url, progress):
    """Run the audio-fingerprint pipeline in its isolated venv; stream JSON lines."""
    if not os.path.exists(SHAZAM_PY):
        raise RuntimeError('Audio recognition engine is not installed (app/.venv_shazam).')
    p = subprocess.Popen([SHAZAM_PY, SHAZAM_SCRIPT, url],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    tracks, err = None, None
    for line in p.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue                                 # ignore any non-JSON noise
        t = msg.get('t')
        if t == 'progress':
            progress(msg.get('stage', 'Working'), msg.get('i', 0), msg.get('total', 1))
        elif t == 'done':
            tracks = msg.get('tracks')
        elif t == 'error':
            err = msg.get('message')
    p.wait()
    if err:
        raise ValueError(err)
    if not tracks:
        tail = (p.stderr.read() or '').strip().splitlines()[-1:] if p.stderr else ['']
        raise ValueError('Audio recognition failed. ' + (tail[0] if tail else ''))
    return tracks


def run_shazam(job_id, url):
    job = JOBS[job_id]

    def progress(stage, i, total):
        job['progress'] = {'stage': stage, 'i': i, 'total': total}

    try:
        progress('Reading the description', 0, 1)
        # fast path: many mixes list the tracklist in the SoundCloud description
        try:
            from tracklist_resolve import _soundcloud_oembed, parse_desc_tracklist
            desc = parse_desc_tracklist(_soundcloud_oembed(url).get('description', ''))
        except Exception:
            desc = []
        if len(desc) >= 3:
            job['tracks'] = '\n'.join(desc)
        else:
            job['tracks'] = _shazam_subprocess(url, progress)   # slow path: fingerprint the audio
        job['status'] = 'done'
    except Exception as e:
        job['status'] = 'error'
        job['error'] = f'{type(e).__name__}: {e}'


class BuildReq(BaseModel):
    setlist: str = ''
    deep: bool = True
    lineup: Optional[List[dict]] = None    # festival mode: [{artist, stage, day, start, end, live}]


@app.post('/api/build')
def build(req: BuildReq):
    jid = uuid.uuid4().hex
    JOBS[jid] = {'status': 'running', 'progress': None, 'graph': None}
    if req.lineup:
        threading.Thread(target=run_festival_build, args=(jid, req.lineup), daemon=True).start()
    else:
        threading.Thread(target=run_build, args=(jid, req.setlist, req.deep), daemon=True).start()
    return {'job_id': jid}


@app.post('/api/lineup')
async def lineup(file: UploadFile = File(...)):
    """Read a festival blokkenschema image into structured acts + a unique-artist
    setlist. The browser drops the setlist into the box and passes `acts` back to
    /api/build for a festival graph."""
    data = await file.read()
    if not data:
        return JSONResponse({'error': 'empty', 'message': 'No image received.'}, status_code=400)
    try:
        res = parse_lineup_image(data, file.filename or '')
    except Exception as e:
        return JSONResponse({'error': 'parse_failed', 'message': f'{type(e).__name__}: {e}'},
                            status_code=400)
    if not res.get('acts'):
        return JSONResponse({'error': 'no_acts', 'message': 'No acts found in that image.'},
                            status_code=422)
    return {'festival': res.get('festival'), 'acts': res['acts'],
            'setlist': acts_to_setlist(res['acts']), 'count': len(res['acts'])}


@app.get('/api/progress/{jid}')
def progress(jid: str):
    def gen():
        last = None
        while True:
            job = JOBS.get(jid)
            if not job:
                yield f'data: {json.dumps({"status": "error", "error": "unknown job"})}\n\n'
                return
            payload = {'status': job['status'], 'progress': job.get('progress'),
                       'error': job.get('error')}
            s = json.dumps(payload)
            if s != last:
                yield f'data: {s}\n\n'
                last = s
            if job['status'] in ('done', 'error'):
                return
            time.sleep(0.4)
    return StreamingResponse(gen(), media_type='text/event-stream')


@app.get('/api/result/{jid}')
def result(jid: str):
    job = JOBS.get(jid)
    if not job or job['status'] != 'done':
        return JSONResponse({'error': 'not ready'}, status_code=404)
    return {'graph': job['graph'], 'tracks': job.get('tracks')}


STORY_SYSTEM = (
    "You are a music writer with a deep ear for electronic music — house, techno, "
    "disco, ambient, the lineages between them. You write the kind of liner note that "
    "makes a digger nod: specific, warm, unpretentious, never breathless. You read a DJ "
    "set the way a critic reads an album — the journey, the juxtapositions, what the "
    "selector is telling you through the sequence, the eras and labels they trust.\n\n"
    "Write 2 short paragraphs (about 110-150 words total) interpreting THIS set. Ground "
    "every claim in the data you are given — genres, the opening/middle/closing feel, the "
    "year span, the labels and the names. You may reference specific tracks or transitions "
    "from the tracklist when it sharpens the read. Do not invent facts, do not list, do not "
    "use markdown or headers or bullet points — just flowing prose. Address the set itself, "
    "not the reader. No preamble like 'This set' every sentence; vary it. End on the feeling "
    "it leaves."
)


FESTIVAL_SYSTEM = (
    "You are a music writer with a deep ear for electronic music — house, techno, "
    "disco, ambient, the lineages between them. You write the kind of festival preview "
    "that makes a digger nod: specific, warm, unpretentious, never breathless. You read "
    "a LINEUP the way a critic reads a label's catalogue — what the bookers value, the "
    "scenes and labels they trust, the throughlines between the names, the balance of "
    "heavy-hitters and genuine deep cuts.\n\n"
    "Write 2 short paragraphs (about 120-160 words total) interpreting THIS festival's "
    "curation. Ground every claim in the data — the dominant labels, the genre balance, "
    "the recurring scenes, the key names. Name the booking DNA (which labels/scenes this "
    "lineup leans into) and call out a couple of the more obscure or adventurous picks. "
    "Do not invent facts, do not list, no markdown or headers or bullets — just flowing "
    "prose. Address the lineup itself, not the reader. Vary your sentence openings. End "
    "on what kind of weekend this promises."
)


class StoryReq(BaseModel):
    facts: dict
    mode: str = 'set'    # 'set' (a DJ set) or 'festival' (a whole lineup)


def festival_prompt(f):
    lines = [f"A festival lineup of {f.get('total', '?')} artists across "
             f"{f.get('stages', '?')} stages."]
    g = f.get('genres') or []
    if g:
        lines.append("Genre balance (artist counts): " + ", ".join(f"{n} ({c})" for n, c in g))
    if f.get('labels'):
        lines.append("Dominant labels (booking DNA): " + ", ".join(f['labels']))
    if f.get('scenes'):
        lines.append("Recurring scenes: " + "; ".join(f['scenes']))
    if f.get('artists'):
        lines.append("Central names: " + ", ".join(f['artists']))
    if f.get('deepcuts'):
        lines.append("More obscure / peripheral bookings: " + ", ".join(f['deepcuts']))
    return "\n".join(lines)


def story_prompt(f):
    lines = [f"A {f.get('total', '?')}-track DJ set."]
    g = f.get('genres') or []
    if g:
        lines.append("Genre mix (track counts): " + ", ".join(f"{n} ({c})" for n, c in g))
    if f.get('open') or f.get('close'):
        lines.append(f"Arc — opens in {f.get('open') or '?'}, "
                     f"middle leans {f.get('mid') or '?'}, closes in {f.get('close') or '?'}.")
    if f.get('ymin') and f.get('ymax'):
        lines.append(f"Years span {f['ymin']}–{f['ymax']} "
                     f"({f.get('older', 0)} tracks pre-2005, {f.get('dated', 0)} dated).")
    if f.get('labels'):
        lines.append("Labels in play: " + ", ".join(f['labels']))
    if f.get('artists'):
        lines.append("Key names: " + ", ".join(f['artists']))
    tl = f.get('tracks') or []
    if tl:
        seq = "\n".join(
            f"{i+1}. {t.get('artist','')} — {t.get('title','')}"
            f"{(' ('+str(t['year'])+')') if t.get('year') else ''}"
            f"{(' ['+t['genre']+']') if t.get('genre') and t['genre']!='Other' else ''}"
            for i, t in enumerate(tl[:40]))
        lines.append("Tracklist in play order:\n" + seq)
    return "\n".join(lines)


@app.post('/api/story')
def story(req: StoryReq):
    key = load_env_key('ANTHROPIC_API_KEY')
    if not key:
        return JSONResponse(
            {'error': 'no_key',
             'message': 'Add ANTHROPIC_API_KEY to your .env to enable the AI read.'},
            status_code=400)
    try:
        import anthropic
    except ImportError:
        return JSONResponse(
            {'error': 'no_sdk', 'message': 'Run: pip install anthropic'}, status_code=400)

    client = anthropic.Anthropic(api_key=key)
    festival = (req.mode == 'festival')
    prompt = festival_prompt(req.facts) if festival else story_prompt(req.facts)
    system = FESTIVAL_SYSTEM if festival else STORY_SYSTEM

    def gen():
        try:
            with client.messages.stream(
                model='claude-opus-4-8',
                max_tokens=600,
                system=system,
                messages=[{'role': 'user', 'content': prompt}],
            ) as stream:
                for text in stream.text_stream:
                    yield text
        except Exception as e:
            yield f'\n\n[story unavailable: {type(e).__name__}]'

    return StreamingResponse(gen(), media_type='text/plain; charset=utf-8')


@app.get('/api/presets')
def presets():
    out = []
    for p in PRESETS:
        path = os.path.join(APP_DIR, p['file'])
        n = max(0, sum(1 for _ in open(path)) - 1) if os.path.exists(path) else 0
        out.append({'id': p['id'], 'name': p['name'], 'tracks': n})
    return out


@app.get('/api/lineup_preset/{pid}')
def lineup_preset(pid: str):
    """A baked-in festival lineup (acts with stage/day/time) for one-click demos
    and ?demo= deep-links."""
    safe = ''.join(c for c in pid if c.isalnum() or c in '-_').replace('-', '_')
    path = os.path.join(APP_DIR, 'presets', safe + '.json')
    if not os.path.exists(path):
        return JSONResponse({'error': 'not found'}, status_code=404)
    d = json.load(open(path, encoding='utf-8'))
    return {'name': d.get('name'), 'festival': d.get('festival'),
            'acts': d.get('acts') or [], 'setlist': d.get('setlist') or ''}


@app.get('/api/preset/{pid}')
def preset(pid: str):
    p = next((x for x in PRESETS if x['id'] == pid), None)
    if not p:
        return JSONResponse({'error': 'unknown preset'}, status_code=404)
    return {'content': open(os.path.join(APP_DIR, p['file']), encoding='utf-8').read()}


# ---- saved sets (the library: a 1001tracklists-style store of built mixes) ----
SAVED_DIR = os.path.join(APP_DIR, 'saved')


def _slug(s):
    out = ''.join(c if c.isalnum() else '-' for c in (s or 'set').strip().lower())
    return '-'.join(filter(None, out.split('-')))[:40] or 'set'


def _safe_sid(sid):
    return bool(sid) and all(c.isalnum() or c == '-' for c in sid)


class SaveReq(BaseModel):
    name: str
    setlist: str


@app.post('/api/save')
def save_set(req: SaveReq):
    setlist = (req.setlist or '').strip()
    if not setlist:
        return JSONResponse({'error': 'empty', 'message': 'Nothing to save.'}, status_code=400)
    os.makedirs(SAVED_DIR, exist_ok=True)
    sid = _slug(req.name) + '-' + uuid.uuid4().hex[:6]
    rec = {'id': sid, 'name': (req.name or 'Untitled set').strip()[:80],
           'setlist': setlist, 'count': sum(1 for l in setlist.splitlines() if l.strip()),
           'created': time.time()}
    with open(os.path.join(SAVED_DIR, sid + '.json'), 'w', encoding='utf-8') as f:
        json.dump(rec, f)
    return {'id': sid, 'name': rec['name'], 'count': rec['count']}


@app.get('/api/saved')
def saved_list():
    out = []
    if os.path.isdir(SAVED_DIR):
        for fn in os.listdir(SAVED_DIR):
            if not fn.endswith('.json'):
                continue
            try:
                r = json.load(open(os.path.join(SAVED_DIR, fn), encoding='utf-8'))
                out.append({'id': r['id'], 'name': r.get('name', 'Untitled'),
                            'count': r.get('count', 0), 'created': r.get('created', 0)})
            except Exception:
                pass
    out.sort(key=lambda x: -x.get('created', 0))
    return out


@app.get('/api/saved/{sid}')
def saved_get(sid: str):
    if not _safe_sid(sid):
        return JSONResponse({'error': 'bad id'}, status_code=400)
    path = os.path.join(SAVED_DIR, sid + '.json')
    if not os.path.exists(path):
        return JSONResponse({'error': 'not found'}, status_code=404)
    r = json.load(open(path, encoding='utf-8'))
    return {'content': r.get('setlist', ''), 'name': r.get('name', '')}


@app.delete('/api/saved/{sid}')
def saved_delete(sid: str):
    if not _safe_sid(sid):
        return JSONResponse({'error': 'bad id'}, status_code=400)
    path = os.path.join(SAVED_DIR, sid + '.json')
    if not os.path.exists(path):
        return JSONResponse({'error': 'not found'}, status_code=404)
    os.remove(path)
    return {'ok': True}


class ResolveReq(BaseModel):
    url: str


@app.post('/api/resolve')
def resolve(req: ResolveReq):
    """Turn a SoundCloud / 1001tracklists link into a plain-text setlist."""
    try:
        text = resolve_url(req.url)
    except Exception as e:
        return JSONResponse({'error': 'resolve_failed', 'message': str(e)}, status_code=400)
    if not text.strip():
        return JSONResponse({'error': 'empty', 'message': 'No tracks found at that link.'},
                            status_code=422)
    n = sum(1 for l in text.splitlines() if l.strip())
    return {'tracks': text, 'count': n}


class ShazamReq(BaseModel):
    url: str


@app.post('/api/shazam')
def shazam(req: ShazamReq):
    """Identify a mix's tracks by audio (description fast-path, else fingerprint)."""
    jid = uuid.uuid4().hex
    JOBS[jid] = {'status': 'running', 'progress': None, 'tracks': None}
    threading.Thread(target=run_shazam, args=(jid, req.url.strip()), daemon=True).start()
    return {'job_id': jid}


@app.get('/api/shazam_result/{jid}')
def shazam_result(jid: str):
    job = JOBS.get(jid)
    if not job or job['status'] != 'done':
        return JSONResponse({'error': 'not ready'}, status_code=404)
    tracks = job.get('tracks') or ''
    n = sum(1 for l in tracks.splitlines() if l.strip())
    return {'tracks': tracks, 'count': n}


@app.get('/sigma')
def sigma_page():
    return FileResponse(os.path.join(APP_DIR, 'static', 'sigma.html'))


@app.get('/globe')
def globe_page():
    return FileResponse(os.path.join(APP_DIR, 'static', 'globe.html'))


@app.get('/festival')
def festival_page():
    return FileResponse(os.path.join(APP_DIR, 'static', 'festival.html'))


@app.get('/m')
def mobile_page():
    return FileResponse(os.path.join(APP_DIR, 'static', 'mobile.html'))


@app.get('/')
def index():
    return FileResponse(os.path.join(APP_DIR, 'static', 'index.html'))


if __name__ == '__main__':
    import uvicorn
    print('Setlist Graph running -> http://127.0.0.1:8765')
    uvicorn.run(app, host='127.0.0.1', port=8765, log_level='warning')
