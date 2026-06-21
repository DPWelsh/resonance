#!/usr/bin/env python3
"""Setlist -> graph web app. Wraps setlist_graph.py in a FastAPI server with
live (SSE) progress. Run:  python3 app/server.py   then open http://127.0.0.1:8765
"""
import os, sys, json, uuid, time, threading, tempfile, traceback, hashlib
APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(APP_DIR))          # import setlist_graph from parent
import setlist_graph as slg
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from pydantic import BaseModel

app = FastAPI(title="Setlist Graph")
JOBS = {}
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
            'appearances': d.get('appearances', 0),
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


class BuildReq(BaseModel):
    setlist: str
    deep: bool = True


@app.post('/api/build')
def build(req: BuildReq):
    jid = uuid.uuid4().hex
    JOBS[jid] = {'status': 'running', 'progress': None, 'graph': None}
    threading.Thread(target=run_build, args=(jid, req.setlist, req.deep), daemon=True).start()
    return {'job_id': jid}


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


@app.get('/api/presets')
def presets():
    out = []
    for p in PRESETS:
        path = os.path.join(APP_DIR, p['file'])
        n = max(0, sum(1 for _ in open(path)) - 1) if os.path.exists(path) else 0
        out.append({'id': p['id'], 'name': p['name'], 'tracks': n})
    return out


@app.get('/api/preset/{pid}')
def preset(pid: str):
    p = next((x for x in PRESETS if x['id'] == pid), None)
    if not p:
        return JSONResponse({'error': 'unknown preset'}, status_code=404)
    return {'content': open(os.path.join(APP_DIR, p['file']), encoding='utf-8').read()}


@app.get('/sigma')
def sigma_page():
    return FileResponse(os.path.join(APP_DIR, 'static', 'sigma.html'))


@app.get('/globe')
def globe_page():
    return FileResponse(os.path.join(APP_DIR, 'static', 'globe.html'))


@app.get('/')
def index():
    return FileResponse(os.path.join(APP_DIR, 'static', 'index.html'))


if __name__ == '__main__':
    import uvicorn
    print('Setlist Graph running -> http://127.0.0.1:8765')
    uvicorn.run(app, host='127.0.0.1', port=8765, log_level='warning')
