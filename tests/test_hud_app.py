"""The HUD pane itself: does it draw two readable lines per provider at 80 columns, and does
`glyphs = "ascii"` really produce ASCII?

Textual's test harness is async and this project has no async pytest plugin, so each test
drives it through `asyncio.run`.
"""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pantheon import config, theme
from pantheon.hud import app as hud_app
from pantheon.hud import calc as hud_calc
from pantheon.models import format_clock

FIXTURES = Path(__file__).parent / "fixtures" / "hud"
MARKUP = re.compile(r"\[/?[^\]]*\]")


def visible(text: str) -> str:
    """What a terminal actually shows: colour markup takes up no columns."""
    return MARKUP.sub("", text)


def widget_text(widget) -> str:
    """The words a Static is currently displaying, whatever Textual wraps them in."""
    content = widget.content
    return getattr(content, "plain", None) or str(content)


def _picture(fresh: bool = True) -> dict:
    with open(FIXTURES / "hud.json", "r", encoding="utf-8") as fh:
        picture = json.load(fh)
    if fresh:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        picture["fetched_at"] = stamp
        picture["claude"]["fetched_at"] = stamp
        picture["codex"]["fetched_at"] = stamp
    return picture


@pytest.fixture()
def make_cfg(tmp_path):
    def _make(glyphs: str = "unicode", picture: dict | None = None):
        cfg = config.Config(state_dir=str(tmp_path / glyphs / "state"), appearance={"glyphs": glyphs})
        target = Path(cfg.hud_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(picture or _picture()), encoding="utf-8")
        return cfg

    return _make


def _run_app(cfg, size=(92, 30)):
    """Mount the pane, let it draw once, and hand back what it put on screen.

    92 columns of terminal (90 of drawable width, once the pane's own padding is subtracted)
    is the narrowest size that still renders the two-line-per-provider block -- the phone
    breakpoint starts one column below that. See test_hud_app.py's phone-width
    tests for the under-90 behaviour.
    """

    async def _go():
        application = hud_app.HudApp(cfg, start_refresher=False)
        async with application.run_test(size=size):
            await application.workers.wait_for_complete()
            body = widget_text(application.query_one("#hud-body"))
        return list(application.rendered_lines), str(body)

    return asyncio.run(_go())


# ---- rendering ------------------------------------------------------------

def test_two_lines_per_provider_at_eighty_columns(make_cfg):
    lines, body = _run_app(make_cfg())
    shown = [visible(line) for line in lines]

    claude = [i for i, line in enumerate(shown) if line.startswith("Claude")]
    codex = [i for i, line in enumerate(shown) if line.startswith("Codex")]
    assert len(claude) == 1 and len(codex) == 1

    for start in (claude[0], codex[0]):
        first, second = shown[start], shown[start + 1]
        assert "5h" in first and "week" in first
        assert "resets" in second and "today" in second
    # Changed 2026-09-26: an unknown burn is left out instead of `burn ?`; the fixture's
    # Codex has no burn rate, its Claude has one.
    assert "burn" in shown[claude[0] + 1] and "burn" not in shown[codex[0] + 1]
    assert "?" not in "\n".join(shown)
    assert "42%" in shown[claude[0]] and "61%" in shown[claude[0]]
    assert "81%" in shown[codex[0]] and "29%" in shown[codex[0]]
    assert "today $77.15" in shown[claude[0] + 1]
    assert "today $13.65" in shown[codex[0] + 1]
    assert "Claude" in body and "Codex" in body


def test_nothing_wraps_at_ninety_columns(make_cfg):
    # The pane keeps one column of padding each side, so 90 is all a line may use at 92.
    lines, _ = _run_app(make_cfg())
    for line in lines:
        assert len(visible(line)) <= 90, line
    for line in hud_app.HELP_TEXT.splitlines():
        assert len(line) <= 78, line


def test_the_trend_sparkline_is_dropped_rather_than_wrapping(make_cfg):
    # Right at the phone breakpoint: one column narrower drops the whole
    # two-line block for the one-line phone form, rather than wrapping anything.
    cfg = make_cfg()
    picture = _picture()
    wide = hud_app.render_lines(picture, cfg, hud_app.PHONE_WIDTH_BREAKPOINT)
    narrow = hud_app.render_lines(picture, cfg, hud_app.PHONE_WIDTH_BREAKPOINT - 1)
    claude_wide = next(line for line in wide if visible(line).startswith("Claude"))
    claude_narrow = next(line for line in narrow if visible(line).startswith("Claude"))
    assert len(visible(claude_wide)) <= hud_app.PHONE_WIDTH_BREAKPOINT
    assert len(visible(claude_narrow)) < len(visible(claude_wide))


def test_one_reading_is_not_drawn_as_a_trend(make_cfg):
    cfg = make_cfg()
    picture = _picture()
    picture["history"]["claude_pct"] = [42.0]
    line = next(
        l for l in hud_app.render_lines(picture, cfg, 90) if visible(l).startswith("Claude")
    )
    assert visible(line).rstrip().endswith("61%")


# ---- phone width --------------------------------------------

def test_phone_width_is_one_line_per_provider(make_cfg):
    # 80 columns: under PHONE_WIDTH_BREAKPOINT (90), so the one-line-per-provider layout, with
    # room for every part of the line.
    cfg = make_cfg()
    picture = _picture()
    lines = hud_app.render_lines(picture, cfg, 80)
    shown = [visible(line) for line in lines]
    for line in shown:
        assert len(line) <= 80, line

    claude = [i for i, line in enumerate(shown) if line.startswith("Claude")]
    codex = [i for i, line in enumerate(shown) if line.startswith("Codex")]
    assert len(claude) == 1 and len(codex) == 1
    assert "5h" in shown[claude[0]] and "wk" in shown[claude[0]] and "burn" in shown[claude[0]]
    assert "no live session yet" not in shown[codex[0]]   # retired 2026-09-02 (see the next two)


def test_phone_line_says_why_claude_has_no_numbers(make_cfg):
    # The user's phone, 2026-09-02 17:00: `Claude 5h ???????? ? · wk ? · burn 9%/h · no live
    # session yet` while the agents panel showed a Claude session working on the desktop. The
    # Desktop app writes no statusline, so the line now names what would fill it in, the bar
    # of question marks is gone (copy review row 30), and it fits the 65-column phone.
    cfg = make_cfg()
    picture = _picture()
    picture["claude"].update({
        "source": "ccusage", "note": "from history", "five_hour_pct": None,
        "seven_day_pct": None, "burn_pct_per_hour": None,
    })
    line = next(visible(l) for l in hud_app.render_lines(picture, cfg, 65)
                if visible(l).startswith("Claude"))
    # 2026-09-26: the reason now comes before the burn rate (a narrow line is cut from the right)
    assert "no reading yet · needs Claude in tmux" in line, line
    assert "????" not in line and "no live session" not in line
    assert len(line) <= 65


def test_phone_line_dates_a_codex_reading(make_cfg):
    # Codex has no live feed: its own log is the source, so the line says how old the reading
    # is. Changed 2026-09-26: the age in words (`last reading 3h ago`), and only once the
    # reading is older than ten minutes -- the same rule the Claude numbers follow.
    cfg = make_cfg()
    picture = _picture()
    old = (datetime.now(timezone.utc) - timedelta(hours=3, minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    picture["codex"].update({
        "source": "codex-log", "note": "", "reported_at": old,
        "five_hour_pct": 50.0, "seven_day_pct": 61.0, "burn_pct_per_hour": None,
    })
    line = next(visible(l) for l in hud_app.render_lines(picture, cfg, 65)
                if visible(l).startswith("Codex"))
    assert line.endswith("last reading 3h ago"), line
    assert len(line) <= 65
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    picture["codex"]["reported_at"] = fresh
    line = next(visible(l) for l in hud_app.render_lines(picture, cfg, 65)
                if visible(l).startswith("Codex"))
    assert "last reading" not in line and "as of" not in line


def test_desk_block_dates_a_codex_reading_and_explains_the_claude_gap(make_cfg):
    cfg = make_cfg()
    picture = _picture()
    old = (datetime.now(timezone.utc) - timedelta(minutes=42)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    picture["codex"].update({"source": "codex-log", "note": "", "reported_at": old})
    picture["claude"].update({"source": "ccusage", "note": "from history", "five_hour_pct": None})
    text = "\n".join(visible(l) for l in hud_app.render_lines(picture, cfg, 120))
    assert "last reading 42m ago" in text      # was `as of HH:MM` until 2026-09-26
    assert "from Codex's own log" in text
    assert "need a Claude Code session in tmux" in text
    assert "????" not in text


def test_phone_width_pane_mounts_at_sixty_columns(make_cfg):
    lines, _ = _run_app(make_cfg(), size=(62, 30))
    shown = [visible(line) for line in lines]
    for line in shown:
        assert len(line) <= 60, line
    assert sum(1 for line in shown if line.startswith("Claude")) == 1
    assert sum(1 for line in shown if line.startswith("Codex")) == 1


def test_at_ninety_columns_nothing_changes_from_the_wide_block(make_cfg):
    cfg = make_cfg()
    picture = _picture()
    lines = hud_app.render_lines(picture, cfg, hud_app.PHONE_WIDTH_BREAKPOINT)
    shown = [visible(line) for line in lines]
    claude = next(i for i, line in enumerate(shown) if line.startswith("Claude"))
    assert "week" in shown[claude]                      # the wide label, not the phone "wk"
    assert "burn" in shown[claude + 1] and "resets" in shown[claude + 1]


def test_ascii_glyphs_produce_pure_ascii(make_cfg):
    lines, body = _run_app(make_cfg("ascii"))
    joined = "\n".join(lines)
    assert all(ord(c) < 128 for c in joined), [c for c in joined if ord(c) >= 128][:5]
    assert all(ord(c) < 128 for c in body)
    assert "#" in joined and "." in joined  # the ASCII bar characters


def test_unicode_mode_uses_block_bars(make_cfg):
    lines, _ = _run_app(make_cfg())
    assert any("█" in line for line in lines)


# ---- what the words say ---------------------------------------------------

def test_the_screen_explains_itself_in_plain_words(make_cfg):
    lines, _ = _run_app(make_cfg())
    text = visible("\n".join(lines))
    assert "getting full" in text or "ok" in text  # the memory band, named in words
    assert "from Codex's own log" in text          # Codex never reports live
    assert "five_hour_pct" not in text and "ctx" not in text


def test_a_provider_with_no_numbers_says_so_once_in_words(make_cfg):
    blank = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "claude": {"provider": "claude", "source": "unknown", "note": "no live session"},
        "codex": {"provider": "codex", "source": "unknown", "note": "no Codex logs found"},
        "history": {"claude_pct": [], "claude_samples": []},
    }
    lines, _ = _run_app(make_cfg("unicode", blank))
    shown = [visible(line) for line in lines]
    claude = next(line for line in shown if line.startswith("Claude"))
    # Changed 2026-09-26: the unknown numbers are said once, in words.
    assert "no reading yet" in claude
    assert not any("?" in line for line in shown)
    assert not any("0%" in line for line in shown)  # unknown is never drawn as zero


def test_attention_line_appears_only_when_something_is_high(make_cfg):
    quiet, _ = _run_app(make_cfg())
    assert not any("of this 5-hour budget is used" in visible(line) for line in quiet)

    loud_picture = _picture()
    loud_picture["claude"]["five_hour_pct"] = 94.0
    loud, _ = _run_app(make_cfg("unicode", loud_picture))
    assert any("94% of this 5-hour budget is used" in visible(line) for line in loud)


def test_stale_file_says_so(make_cfg):
    old = _picture(fresh=False)
    lines, _ = _run_app(make_cfg("unicode", old))
    assert any("more than 5 minutes old" in visible(line) for line in lines)


def test_empty_state_directory_does_not_crash_the_pane(tmp_path):
    cfg = config.Config(state_dir=str(tmp_path / "state"))
    lines, _ = _run_app(cfg)
    assert any("No usage data yet" in visible(line) for line in lines)


# ---- desk cards at >= 90 columns ----------------

def _mount_window(cfg, size):
    async def _go():
        application = hud_app.HudApp(cfg, start_refresher=False)
        async with application.run_test(size=size):
            await application.workers.wait_for_complete()
            cards = list(application.query(hud_app.ProviderCard))
            titles = [c.border_title for c in cards]
            gauges = {t: [widget_text(w) for w in c.query(".hud-gaugeline")]
                     for t, c in zip(titles, cards)}
            cards_display = application.query_one("#hud-cards").display
            body_wrap_display = application.query_one("#hud-body-wrap").display
            readat = widget_text(application.query_one("#hud-readat"))
            has_header = bool(application.query("Header"))
        return titles, gauges, cards_display, body_wrap_display, readat, has_header

    return asyncio.run(_go())


def test_no_stock_header(make_cfg):
    *_rest, has_header = _mount_window(make_cfg(), size=(200, 50))
    assert has_header is False


def test_wide_window_shows_two_provider_cards_side_by_side(make_cfg):
    titles, gauges, cards_display, body_wrap_display, readat, _hdr = _mount_window(
        make_cfg(), size=(200, 50)
    )
    assert cards_display is True
    assert body_wrap_display is False
    assert any("CLAUDE" in (t or "") for t in titles)
    assert any("CODEX" in (t or "") for t in titles)
    claude_title = next(t for t in titles if "CLAUDE" in t)
    codex_title = next(t for t in titles if "CODEX" in t)
    assert "42%" in "\n".join(gauges[claude_title])
    assert "81%" in "\n".join(gauges[codex_title])
    assert readat.startswith("read at")


def test_narrow_window_still_shows_the_render_lines_body(make_cfg):
    _titles, _gauges, cards_display, body_wrap_display, _readat, _hdr = _mount_window(
        make_cfg(), size=(80, 30)
    )
    assert cards_display is False
    assert body_wrap_display is True


def test_exactly_at_the_phone_breakpoint_shows_cards(make_cfg):
    # PHONE_WIDTH_BREAKPOINT itself is desk width, same rule as everywhere else in.
    _titles, _gauges, cards_display, body_wrap_display, _readat, _hdr = _mount_window(
        make_cfg(), size=(hud_app.PHONE_WIDTH_BREAKPOINT, 30)
    )
    assert cards_display is True
    assert body_wrap_display is False


# ---- keys -----------------------------------------------------------------

# ---- clock format setting ------------------------

def test_format_clock_defaults_to_24h():
    when = datetime(2026, 9, 2, 19, 11, tzinfo=timezone.utc).astimezone(timezone.utc)
    assert format_clock(when) == when.astimezone().strftime("%H:%M")


def test_format_clock_12h_mode_drops_the_leading_zero_and_lowercases_the_suffix():
    morning = datetime(2026, 9, 2, 7, 11, tzinfo=timezone.utc)
    assert format_clock(morning, "12h") == morning.astimezone().strftime("%I:%M %p").lstrip("0").lower()


def test_format_clock_none_is_a_question_mark():
    assert format_clock(None) == "?"
    assert format_clock(None, "12h") == "?"


def test_format_clock_accepts_a_config_or_an_appearance(make_cfg):
    cfg = make_cfg()  # default: 24h
    twelve = config.Config(appearance={"clock": "12h"})
    when = datetime(2026, 9, 2, 19, 11, tzinfo=timezone.utc)
    assert format_clock(when, cfg) == when.astimezone().strftime("%H:%M")
    assert format_clock(when, twelve) == format_clock(when, "12h")
    assert format_clock(when, twelve.appearance_settings()) == format_clock(when, "12h")


def test_desk_cards_use_the_twelve_hour_clock_when_set(tmp_path):
    cfg24 = config.Config(state_dir=str(tmp_path / "24" / "state"))
    cfg12 = config.Config(state_dir=str(tmp_path / "12" / "state"), appearance={"clock": "12h"})
    for cfg in (cfg24, cfg12):
        target = Path(cfg.hud_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(_picture()), encoding="utf-8")

    async def _foot_text(cfg):
        async def _go():
            application = hud_app.HudApp(cfg, start_refresher=False)
            async with application.run_test(size=(200, 12)):
                await application.workers.wait_for_complete()
                card = application.query_one("#hud-window-card-claude", hud_app.ProviderCard)
                return widget_text(application.query_one(f"#{card.id}-foot1"))
        return await _go()

    foot24 = asyncio.run(_foot_text(cfg24))
    foot12 = asyncio.run(_foot_text(cfg12))
    assert foot24 != foot12
    assert ("am" in foot12 or "pm" in foot12) and ("am" not in foot24 and "pm" not in foot24)


# ---- hide fields on the budget card --------------

def _mount_card(usage=None, hide=None, size=(60, 12)):
    async def _go():
        cfg = config.Config(appearance={"hide": hide or []})

        class _Host(hud_app.App):
            def compose(self):
                yield hud_app.ProviderCard("claude", usage or _picture()["claude"], cfg, id="c",
                                           history=_picture()["history"]["claude_pct"])

        app = _Host()
        async with app.run_test(size=size):
            card = app.query_one(hud_app.ProviderCard)
            foot1 = widget_text(app.query_one("#c-foot1"))
            foot2 = widget_text(app.query_one("#c-foot2"))
            week_display = app.query_one("#c-wk").display
            sparkrow_display = app.query_one("#c-sparkrow").display
        return foot1, foot2, week_display, sparkrow_display

    return asyncio.run(_go())


def test_hide_drops_today_from_the_foot_line():
    foot1, _foot2, _wk, _spark = _mount_card()
    assert "today" in foot1
    foot1_hidden, _foot2, _wk, _spark = _mount_card(hide=["today"])
    assert "today" not in foot1_hidden


def test_hide_drops_burn_and_resets():
    foot1, _f2, _wk, _spark = _mount_card(hide=["burn", "resets"])
    assert "burn" not in foot1 and "resets " not in foot1
    assert "today" in foot1


def test_hide_drops_resets_in_runs_out_and_memory():
    usage = dict(_picture()["claude"])
    usage["context_pct"] = 40.0
    _f1, foot2, _wk, _spark = _mount_card(usage=usage, hide=["resets_in", "runs_out", "memory"])
    assert "resets in" not in foot2 and "runs out" not in foot2 and "memory" not in foot2


# ---- caution colour when the budget runs out before the reset -

def _style_at(text, substring):
    idx = text.plain.find(substring)
    assert idx != -1, f"{substring!r} not found in {text.plain!r}"
    for span in text.spans:
        if span.start <= idx < span.end:
            return span.style
    return None


def test_burn_and_runs_out_get_the_caution_colour_when_the_limit_falls_inside_the_window():
    usage = dict(_picture()["claude"])
    usage["five_hour_pct"] = 50.0
    usage["burn_pct_per_hour"] = 40.0
    usage["five_hour_resets_at"] = (
        datetime.now(timezone.utc) + timedelta(hours=3)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    card = hud_app.ProviderCard("claude", usage)
    left = hud_calc.time_to_limit(50.0, 40.0, usage["five_hour_resets_at"])
    runs_out = hud_calc.format_time_to_limit(left, usage["five_hour_resets_at"])
    warning = f"bold {theme.TOKENS['warning']}"

    assert _style_at(card._foot1(90), "40%/h") == warning
    assert _style_at(card._foot2(90), runs_out) == warning


def test_burn_and_runs_out_stay_plain_when_the_window_resets_first():
    usage = dict(_picture()["claude"])
    usage["five_hour_pct"] = 50.0
    usage["burn_pct_per_hour"] = 1.0
    usage["five_hour_resets_at"] = (
        datetime.now(timezone.utc) + timedelta(minutes=2)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    card = hud_app.ProviderCard("claude", usage)

    assert _style_at(card._foot1(90), "1%/h") == "bold"
    assert _style_at(card._foot2(90), "after the reset") == "bold"


def test_hide_week_hides_the_weekly_gauge_row():
    _f1, _f2, week_display, _spark = _mount_card()
    assert week_display is True
    _f1, _f2, week_display_hidden, _spark = _mount_card(hide=["week"])
    assert week_display_hidden is False


def test_hide_trend_hides_the_sparkline_row():
    _f1, _f2, _wk, sparkrow_display = _mount_card()
    assert sparkrow_display is True
    _f1, _f2, _wk, sparkrow_hidden = _mount_card(hide=["trend"])
    assert sparkrow_hidden is False


# ---- gauge fill style: "solid" (default) vs "blocks" -----------------

def _mount_card_gauge(gauge=None, usage=None, size=(60, 12)):
    async def _go():
        appearance = {"gauge": gauge} if gauge is not None else {}
        cfg = config.Config(appearance=appearance)

        class _Host(hud_app.App):
            def compose(self):
                yield hud_app.ProviderCard("claude", usage or _picture()["claude"], cfg, id="c")

        app = _Host()
        async with app.run_test(size=size):
            five = widget_text(app.query_one("#c-5h"))
            week = widget_text(app.query_one("#c-wk"))
        return five, week

    return asyncio.run(_go())


def test_gauge_defaults_to_solid_fill():
    five, week = _mount_card_gauge(gauge=None)
    assert "█" in five or "█" in week   # bar_full ("█") -- the default look
    assert "▉" not in five and "▉" not in week   # bar_block ("▉") absent


def test_gauge_blocks_setting_swaps_the_fill_glyph():
    five, week = _mount_card_gauge(gauge="blocks")
    assert "▉" in five or "▉" in week   # bar_block ("▉") -- the hairline-separated cell
    assert "█" not in five and "█" not in week   # bar_full ("█") absent


def test_gauge_blocks_keeps_the_same_percent_text_as_solid():
    # Only the fill texture changes -- filled width, percent readout, and band ticks match.
    usage = dict(_picture()["claude"])
    solid_five, solid_week = _mount_card_gauge(gauge="solid", usage=usage)
    blocks_five, blocks_week = _mount_card_gauge(gauge="blocks", usage=usage)
    assert solid_five.split()[-1] == blocks_five.split()[-1]
    assert solid_week.split()[-1] == blocks_week.split()[-1]


def test_render_block_narrow_phone_tier_honours_gauge_setting():
    # The one-line phone-tier row degrades exactly like the wide card: same
    # bullet-graph, only the fill glyph changes with `[appearance] gauge`.
    usage = dict(_picture()["claude"])
    solid_cfg = config.Config(appearance={})
    blocks_cfg = config.Config(appearance={"gauge": "blocks"})
    solid_line = hud_app.render_block_narrow(usage, solid_cfg, 60)
    blocks_line = hud_app.render_block_narrow(usage, blocks_cfg, 60)
    assert "█" in solid_line and "▉" not in solid_line
    assert "▉" in blocks_line and "█" not in blocks_line


def test_render_block_wide_strip_honours_gauge_setting():
    usage = dict(_picture()["claude"])
    solid_cfg = config.Config(appearance={})
    blocks_cfg = config.Config(appearance={"gauge": "blocks"})
    solid_lines = "\n".join(hud_app.render_block(usage, solid_cfg, 100))
    blocks_lines = "\n".join(hud_app.render_block(usage, blocks_cfg, 100))
    assert "█" in solid_lines and "▉" not in solid_lines
    assert "▉" in blocks_lines and "█" not in blocks_lines


def test_help_text_documents_the_hide_field_names():
    assert "hide" in hud_app.HELP_TEXT
    for name in ("today", "burn", "resets", "resets_in", "runs_out", "memory", "as_of", "trend", "week"):
        assert name in hud_app.HELP_TEXT


def test_help_text_does_not_get_eaten_as_rich_markup(make_cfg):
    # `[appearance]` in HELP_TEXT must render literally, not be swallowed as a Rich markup tag.
    cfg = make_cfg()

    async def _go():
        application = hud_app.HudApp(cfg, start_refresher=False)
        async with application.run_test(size=(80, 45)) as pilot:
            await pilot.press("question_mark")
            return widget_text(application.query_one("#hud-help"))

    text = asyncio.run(_go())
    assert "[appearance]" in text


# ---- HISTORY panel --------------------------------

def _mount_history(tmp_path, size=(200, 50)):
    async def _go():
        cfg = config.Config(state_dir=str(tmp_path / "state"))
        target = Path(cfg.hud_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(_picture()), encoding="utf-8")
        application = hud_app.HudApp(cfg, start_refresher=False)
        async with application.run_test(size=size):
            await application.workers.wait_for_complete()
            history_display = application.query_one("#hud-history").display
            help_display = application.query_one("#hud-help").display
            claude_stats = widget_text(application.query_one("#hud-history-claude-stats"))
            codex_stats = widget_text(application.query_one("#hud-history-codex-stats"))
        return history_display, help_display, claude_stats, codex_stats

    return asyncio.run(_go())


def test_history_panel_shown_and_filled_at_desk_size(tmp_path):
    history_display, help_display, claude_stats, codex_stats = _mount_history(tmp_path, size=(200, 50))
    assert history_display is True
    assert help_display is True   # >= 40 rows tall: help shown by default (item 4)
    assert "min" in claude_stats and "max" in claude_stats and "last" in claude_stats
    assert "no samples yet" in codex_stats   # collector never wrote codex_pct history


def test_history_panel_and_default_help_hidden_below_forty_rows(tmp_path):
    history_display, help_display, _c, _x = _mount_history(tmp_path, size=(200, 30))
    assert history_display is False
    assert help_display is False   # unchanged from today's behaviour


def test_narrow_window_never_shows_the_history_panel(tmp_path):
    history_display, _help, _c, _x = _mount_history(tmp_path, size=(80, 50))
    assert history_display is False


def test_help_overlay_toggles_and_quit_works(make_cfg):
    cfg = make_cfg()

    async def _go():
        application = hud_app.HudApp(cfg, start_refresher=False)
        async with application.run_test(size=(80, 30)) as pilot:
            help_widget = application.query_one("#hud-help")
            assert help_widget.display is False
            await pilot.press("question_mark")
            assert help_widget.display is True
            await pilot.press("question_mark")
            assert help_widget.display is False
            await pilot.press("r")
            message = widget_text(application.query_one("#hud-message"))
        return message

    assert "usage" in asyncio.run(_go())
