from __future__ import annotations
import os, sqlite3
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

DB_PATH = os.getenv('DATABASE_PATH', 'munna_signals.sqlite3')
ROOT = Path(__file__).parent
app = FastAPI(title='MUNNA AI PREMIUM Dashboard', version='1.1')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['GET'], allow_headers=['*'])

def db():
    if not Path(DB_PATH).exists():
        return None
    con = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    return con

@app.get('/')
def home():
    return FileResponse(ROOT / 'dashboard' / 'index.html')

@app.get('/api/health')
def health():
    return {'dashboard': 'online', 'database': Path(DB_PATH).exists(), 'feed': 'not_verified'}

@app.get('/api/report')
def report():
    con = db()
    if con is None:
        return {'PENDING': 0, 'WIN': 0, 'LOSS': 0, 'TIE': 0, 'VOID': 0}
    try:
        rows = con.execute('SELECT status, COUNT(*) n FROM signals GROUP BY status').fetchall()
        data = {r['status']: int(r['n']) for r in rows}
        return {k: data.get(k, 0) for k in ['PENDING','WIN','LOSS','TIE','VOID']}
    finally: con.close()

@app.get('/api/history')
def history():
    con = db()
    if con is None: return []
    try:
        rows = con.execute('SELECT id,symbol,direction,entry_ts,expiry_ts,entry_price,status,exit_price FROM signals ORDER BY id DESC LIMIT 30').fetchall()
        return [dict(r) for r in rows]
    finally: con.close()
