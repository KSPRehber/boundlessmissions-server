"""In-memory Storage bucket + per-(guild,user) import queues for the craft audit."""
import copy, threading
from _h import FakeCol


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        with self.bucket.lock:
            # GCS: if_generation_match=0 means "only if no object exists" (412 otherwise).
            if if_generation_match == 0 and self.name in self.bucket.objects:
                raise RuntimeError(f"412 Precondition Failed: {self.name} exists")
            self.bucket.objects[self.name] = {"data": bytes(data), "ct": content_type, "public": False}

    def make_public(self):
        self.bucket.objects[self.name]["public"] = True

    @property
    def public_url(self):
        return f"https://storage.example/{self.name}"

    def generate_signed_url(self, **kw):
        return f"https://signed.example/{self.name}?sig"

    def delete(self):
        with self.bucket.lock:
            self.bucket.objects.pop(self.name, None)


class FakeBucket:
    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.lock = threading.Lock()

    def blob(self, name):
        return FakeBlob(self, name)

    def list_blobs(self, prefix=""):
        with self.lock:
            names = [n for n in self.objects if n.startswith(prefix)]
        return [FakeBlob(self, n) for n in names]


class FakeQueues:
    """Replaces data.imports._col: one FakeCol per (guild_id, user_id)."""
    def __init__(self):
        self.cols: dict[tuple[str, str], FakeCol] = {}

    def __call__(self, gid, uid):
        return self.cols.setdefault((str(gid), str(uid)), FakeCol())

    def entries(self, gid, uid):
        return [copy.deepcopy(v) for v in self(gid, uid).docs.values()]
