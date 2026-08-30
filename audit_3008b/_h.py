"""Bridge to the 2908 harness (same rules: nothing here writes to Firestore)."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "audit_2908"))
from _harness import check, section, finish, quiet, src, between, FakeCol  # noqa: F401,E402
