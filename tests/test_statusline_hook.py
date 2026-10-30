"""hooks/statusline.py keeps bin/statusline_capture's root contract:
PANTHEON_ROOT picks whose pantheon package runs, and the capture runs from that root."""
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "statusline.py"


def test_pantheon_root_picks_the_package_and_the_working_folder(tmp_path):
    hud = tmp_path / "pantheon" / "hud"
    hud.mkdir(parents=True)
    (tmp_path / "pantheon" / "__init__.py").write_text("")
    (hud / "__init__.py").write_text("")
    (hud / "capture.py").write_text("import os\ndef capture(raw):\n    return 'fake root ' + os.getcwd()\n")
    env = dict(os.environ, PANTHEON_ROOT=str(tmp_path))
    out = subprocess.run([sys.executable, str(HOOK)], input="{}", capture_output=True, text=True,
                         env=env, cwd=str(tmp_path.parent), timeout=30).stdout.strip()
    assert out.startswith("fake root ")
    assert Path(out[len("fake root "):]).resolve() == tmp_path.resolve()


def test_guide_segment_from_payload_cwd(tmp_path):
    sys.path.insert(0, str(HOOK.parent))
    try:
        import statusline
    finally:
        sys.path.remove(str(HOOK.parent))
    game = tmp_path / "Guides" / "atomic_heart"
    game.mkdir(parents=True)
    (game / "persona.md").write_text("## Current active persona\n\n**NORA** -- notes\n---\n")
    (game / "warning_tiers.md").write_text("- **Enemies: Tier 2** x\n- **Puzzles: Tier 1** y\n")
    seg = statusline.guide_segment(str(game).replace("/", "\\"))
    assert seg == "NORA · atomic heart guide · enemy tier 2 · puzzle tier 1"
    assert statusline.guide_segment(str(tmp_path)) == ""
