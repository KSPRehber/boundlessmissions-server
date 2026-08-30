"""Shared plumbing for the 2026-08-29 audit scripts.

Every script here encodes the behaviour the code *should* have. A line printed as
`BUG` is a reproduced finding; `ok` is either a control or a property that holds.
Exit status is 1 when any BUG line was printed, so the runner can count findings.

Nothing here writes to Firestore: the `store` singleton is used purely in memory
(no `load()`, no `save()`), every Firestore-backed module function a script
touches is replaced with an in-process fake, and notifications are muted.
"""
import copy
import os
import sys
import threading
import time

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BOT not in sys.path:
    sys.path.insert(0, BOT)
os.chdir(BOT)          # config.py / firebase credential paths are relative to the bot dir

BUGS: list[str] = []
OKS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> bool:
    if cond:
        OKS.append(label)
        print(f"  ok   {label}")
    else:
        BUGS.append(label)
        print(f"  BUG  {label}" + (f"\n         -> {detail}" if detail else ""))
    return bool(cond)


def section(title: str) -> None:
    print(f"\n{title}")


def finish() -> None:
    print(f"\n{len(OKS)} ok, {len(BUGS)} finding(s) reproduced")
    for b in BUGS:
        print(f"   - {b}")
    sys.exit(1 if BUGS else 0)


def src(relpath: str) -> str:
    with open(os.path.join(BOT, relpath), encoding="utf-8") as fh:
        return fh.read()


def between(text: str, start: str, end: str) -> str:
    """The slice of `text` from the first `start` to the next `end` after it."""
    i = text.index(start)
    j = text.index(end, i + len(start))
    return text[i:j]


def quiet(api_server) -> None:
    """Mute everything on the API module that would reach Firestore/Discord."""
    async def _noop_async(*a, **k):
        return None

    def _noop(*a, **k):
        return None

    api_server._create_notification = _noop
    api_server.flag_suspicion = _noop_async
    api_server._note_user_action = _noop
    if hasattr(api_server, "_deliver_craft_to_corp"):
        api_server._deliver_craft_to_corp = _noop_async


# ── A tiny in-memory Firestore, enough for the modules under test ────────────

try:
    from google.cloud.firestore_v1.transforms import Increment as _Increment, DELETE_FIELD as _DELETE
except Exception:  # pragma: no cover - the real SDK is present in the venv
    _Increment = None
    _DELETE = object()


def _apply(dst: dict, patch: dict) -> None:
    for k, v in patch.items():
        if v is _DELETE:
            dst.pop(k, None)
        elif _Increment is not None and isinstance(v, _Increment):
            dst[k] = (dst.get(k) or 0) + v.value
        elif isinstance(v, dict) and isinstance(dst.get(k), dict):
            _apply(dst[k], v)
        elif isinstance(v, dict):
            dst[k] = {}
            _apply(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)


class Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self.exists = data is not None
        self._d = copy.deepcopy(data) if data is not None else None
        self.reference = None

    def to_dict(self):
        return copy.deepcopy(self._d) if self._d is not None else None


class FakeDoc:
    def __init__(self, col, doc_id):
        self._col = col
        self.id = doc_id

    def get(self, transaction=None):
        # The snapshot is taken on arrival (as Firestore does) and the latency is
        # the trip back — so requests that arrive together all see the same state,
        # which is the window in which a read-then-write races.
        with self._col.lock:
            snap = Snap(self.id, self._col.docs.get(self.id))
        if self._col.latency:
            time.sleep(self._col.latency)
        return snap

    def set(self, data, merge=False):
        with self._col.lock:
            if merge and self.id in self._col.docs:
                _apply(self._col.docs[self.id], data)
            else:
                self._col.docs[self.id] = {}
                _apply(self._col.docs[self.id], data)

    def update(self, data):
        with self._col.lock:
            if self.id not in self._col.docs:
                raise KeyError(f"no such document {self.id}")
            _apply(self._col.docs[self.id], data)

    def delete(self):
        with self._col.lock:
            self._col.docs.pop(self.id, None)


class FakeCol:
    def __init__(self, latency: float = 0.0):
        self.docs: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.latency = latency

    def document(self, doc_id):
        return FakeDoc(self, str(doc_id))

    def stream(self):
        with self.lock:
            items = list(self.docs.items())
        return [Snap(k, v) for k, v in items]
