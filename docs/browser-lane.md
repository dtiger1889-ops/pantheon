# Browser lane: how an SSH-side agent drives a signed-in browser

For any session in the pantheon tmux (Windows session 0) that needs a web page you are signed in
to. The lane browser runs on its own profile under `%LOCALAPPDATA%/pantheon/browser-profiles/`,
never your everyday Chrome, Edge or Firefox profile.

Never open your normal browser from this side: no `start`/`explorer`/`rundll32` URL opens, no
`chrome.exe` without the lane's profile. Those run invisibly in session 0 and can lock the
everyday browser profile until the hidden process is ended.

## 1. Start it, use it, stop it

```
bin/browser start                 # Chrome, headless; prints http://127.0.0.1:<port>
bin/browser status                # what is running, its PID and endpoint
bin/browser stop                  # ends only the PID it recorded
```

`--browser msedge` or `--browser firefox` pick another browser (each has its own profile unless
`--profile NAME` says otherwise). `--port N` fixes the port, for an MCP config. Stop the browser when
the task is done: while it runs, any program on this PC can drive it through the port.

## 2. Attach

Python Playwright attaches to the running lane browser without downloading a browser of its own:

```python
# run with: <a Python that has playwright installed> script.py <endpoint>
import sys
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(sys.argv[1])
    context = browser.contexts[0]          # the lane profile: its cookies and sign-ins
    page = context.new_page()
    page.goto("https://example.com")
    print(page.inner_text("body")[:2000])
    page.close()                           # close your tab; leave the browser to `bin/browser stop`
```

This works for Chrome and Edge. Firefox speaks WebDriver BiDi (`ws://127.0.0.1:<port>/session`),
which needs Puppeteer, so use Chrome unless a site requires Firefox.

Headless Chrome names itself `HeadlessChrome` in its user agent. If a site refuses it, set a normal
user agent from the attaching client, or ask for the page from a desk session with `--headed`.

## 3. When the page needs a sign-in

The profile has to be signed in once, by you, on the desk. Run:

```
bin/browser login https://example.com
```

From session 0 it starts nothing and prints one PowerShell line. Run that line in a desk PowerShell
as-is (it opens a normal window on the lane profile, with no debugging port open while you type).
Sign in and close the window; after that, `bin/browser start` here has your sign-in. From a desk
session the same command opens the window itself. `login` refuses while the lane browser for that
profile is running, and `start` refuses while a sign-in window holds the profile.

## 4. Browser tools as an MCP server (optional, not set up by Pantheon)

Playwright MCP or Chrome DevTools MCP would give agents click/type/read tools instead of scripts.
Both run through `npx` (a download) and need a settings change:

```
claude mcp add playwright -- npx @playwright/mcp@latest --cdp-endpoint http://127.0.0.1:9333
```

used with `bin/browser start --port 9333`. Chrome DevTools MCP takes
`--browser-url=http://127.0.0.1:9333` the same way.
