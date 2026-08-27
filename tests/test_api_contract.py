"""The front end must read keys /capture returns and send params /email requires.

Derived from source on both sides rather than restating the names, so the two drift
apart loudly instead of silently. Pure stdlib: no app dependencies, no camera.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN = (ROOT / "main.py").read_text()
HTML = (ROOT / "index.html").read_text()
TREE = ast.parse(MAIN)


def _fn(name):
    return next(n for n in ast.walk(TREE)
                if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name)


def _capture_returns():
    keys = set()
    for node in ast.walk(_fn("capture_image")):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            keys |= {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    return keys


def _frontend_reads():
    i = HTML.index("fetch('/capture'")
    return set(re.findall(r"\bdata\.(\w+)", HTML[i:i + 1200]))


def test_capture_returns_a_dict_literal():
    assert _capture_returns()


def test_frontend_reads_only_keys_capture_returns():
    missing = _frontend_reads() - _capture_returns()
    assert not missing, f"index.html reads {sorted(missing)}, which /capture does not return"


def test_frontend_uses_the_image_url_key():
    assert "url" in _frontend_reads()


def test_frontend_sends_every_required_email_param():
    sig = _fn("trigger_email").args
    required = {
        a.arg for a, d in zip(sig.args, sig.defaults)
        if isinstance(d, ast.Call) and getattr(d.func, "id", "") == "Query"
        and d.args and isinstance(d.args[0], ast.Constant) and d.args[0].value is Ellipsis
    }
    sent = set(re.findall(r"[?&](\w+)=", re.search(r"`/email\?[^`]*`", HTML).group(0)))
    assert required <= sent, f"index.html omits {sorted(required - sent)}"


def test_capture_downloads_through_the_retry_helper():
    """A direct client.get would bypass the retries and the 90s timeout."""
    fn = _fn("capture_image")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
    direct = [n for n in calls
              if isinstance(n.func, ast.Attribute) and n.func.attr == "get"
              and isinstance(n.func.value, ast.Name) and n.func.value.id == "client"]
    assert not direct, f"direct client.get at line(s) {[n.lineno for n in direct]}"
    assert "download_capture" in {n.func.id for n in calls if isinstance(n.func, ast.Name)}


def test_capture_502_names_the_failing_phase():
    src = ast.get_source_segment(MAIN, _fn("capture_image"))
    assert "during {stage}" in src
    assert "CAPTURE_POLL_ATTEMPTS" in src
