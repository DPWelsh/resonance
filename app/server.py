"""
resonance app — FastAPI backend (bring-your-own-Discogs-token).

POST /api/analyze   multipart: file=<playlist.csv>, token=<discogs>, deep=<bool> -> {job_id}
GET  /api/status/{job_id}                                                        -> {status, progress}
GET  /api/result/{job_id}                                                        -> graph + lists
GET  /                                                                           -> the web UI

Run:  uvicorn app.server:app --reload   (from repo root)
"""
import os, uuid, threading
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.pipeline import parse_playlist_csv, analyze, Discogs

HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="resonance")
JOBS = {}  # job_id -> {status, progress, result, error}


def _run(jid, token, rows, deep):
    def prog(stage, done, total):
        JOBS[jid]['progress'] = {'stage': stage, 'done': done, 'total': total}
    try:
        JOBS[jid]['result'] = analyze(token, rows, progress=prog, deep=deep)
        JOBS[jid]['status'] = 'done'
    except Exception as e:  # noqa
        JOBS[jid]['status'] = 'error'
        JOBS[jid]['error'] = str(e)


@app.post('/api/analyze')
async def start(file: UploadFile = File(...), token: str = Form(...), deep: bool = Form(False)):
    text = (await file.read()).decode('utf-8', 'ignore')
    rows = parse_playlist_csv(text)
    if not rows:
        raise HTTPException(400, 'No tracks found — is this an Exportify CSV?')
    if not Discogs(token).verify():
        raise HTTPException(401, 'Discogs token rejected — check it at discogs.com/settings/developers')
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {'status': 'running', 'progress': {'stage': 'starting', 'done': 0, 'total': len(rows)}}
    threading.Thread(target=_run, args=(jid, token, rows, deep), daemon=True).start()
    return {'job_id': jid, 'tracks': len(rows)}


@app.get('/api/status/{jid}')
def status(jid: str):
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, 'unknown job')
    return {'status': j['status'], 'progress': j.get('progress'), 'error': j.get('error')}


@app.get('/api/result/{jid}')
def result(jid: str):
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, 'unknown job')
    if j['status'] != 'done':
        raise HTTPException(409, f"job is {j['status']}")
    return j['result']


@app.get('/')
def index():
    return FileResponse(os.path.join(HERE, 'static', 'index.html'))


app.mount('/static', StaticFiles(directory=os.path.join(HERE, 'static')), name='static')
