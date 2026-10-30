"""Browser lane (docs/browser-lane.md): fakes stand in for Windows, no real browser starts."""
import json
import re

import pytest

from pantheon import browser_lane as bl

CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe"
FIREFOX = "C:/Program Files/Mozilla Firefox/firefox.exe"


class FakeWin:
    def __init__(self, session=0, console=1):
        self.session, self.console = session, console
        self.procs, self.sockets, self.windows = {}, {}, set()
        self.killed = []
        self.clock = 10 ** 17

    def session_id(self):
        return self.session

    def console_session(self):
        return self.console

    def process_info(self, pid):
        return self.procs.get(pid)

    def terminate(self, pid, created=None):
        if created is not None and getattr(self.procs.get(pid), "created", None) != created:
            return False
        self.killed.append(pid)
        self.procs.pop(pid, None)
        return True

    def visible_window_pids(self):
        return set(self.windows)

    def listeners(self, port):
        return list(self.sockets.get(port, []))

    def now(self):
        return self.clock


class FakeSpawn:
    """Starts a pretend browser: a process, plus a listener when the command line asks for one."""

    def __init__(self, win, address="127.0.0.1", image=None):
        self.win, self.address, self.image = win, address, image
        self.calls = []
        self.next_pid = 5000

    def __call__(self, argv, cwd):
        self.calls.append(argv)
        self.next_pid += 1
        pid = self.next_pid
        image = self.image or argv[0]
        self.win.procs[pid] = bl.ProcInfo(image=image, created=self.win.clock + 1)
        text = " ".join(argv)
        port = re.search(r"--remote-debugging-port[= ](\d+)", text)
        if port:
            self.win.sockets[int(port.group(1))] = [(self.address, pid)]
        else:
            self.win.windows.add(pid)
        return pid


@pytest.fixture
def local(tmp_path):
    """A pretend %LOCALAPPDATA% that lives in the test's temp folder, in Windows form."""
    folder = tmp_path / "Local"
    folder.mkdir()
    return bl.mixed(str(folder))


def make_lane(tmp_path, local, win=None, env_extra=None, spawn=None, exe=CHROME):
    win = win or FakeWin()
    env = {"LOCALAPPDATA": local, "APPDATA": local + "/../Roaming", "USERPROFILE": local + "/.."}
    env.update(env_extra or {})
    lane = bl.Lane(str(tmp_path / "state"), win=win, env=env, spawn=spawn or FakeSpawn(win),
                   free_port=lambda: 51234, cdp_version=lambda port: {"Browser": "Chrome/155"},
                   sleep=lambda s: None)
    lane.find_exe = lambda browser: FIREFOX if browser == "firefox" else exe
    lane.profile_locked = lambda kind, path: False
    return lane


# ---------------------------------------------------------------- the profile is never the normal one

@pytest.mark.parametrize("browser", ["chrome", "msedge", "firefox"])
def test_profile_lives_under_the_lane_root_never_the_everyday_profile(tmp_path, local, browser):
    lane = make_lane(tmp_path, local)
    path = lane.profile_dir(browser)
    assert bl.inside(path, local + "/pantheon/browser-profiles")
    for normal in bl.NORMAL_PROFILES:
        normal = bl.expand(normal, lane.env)
        assert not bl.inside(path, normal) and not bl.inside(normal, path)


@pytest.mark.parametrize("root,name", [
    ("%LOCALAPPDATA%/Google/Chrome/User Data", "default"),
    ("%LOCALAPPDATA%/Microsoft/Edge/User Data", "default"),
    ("%APPDATA%/Mozilla/Firefox/Profiles", "abc"),
    ("%LOCALAPPDATA%", "google"),   # a profile folder wrapped around the everyday one
])
def test_refuses_a_profile_that_overlaps_an_everyday_profile(tmp_path, local, root, name):
    lane = make_lane(tmp_path, local)
    lane.env["PANTHEON_BROWSER_PROFILES"] = bl.expand(root, lane.env)
    with pytest.raises(bl.LaneError, match="everyday profile"):
        lane.profile_dir(name)


@pytest.mark.parametrize("name", ["../chrome", "Default", "a b", "", "x" * 40, "c:\\x"])
def test_rejects_profile_names_that_could_escape_the_root(tmp_path, local, name):
    with pytest.raises(bl.LaneError):
        make_lane(tmp_path, local).profile_dir(name)


def test_start_passes_the_lane_profile_never_a_default_one(tmp_path, local):
    win = FakeWin()
    spawn = FakeSpawn(win)
    lane = make_lane(tmp_path, local, win=win, spawn=spawn)
    lane.start("chrome")
    argv = spawn.calls[0]
    data_dirs = [a for a in argv if a.startswith("--user-data-dir=")]
    assert data_dirs == ["--user-data-dir=" + bl.backslash(local + "/pantheon/browser-profiles/chrome")]
    assert not any("User Data" in a for a in argv)


# ---------------------------------------------------------------- the port stays on 127.0.0.1

def test_start_binds_loopback_and_records_the_browser(tmp_path, local):
    win = FakeWin()
    spawn = FakeSpawn(win)
    lane = make_lane(tmp_path, local, win=win, spawn=spawn)
    record = lane.start("chrome")
    argv = spawn.calls[0]
    assert "--remote-debugging-port=51234" in argv
    assert "--remote-debugging-address=127.0.0.1" in argv
    assert "--headless=new" in argv
    assert record["endpoint"] == "http://127.0.0.1:51234"
    assert record["pid"] == 5001 and record["listening"] == ["127.0.0.1"]
    saved = json.loads((tmp_path / "state" / "browser" / "chrome.json").read_text())
    assert saved["pid"] == 5001 and saved["created"] == win.clock + 1


@pytest.mark.parametrize("address", ["0.0.0.0", "[::]", "100.64.0.9", "192.168.0.20"])
def test_start_stops_a_browser_that_listens_beyond_loopback(tmp_path, local, address):
    win = FakeWin()
    lane = make_lane(tmp_path, local, win=win, spawn=FakeSpawn(win, address=address))
    with pytest.raises(bl.LaneError, match="not only 127.0.0.1"):
        lane.start("chrome")
    assert win.killed == [5001]
    assert not (tmp_path / "state" / "browser" / "chrome.json").exists()


def test_start_never_adopts_or_kills_a_stranger_on_the_port(tmp_path, local):
    win = FakeWin()
    lane = make_lane(tmp_path, local, win=win, spawn=FakeSpawn(win, image=r"C:\Tools\other.exe"))
    with pytest.raises(bl.LaneError, match="did not start"):
        lane.start("chrome")
    assert win.killed == []


def test_fixed_port_is_used_and_a_busy_one_refused(tmp_path, local):
    win = FakeWin()
    spawn = FakeSpawn(win)
    lane = make_lane(tmp_path, local, win=win, spawn=spawn)
    assert lane.start("chrome", port=9333)["endpoint"] == "http://127.0.0.1:9333"
    win.sockets[9444] = [("127.0.0.1", 42)]
    with pytest.raises(bl.LaneError, match="already in use"):
        lane.start("msedge", port=9444)
    assert win.killed == [] and len(spawn.calls) == 1


def test_parse_netstat_reads_ipv4_and_ipv6_listeners():
    text = """
  Proto  Local Address          Foreign Address        State           PID
  TCP    127.0.0.1:51234        0.0.0.0:0              LISTENING       5001
  TCP    127.0.0.1:51234        127.0.0.1:60000        ESTABLISHED     5001
  TCP    0.0.0.0:22             0.0.0.0:0              LISTENING       4912
  TCP    [::1]:51234            [::]:0                 LISTENING       5001
  UDP    127.0.0.1:51234        *:*                                    77
"""
    assert bl.parse_netstat(text, 51234) == [("127.0.0.1", 5001), ("[::1]", 5001)]
    assert all(bl.is_loopback(a) for a, _ in bl.parse_netstat(text, 51234))
    assert not bl.is_loopback("0.0.0.0") and not bl.is_loopback("[::]")


def test_status_flags_a_listener_that_is_not_loopback(tmp_path, local):
    win = FakeWin()
    lane = make_lane(tmp_path, local, win=win)
    lane.start("chrome")
    win.sockets[51234].append(("0.0.0.0", 5001))
    row = lane.status("chrome")[0]
    assert row["running"] and row["loopback_only"] is False


# ---------------------------------------------------------------- stop ends only the recorded PID

def test_stop_kills_only_the_recorded_pid(tmp_path, local):
    win = FakeWin()
    lane = make_lane(tmp_path, local, win=win)
    lane.start("chrome")
    win.procs[7777] = bl.ProcInfo(image=CHROME, created=1)  # the user's own Chrome, say
    assert "stopped PID 5001" in lane.stop("chrome")
    assert win.killed == [5001]
    assert 7777 in win.procs
    assert lane.read_state("chrome") is None


def test_stop_leaves_a_reused_pid_alone(tmp_path, local):
    win = FakeWin()
    lane = make_lane(tmp_path, local, win=win)
    lane.start("chrome")
    win.procs[5001] = bl.ProcInfo(image=CHROME, created=win.clock + 999)  # same number, new process
    message = lane.stop("chrome")
    assert "left it alone" in message and win.killed == []
    assert lane.read_state("chrome") is None


def test_stop_leaves_a_pid_reused_between_the_check_and_the_kill_alone(tmp_path, local):
    win = FakeWin()
    lane = make_lane(tmp_path, local, win=win)
    lane.start("chrome")
    real_info = win.process_info

    def swap_after_check(pid):
        info = real_info(pid)
        win.procs[pid] = bl.ProcInfo(image=CHROME, created=win.clock + 999)
        return info

    win.process_info = swap_after_check
    with pytest.raises(bl.LaneError, match="could not stop"):
        lane.stop("chrome")
    assert win.killed == []


def test_refuses_a_lane_profile_that_is_a_link_into_an_everyday_profile(tmp_path, local, monkeypatch):
    lane = make_lane(tmp_path, local)
    everyday = bl.expand("%LOCALAPPDATA%/Google/Chrome/User Data", lane.env)
    real = bl.os.path.realpath
    monkeypatch.setattr(bl.os.path, "realpath", lambda p: (
        bl.native(everyday) if bl._key(p).startswith(bl._key(lane.profile_root())) else real(p)))
    with pytest.raises(bl.LaneError, match="everyday profile"):
        lane.profile_dir("chrome")


def test_stop_with_no_record_or_a_finished_browser_kills_nothing(tmp_path, local):
    win = FakeWin()
    lane = make_lane(tmp_path, local, win=win)
    assert "nothing stopped" in lane.stop("chrome")
    lane.start("chrome")
    del win.procs[5001]
    assert "was not running" in lane.stop("chrome")
    assert win.killed == []


def test_start_twice_reuses_the_running_browser(tmp_path, local):
    win = FakeWin()
    spawn = FakeSpawn(win)
    lane = make_lane(tmp_path, local, win=win, spawn=spawn)
    lane.start("chrome")
    again = lane.start("chrome")
    assert again["note"] == "already running" and len(spawn.calls) == 1


def test_start_refuses_a_profile_another_window_holds(tmp_path, local):
    lane = make_lane(tmp_path, local)
    lane.profile_locked = lambda kind, path: True
    with pytest.raises(bl.LaneError, match="open in another browser window"):
        lane.start("chrome")


def test_firefox_uses_its_own_profile_flag_and_a_bidi_endpoint(tmp_path, local):
    win = FakeWin()
    spawn = FakeSpawn(win)
    lane = make_lane(tmp_path, local, win=win, spawn=spawn)
    record = lane.start("firefox")
    argv = spawn.calls[0]
    assert argv[argv.index("--profile") + 1] == bl.backslash(local + "/pantheon/browser-profiles/firefox")
    assert "--headless" in argv and "--no-remote" in argv
    assert record["endpoint"] == "ws://127.0.0.1:51234/session"


# ---------------------------------------------------------------- session 0 routes the sign-in

def test_login_from_session_0_prints_the_desk_line_and_starts_nothing(tmp_path, local):
    win = FakeWin(session=0, console=1)
    spawn = FakeSpawn(win)
    lane = make_lane(tmp_path, local, win=win, spawn=spawn)
    result = lane.login("https://events.example/events")
    assert result["mode"] == "desk_line" and spawn.calls == []
    line = result["line"]
    assert line.startswith("& 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe' ")
    assert "'https://events.example/events'" in line
    assert "remote-debugging" not in line and "headless" not in line
    assert bl.backslash(local + "/pantheon/browser-profiles/chrome") in line


def test_login_from_a_session_that_is_not_the_console_also_prints_the_line(tmp_path, local):
    win = FakeWin(session=2, console=1)
    lane = make_lane(tmp_path, local, win=win)
    assert lane.login("https://example.com")["mode"] == "desk_line"


def test_login_on_the_desk_opens_a_window_with_no_debugging_port(tmp_path, local):
    win = FakeWin(session=1, console=1)
    spawn = FakeSpawn(win)
    lane = make_lane(tmp_path, local, win=win, spawn=spawn)
    result = lane.login("https://events.example/login")
    assert result["mode"] == "opened" and result["pid"] == 5001
    argv = spawn.calls[0]
    assert argv[-1] == "https://events.example/login"
    assert not any("remote-debugging" in a or "headless" in a for a in argv)
    assert win.sockets == {}


def test_headed_start_is_refused_from_session_0(tmp_path, local):
    lane = make_lane(tmp_path, local, win=FakeWin(session=0))
    with pytest.raises(bl.LaneError, match="invisible"):
        lane.start("chrome", headed=True)


def test_headed_start_is_allowed_on_the_desk(tmp_path, local):
    win = FakeWin(session=1, console=1)
    spawn = FakeSpawn(win)
    lane = make_lane(tmp_path, local, win=win, spawn=spawn)
    lane.start("chrome", headed=True)
    assert "--headless=new" not in spawn.calls[0]


def test_login_waits_for_the_lane_browser_to_stop(tmp_path, local):
    win = FakeWin(session=1, console=1)
    lane = make_lane(tmp_path, local, win=win)
    lane.start("chrome")
    with pytest.raises(bl.LaneError, match="browser stop"):
        lane.login("https://example.com")


@pytest.mark.parametrize("url", ["file:///C:/Users/x/secret.txt", "javascript:alert(1)",
                                 "chrome://settings", "https://a.com/x'y", "events.example"])
def test_login_takes_only_plain_web_addresses(tmp_path, local, url):
    with pytest.raises(bl.LaneError, match="http"):
        make_lane(tmp_path, local).login(url)


def test_powershell_line_keeps_dollar_signs_literal():
    line = bl.powershell_line(["C:\\x\\chrome.exe", "--user-data-dir=C:\\p", "https://a.com/?q=$env:USERNAME"])
    assert line == "& 'C:\\x\\chrome.exe' '--user-data-dir=C:\\p' 'https://a.com/?q=$env:USERNAME'"


def test_cli_login_from_session_0_prints_the_line(tmp_path, local, capsys):
    lane = make_lane(tmp_path, local, win=FakeWin(session=0))
    assert bl.main(["login", "https://example.com"], lane=lane) == 0
    out = capsys.readouterr().out
    assert "PowerShell on the desk" in out and out.strip().splitlines()[-1].startswith("& '")


def test_cli_start_prints_only_the_endpoint_on_stdout(tmp_path, local, capsys):
    lane = make_lane(tmp_path, local)
    assert bl.main(["start"], lane=lane) == 0
    assert capsys.readouterr().out == "http://127.0.0.1:51234\n"
