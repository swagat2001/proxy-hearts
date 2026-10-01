"""Tiny thread-safe JSON store. Seeds from seed/demo.json so the finished example survives redeploys."""
import copy
import json
import os
import shutil
import threading
import time
import uuid

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data", "store.json")
SEED = os.path.join(ROOT, "seed", "demo.json")
_lock = threading.RLock()
_db = None
_dirty = False


def _empty():
    return {"people": {}, "dates": {}, "scores": {}, "invites": [], "events": [], "round": {"status": "idle"}}


def load():
    global _db
    with _lock:
        if _db is not None:
            return _db
        os.makedirs(os.path.dirname(DATA), exist_ok=True)
        if not os.path.exists(DATA) and os.path.exists(SEED):
            shutil.copy(SEED, DATA)
        if os.path.exists(DATA):
            with open(DATA, encoding="utf-8") as f:
                _db = json.load(f)
            for k, v in _empty().items():
                _db.setdefault(k, v)
        else:
            _db = _empty()
        return _db


def save(force=False):
    global _dirty
    with _lock:
        tmp = DATA + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(load(), f, ensure_ascii=False, indent=1)
        os.replace(tmp, DATA)
        _dirty = False


def _autosaver():
    while True:
        time.sleep(2)
        if _dirty:
            try:
                save()
            except Exception as e:  # noqa: BLE001
                print("save failed", e)


threading.Thread(target=_autosaver, daemon=True).start()


def touch():
    global _dirty
    _dirty = True


def new_id():
    return uuid.uuid4().hex[:8]


def snapshot(key=None):
    with _lock:
        db = load()
        return copy.deepcopy(db[key] if key else db)


VERSION = [0]


def mutate(fn):
    with _lock:
        out = fn(load())
        VERSION[0] += 1
        touch()
        return out


def read(key=None):
    """Zero-copy view for read-only use (pages). Do not modify the result."""
    db = load()
    return db[key] if key else db


def event(kind, text, **extra):
    def _f(db):
        db["event_seq"] = db.get("event_seq", 0) + 1
        db["events"].append({"n": db["event_seq"], "t": time.time(), "kind": kind, "text": text, **extra})
        if len(db["events"]) > 3000:
            db["events"] = db["events"][-2000:]
    mutate(_f)
