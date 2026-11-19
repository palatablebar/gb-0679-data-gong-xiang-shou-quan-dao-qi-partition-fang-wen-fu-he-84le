import sqlite3,json
from pathlib import Path
from app.config import DATA_DIR
from app.errors import DomainError
def connect():
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(DATA_DIR/"records.db",timeout=20)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,payload TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,decision TEXT NOT NULL DEFAULT '待复核')")
    db.execute("CREATE TABLE IF NOT EXISTS rulings (checkpoint_key TEXT PRIMARY KEY,run_id TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS ruling_runs (checkpoint_key TEXT NOT NULL,run_id TEXT NOT NULL,PRIMARY KEY(checkpoint_key,run_id))")
    return db
def save(run_id,payload):
    ruling=payload.get("ruling")
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO runs(id,payload) VALUES (?,?)",(run_id,json.dumps(payload,ensure_ascii=False)))
        if ruling and ruling.get("checkpoint_key"):
            db.execute("INSERT OR IGNORE INTO rulings(checkpoint_key,run_id,payload,created_at) VALUES (?,?,?,?)",(ruling["checkpoint_key"],run_id,json.dumps(ruling,ensure_ascii=False),ruling["created_at"]))
            db.execute("INSERT OR IGNORE INTO ruling_runs(checkpoint_key,run_id) VALUES (?,?)",(ruling["checkpoint_key"],run_id))
    return get(run_id)
def get(run_id):
    with connect() as db: row=db.execute("SELECT payload,version,decision FROM runs WHERE id=?",(run_id,)).fetchone()
    if not row: raise DomainError("NOT_FOUND","批次不存在",404)
    return {**json.loads(row[0]),"version":row[1],"decision":row[2]}
def latest():
    with connect() as db: row=db.execute("SELECT id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
    return get(row[0]) if row else None
def history():
    with connect() as db: return [{"run_id":i,"version":v,"decision":d} for i,v,d in db.execute("SELECT id,version,decision FROM runs ORDER BY rowid DESC")]
def review(run_id,version,decision):
    if decision not in {"已复核","退回复核"}: raise DomainError("BAD_DECISION","复核状态无效")
    with connect() as db:
        cursor=db.execute("UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?",(decision,run_id,version))
        if cursor.rowcount!=1:
            if not db.execute("SELECT 1 FROM runs WHERE id=?",(run_id,)).fetchone(): raise DomainError("NOT_FOUND","批次不存在",404)
            raise DomainError("STALE_VERSION","版本已更新，请刷新",409)
    return get(run_id)
def find_ruling(checkpoint_key):
    if not checkpoint_key: return None
    with connect() as db: row=db.execute("SELECT payload FROM rulings WHERE checkpoint_key=?",(checkpoint_key,)).fetchone()
    return json.loads(row[0]) if row else None
def record_ruling(run_id,ruling):
    """把裁定原子写入；同一断点只保留首次裁定，返回已存在的裁定（若有）。"""
    if not ruling.get("checkpoint_key"):
        return {"ruling":ruling,"run_id":run_id}
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO rulings(checkpoint_key,run_id,payload,created_at) VALUES (?,?,?,?)",(ruling["checkpoint_key"],run_id,json.dumps(ruling,ensure_ascii=False),ruling["created_at"]))
        db.execute("INSERT OR IGNORE INTO ruling_runs(checkpoint_key,run_id) VALUES (?,?)",(ruling["checkpoint_key"],run_id))
        row=db.execute("SELECT payload,run_id FROM rulings WHERE checkpoint_key=?",(ruling["checkpoint_key"],)).fetchone()
    return {"ruling":json.loads(row[0]),"run_id":row[1]}
def ruling_for(run_id):
    with connect() as db: row=db.execute("SELECT payload FROM rulings WHERE run_id=?",(run_id,)).fetchone()
    return json.loads(row[0]) if row else None
def rulings():
    with connect() as db: return [json.loads(p) for (p,) in db.execute("SELECT payload FROM rulings ORDER BY created_at")]
def runs_for_checkpoint(checkpoint_key):
    with connect() as db:
        return [{"run_id":i,"version":v,"decision":d} for i,v,d in db.execute("SELECT r.id,r.version,r.decision FROM runs r JOIN ruling_runs m ON m.run_id=r.id WHERE m.checkpoint_key=? ORDER BY r.rowid",(checkpoint_key,))]
