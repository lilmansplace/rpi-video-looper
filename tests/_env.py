"""Shared test environment: must be imported before any looper module."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

if "LOOPER_TEST_TMP" not in os.environ:
    os.environ["LOOPER_TEST_TMP"] = tempfile.mkdtemp()
TMP = os.environ["LOOPER_TEST_TMP"]
os.environ.setdefault("LOOPER_RUN_DIR", os.path.join(TMP, "run"))
os.environ.setdefault("LOOPER_STATE_DIR", os.path.join(TMP, "state"))
os.environ.setdefault("LOOPER_CONFIG", os.path.join(TMP, "looper.toml"))
os.environ.setdefault("LOOPER_NET_RUN_DIR", os.path.join(TMP, "net"))
os.environ.setdefault("LOOPER_NET_STATE", os.path.join(TMP, "net", "state.json"))
os.environ.setdefault("LOOPER_BOOT_SETTLE", "0")  # no boot wait off-device
