"""Account-audit helpers, kept in their own module so a co-running audit's _h.py
can't clobber them. In-memory Firestore doubles for data/accounts + data/twofa."""
import os, sys, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "audit_2908"))
from _harness import Snap  # noqa: E402


def _apply(dst, patch):
    from google.cloud.firestore_v1.transforms import Increment, ArrayUnion, ArrayRemove, DELETE_FIELD
    for k, v in patch.items():
        if v is DELETE_FIELD:
            dst.pop(k, None)
        elif isinstance(v, Increment):
            dst[k] = (dst.get(k) or 0) + v.value
        elif isinstance(v, ArrayUnion):
            cur = list(dst.get(k) or [])
            for e in v.values:
                if e not in cur:
                    cur.append(e)
            dst[k] = cur
        elif isinstance(v, ArrayRemove):
            dst[k] = [e for e in (dst.get(k) or []) if e not in v.values]
        elif isinstance(v, dict) and isinstance(dst.get(k), dict):
            _apply(dst[k], v)
        else:
            dst[k] = v


class Doc:
    def __init__(self, col, doc_id):
        self._col = col
        self.id = str(doc_id)

    def get(self, transaction=None):
        if self._col.fail_reads > 0:
            self._col.fail_reads -= 1
            raise RuntimeError("firestore down")
        with self._col.lock:
            snap = Snap(self.id, self._col.docs.get(self.id))
        if self._col.latency:
            time.sleep(self._col.latency)
        snap.reference = self
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
                raise KeyError(self.id)
            _apply(self._col.docs[self.id], data)

    def delete(self):
        with self._col.lock:
            self._col.docs.pop(self.id, None)

    @property
    def reference(self):
        return self

    @reference.setter
    def reference(self, _v):
        pass


class Col:
    def __init__(self, latency=0.0):
        self.docs = {}
        self.lock = threading.Lock()
        self.latency = latency
        self.fail_reads = 0

    def document(self, doc_id):
        return Doc(self, str(doc_id))

    def where(self, field=None, op=None, value=None, filter=None):
        if filter is not None:
            field, value = filter.field_path, filter.value
        col = self

        class _Q:
            def limit(self, _n):
                return self

            def stream(self):
                with col.lock:
                    items = list(col.docs.items())
                for k, v in items:
                    if v.get(field) == value:
                        s = Snap(k, v)
                        s.reference = Doc(col, k)
                        yield s
        return _Q()

    def order_by(self, *_a, **_k):
        return self

    def start_at(self, *_a, **_k):
        return self

    def end_at(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def stream(self):
        with self.lock:
            items = list(self.docs.items())
        for k, v in items:
            s = Snap(k, v)
            s.reference = Doc(self, k)
            yield s


class DB:
    def __init__(self, latency=0.0):
        self.cols = {}
        self.latency = latency

    def collection(self, name):
        if name not in self.cols:
            self.cols[name] = Col(self.latency)
        return self.cols[name]

    def transaction(self):
        class _Txn:
            def set(self, ref, payload, merge=False):
                ref.set(payload, merge=merge)

            def update(self, ref, payload):
                ref.update(payload)
        return _Txn()


def patch_accounts_db(db):
    from data import accounts, twofa
    accounts._db = db
    twofa._db = db
    accounts.firestore = type("_FS", (), {"transactional": staticmethod(lambda fn: fn)})()


class MemStore:
    def __init__(self):
        self.users = {}

    def has_user(self, uid):
        return str(uid) in self.users

    def get_user(self, _gid, uid):
        return self.users.setdefault(str(uid), {"xp": 0, "balance": 0,
                                                "rescues": 0, "unlocked_levels": []})
