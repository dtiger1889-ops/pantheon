"""`pantheon/strings_inventory.py`: a read-only
scan, not a refactor. Every fixture here is a small synthetic module written to `tmp_path` --
never the real `pantheon/` package -- so these tests prove the scanner's rules (what counts as a
finding, how a `KEYS_TEXT`-style constant built out of other constants gets resolved, that one bad
file cannot sink the whole run) without being tied to today's exact wording anywhere in the app.
"""
from __future__ import annotations

from pathlib import Path

from pantheon import strings_inventory as inv


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- say / _say / notify calls


def test_finds_a_say_call_with_a_plain_string(tmp_path):
    path = _write(tmp_path, "a.py", 'class X:\n    def m(self):\n        self.say("hello there")\n')
    findings = inv.scan_file(path)
    # A plain string argument is resolved to its own text (no Python quote marks to read past) --
    # only an f-string or an attribute access falls back to the raw source (see the tests below).
    assert any(f.kind == "say" and f.text == "hello there" for f in findings)


def test_finds_an_underscore_say_call(tmp_path):
    path = _write(tmp_path, "a.py", 'class X:\n    def m(self):\n        self._say("gone now")\n')
    findings = inv.scan_file(path)
    assert any(f.kind == "_say" for f in findings)


def test_finds_a_notify_call(tmp_path):
    path = _write(tmp_path, "a.py", 'def f():\n    notify("heads up")\n')
    findings = inv.scan_file(path)
    assert any(f.kind == "notify" and "heads up" in f.text for f in findings)


def test_an_f_string_message_keeps_its_placeholder_visible(tmp_path):
    path = _write(tmp_path, "a.py", 'class X:\n    def m(self, n):\n        self.say(f"window {n} closed")\n')
    findings = inv.scan_file(path)
    match = next(f for f in findings if f.kind == "say")
    assert "{n}" in match.text     # the template, not a resolved value -- nothing ran the program


def test_a_say_call_with_no_argument_says_so_instead_of_crashing(tmp_path):
    path = _write(tmp_path, "a.py", 'class X:\n    def m(self):\n        self.say()\n')
    findings = inv.scan_file(path)
    assert any(f.kind == "say" and "check the call" in f.text for f in findings)


# ---------------------------------------------------------------- message= / LaunchResult(...)


def test_finds_a_message_keyword_argument_on_launchresult(tmp_path):
    path = _write(tmp_path, "a.py", 'x = LaunchResult(False, message="could not start")\n')
    findings = inv.scan_file(path)
    assert any(f.kind == "LaunchResult" and "could not start" in f.text for f in findings)


def test_a_launchresult_with_no_message_says_so(tmp_path):
    path = _write(tmp_path, "a.py", "x = LaunchResult(ok=True)\n")
    findings = inv.scan_file(path)
    assert any(f.kind == "LaunchResult" and "no message=" in f.text for f in findings)


def test_finds_a_message_keyword_on_any_other_call(tmp_path):
    """`Event(..., message=...)` is not `say`/`notify`/`LaunchResult`, but a `message=` kwarg
    reaching some other constructor is still worth a look."""
    path = _write(tmp_path, "a.py", 'e = Event(ts="x", event="kill", message="closed from the deck")\n')
    findings = inv.scan_file(path)
    match = next(f for f in findings if "message=" in f.kind)
    assert match.kind == "Event(message=)"
    assert "closed from the deck" in match.text


def test_a_message_built_from_a_module_constant_shows_the_real_sentence(tmp_path):
    """`providers/ollama.py`'s real shape: `LaunchResult(message=WAITING_ON_GO)` should show the
    sentence `WAITING_ON_GO` actually holds, not the bare variable name."""
    path = _write(
        tmp_path, "a.py",
        'WAITING = "not yet switched on"\nx = LaunchResult(False, message=WAITING)\n',
    )
    findings = inv.scan_file(path)
    match = next(f for f in findings if f.kind == "LaunchResult")
    assert match.text == "not yet switched on"


# ---------------------------------------------------------------- KEYS_TEXT / HELP_TEXT constants


def test_a_multiline_keys_text_constant_becomes_one_finding_per_line(tmp_path):
    path = _write(tmp_path, "a.py", 'KEYS_TEXT = """\\\nj    jump\nk    kill\n"""\n')
    findings = inv.scan_file(path)
    lines = [f.text for f in findings if f.kind == "constant"]
    assert "j    jump" in lines and "k    kill" in lines


def test_help_text_constant_is_also_caught(tmp_path):
    path = _write(tmp_path, "a.py", 'HELP_TEXT = """\\\nfirst line\nsecond line\n"""\n')
    findings = inv.scan_file(path)
    lines = [f.text for f in findings if f.kind == "constant"]
    assert "first line" in lines and "second line" in lines


def test_a_keys_text_built_by_joining_other_constants_is_resolved(tmp_path):
    """`pantheon/keys.py`'s real shape: `KEYS_TEXT = "\\n\\n".join((ANYWHERE, DECK, QUEUE, HUD))`.
    The join is resolved one level deep so the actual key descriptions show up, not the bare
    `"\\n\\n".join((ANYWHERE, DECK, QUEUE, HUD))` source text."""
    path = _write(
        tmp_path, "a.py",
        'DECK = "deck line one\\ndeck line two"\n'
        'QUEUE = "queue line one"\n'
        'KEYS_TEXT = "\\n\\n".join((DECK, QUEUE))\n',
    )
    findings = inv.scan_file(path)
    lines = [f.text for f in findings if f.kind == "constant"]
    assert lines == ["deck line one", "deck line two", "queue line one"]


def test_a_single_line_constant_with_that_name_is_still_reported(tmp_path):
    """Not every KEYS/HELP_TEXT-named thing is a multi-line block -- a one-liner still shows up,
    just as one finding instead of several."""
    path = _write(tmp_path, "a.py", 'HELP_TEXT = "one short line"\n')
    findings = inv.scan_file(path)
    assert any(f.kind == "constant" and f.text == "'one short line'" for f in findings)


def test_a_dict_named_like_a_keys_constant_is_not_silently_dropped(tmp_path):
    """`widgets/footer.py`'s real `PHONE_KEYS = {...}` dict is not a plain string, so it cannot be
    split into lines -- it must still show up as SOMETHING (the raw source), never vanish."""
    path = _write(tmp_path, "a.py", 'PHONE_KEYS = {"deck": "j jump   k kill"}\n')
    findings = inv.scan_file(path)
    assert any(f.kind == "constant" and "j jump" in f.text for f in findings)


def test_a_name_without_the_keys_or_help_text_marker_is_ignored(tmp_path):
    path = _write(tmp_path, "a.py", 'SOMETHING_ELSE = "not a key list"\n')
    findings = inv.scan_file(path)
    assert findings == []


# ---------------------------------------------------------------- resilience


def test_a_file_that_will_not_parse_reports_an_error_and_does_not_raise(tmp_path):
    path = _write(tmp_path, "broken.py", "def f(:\n    pass\n")
    findings = inv.scan_file(path)
    assert len(findings) == 1 and findings[0].kind == "error"


def test_scan_package_keeps_going_past_one_broken_file(tmp_path):
    _write(tmp_path, "broken.py", "def f(:\n")
    _write(tmp_path, "fine.py", 'class X:\n    def m(self):\n        self.say("still found")\n')
    findings = inv.scan_package(tmp_path)
    assert any(f.kind == "error" for f in findings)
    assert any(f.kind == "say" for f in findings)


def test_scan_package_skips_pycache(tmp_path):
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    _write(cache, "junk.py", 'self.say("should never be seen")\n')
    assert inv.scan_package(tmp_path) == []


def test_scan_package_recurses_into_subfolders(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    _write(sub, "deep.py", 'class X:\n    def m(self):\n        self.say("found it")\n')
    findings = inv.scan_package(tmp_path)
    assert any(f.kind == "say" and "found it" in f.text for f in findings)


# ---------------------------------------------------------------- rendering and main()


def test_render_includes_the_count_and_every_finding():
    findings = [inv.Finding(Path("pantheon/x.py"), 3, "say", "'hi'")]
    text = inv.render(findings)
    assert "1 findings." in text
    assert "pantheon/x.py:3" in text and "[say]" in text and "'hi'" in text


def test_render_with_no_findings_still_says_zero():
    assert "0 findings." in inv.render([])


def test_main_prints_the_real_package_inventory(capsys):
    """A light end-to-end check against the real `pantheon/` tree -- not asserting on today's
    exact wording (that would make this test as brittle as the code it is inventorying), only that
    running it finds a non-trivial number of real sentences and never raises."""
    rc = inv.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "User-facing strings inventory" in out
    assert "pantheon/" in out
    count_line = next(ln for ln in out.splitlines() if ln.endswith("findings."))
    assert int(count_line.split()[0]) > 20     # this package has far more than a couple of strings
