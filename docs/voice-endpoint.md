# Talk to the Assistant through a voice puck: one-time setup

Say the wake word to a puck, ask something, and the pinned Assistant (the `◆ Assistant` on the
deck) gets your words; the puck speaks its answer. Home Assistant does the listening and the
speaking. Pantheon's part is a small web endpoint that looks to Home Assistant like an
OpenAI-style chat server: it types what you said into the Assistant and hands back the reply.

Code: `pantheon/voice_endpoint.py`, tests in `tests/test_voice_endpoint.py`. Flashing the puck and
setting its wake word are Home Assistant's own setup and are not covered here.

## Pantheon side (a Claude session inside the deck can do steps 1, 2, 4 and 5)

1. **Make the token file**, outside the Pantheon folder. One line, at least 16 characters; Home
   Assistant sends it with every request. This makes a random one (replace the paths with your
   own):

   ```
   mkdir -p ~/.pantheon && <pantheon folder>/.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.pantheon/voice_token
   ```

   The endpoint refuses a token file inside the Pantheon folder, so it can never be committed.

2. **Turn it on in `pantheon.toml`.** `bind` is this PC's address on the home network (when Home
   Assistant runs on another machine, 127.0.0.1 can't be reached from there). Narrow `allow` to
   Home Assistant's machine and this PC:

   ```toml
   [voice]
   enabled = true
   bind = "<this PC's LAN address>"
   port = 8765
   token_file = "<your home folder>/.pantheon/voice_token"
   allow = ["127.0.0.1/32", "<Home Assistant's address>/32"]
   ```

   Every key is explained in `pantheon.example.toml` under `[voice]`. If another program already
   uses port 8765, pick another and use it everywhere below.

3. **Let Home Assistant through Windows Firewall** (needs an administrator PowerShell). The
   endpoint runs in the deck's tmux (Windows session 0), where no "allow this app?" prompt can
   appear, so the rule has to exist first. This allows only Home Assistant's machine, only on this
   port, only on a private network:

   ```
   New-NetFirewallRule -DisplayName "Pantheon voice endpoint" -Direction Inbound -Protocol TCP -LocalPort 8765 -RemoteAddress <Home Assistant's address> -Profile Private -Action Allow
   ```

4. **Check the settings** without opening anything to the network:

   ```
   cd <pantheon folder> && .venv/bin/python -m pantheon.voice_endpoint --check
   ```

5. **Start it**: `pantheon` does this itself. Every start, `--restart`, `--restart --all` and the
   start after a reboot opens a VOICE window when it is missing, as long as `[voice] enabled = true`
   and the Assistant is on; `--restart` also restarts it to pick up new code. By hand, if ever
   needed:

   ```
   tmux new-window -d -t pantheon -n VOICE 'cd <pantheon folder> && .venv/bin/python -m pantheon.voice_endpoint'
   ```

## Home Assistant side (in the Home Assistant web page)

The endpoint is written for Home Assistant's built-in **llama.cpp** integration, which arrived in
Home Assistant 2026.8; an older install needs an update first.

1. Settings > Devices & services > Add integration > **llama.cpp**.
   - URL: `http://<this PC's LAN address>:8765/v1`
   - API key: the one line in your token file
   - Model: `pantheon-assistant` (the only one offered). The agent shows up as "Pantheon Assistant".
   Setup sends two short test messages ("What is the capital of France?"); the endpoint answers
   those itself and never passes them to the Assistant.
2. In that agent's options, set **Control Home Assistant** to nothing selected. The endpoint passes
   along only what you said, so device lists and tools would just make each request bigger.
   Set **Instructions** to exactly this one line, so the endpoint knows which puck heard you:
   `pantheon-voice room={{ area_name(llm_context.device_id) }} puck={{ device_name(llm_context.device_id) }}`
   (anything else in Instructions is ignored).
3. Settings > Voice assistants > Add assistant, name it "Pantheon":
   - Conversation agent: **Pantheon Assistant** (the llama.cpp one).
   - Speech-to-text and text-to-speech: the Home Assistant Cloud ones already there.
   - **Prefer handling commands locally**: off, so everything you say goes whole to the Assistant.
     On means "turn off the lights" style commands stay with Home Assistant and only the rest
     reaches the Assistant.
4. On each puck's device page, set its assistant to **Pantheon**.

If Home Assistant can't be updated to 2026.8 or later: install **Extended OpenAI Conversation**
from HACS and give it the same address as its "Base Url" and the same token as its API key.

## What you hear

- Your words go in as `(said out loud to the <puck> puck in the <room>; answer in a few short spoken sentences) <what you said>`,
  so the Assistant knows to keep it short and which puck heard you. You see it in the Claude app
  like any other message.
- A puck listed under `[voice.speak_on]` stays quiet and its answer plays elsewhere, in Home
  Assistant Cloud's voice (`pc_voice` / `ha_voice`): `"pc"` plays it on this PC's speakers, a
  `media_player.x` entity on that Home Assistant speaker. `bin/voice_say.ps1` does the
  playing and logs outcomes, never words, to `%TEMP%\pantheon_voice_say.log`. If it can't start,
  the puck says the answer itself. On this PC it unmutes the speakers and sets them to 50% while
  the answer plays, then puts mute and volume back the way they were
  (`-SystemVolume` changes the 50, 0 leaves the speakers alone).
- The answer is read out with formatting, code and web links taken out. A long answer is cut at a
  sentence, followed by "The rest is in the Claude app."
- It types into the Assistant only when the Assistant is idle with nothing typed and no question
  on its screen. Otherwise the puck says it didn't send, and you try again in a minute.
- It waits up to 90 seconds (`reply_wait_seconds`). Past that, the puck says the Assistant is still
  working and the answer will be in the Claude app.
- If the Assistant stops to ask you something on its screen, the puck says so right away.
- One request at a time: asking a second puck while the first waits gets "I'm still working on the
  last thing you asked."
- Saying the same thing again within 20 seconds gets the same answer back without typing it twice
  (Home Assistant can resend a request on its own).

## Turning it off

`[voice] enabled = false` and restart the VOICE window, or just close that window. While
`[assistant] enabled = false` or `PANTHEON_ASSISTANT=off`, it keeps listening but types nothing in,
and the puck says the Assistant is switched off.

## If it doesn't work

- Home Assistant says it can't connect: the VOICE window isn't running, `bind` is 127.0.0.1, or the
  firewall rule is missing. The rule above is for a network Windows calls Private; if the home
  network is marked Public, the rule doesn't apply.
- "invalid auth" in Home Assistant: the API key doesn't match the token file (one line, no spaces).
- No answer at all, not even an error: the caller's address is outside `allow`. The endpoint closes
  the connection without a word to anything outside the list.
- It logs one line per request to its window (what happened, never what you said, never the token).
