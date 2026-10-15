# Message the Assistant from Telegram: one-time setup

Send your Telegram bot a message and it goes to the pinned Assistant (the `◆ Assistant` on the
deck); the Assistant's answer comes back in the same chat. It answers only your chat.

Experimental: not yet run against a real bot.

## What goes where

1. **Chat id**: your own chat's id with the bot. Put it in `pantheon.toml`:

   ```toml
   [assistant.telegram]
   enabled = true
   chat_id = "<that number>"
   credential_target = "pantheon-telegram-bot"
   ```

   If `[notify.telegram]` already has `chat_id` and `credential_target`, you can leave those two
   lines out here; it uses those.

2. **Bot token**: from @BotFather. It goes in Windows Credential Manager, never in
   `pantheon.toml`. In a PowerShell window on the desk, this asks for the token without showing it
   or saving it in your command history:

   ```
   cmdkey /generic:pantheon-telegram-bot /user:telegram-bot /pass
   ```

   The name after `/generic:` must match `credential_target`.

3. **Start the poller.** It runs as its own window in the deck's tmux:

   ```
   tmux new-window -d -n TELEGRAM 'cd <pantheon folder> && .venv/bin/python -m pantheon.assistant_telegram'
   ```

   `pantheon` does not start it by itself yet.

## What it does

- Your message goes in as `(sent from Telegram) <your text>`, so the Assistant knows where it came
  from.
- It types into the Assistant only when the Assistant is idle with nothing typed and no question on
  its screen. Otherwise it tells you it did not send, and you try again in a minute.
- It waits up to 10 minutes for the answer (`reply_wait_seconds`). If the Assistant stops to ask
  permission, you get one message saying so. Answer the question on the deck, and the reply still
  comes to Telegram.
- It sends back only the Assistant's final reply, not the "let me check" lines before a tool call.
- Messages from any other chat are ignored with no reply. Messages sent more than 10 minutes
  before the poller started (while it was down) are not passed on; you get a note saying so.
- Text only: photos, voice notes and files get "Only text messages reach the Assistant for now."
- One message at a time. Messages you send while it waits for an answer are handled after that.

## Turning it off

Any one of these stops it: `[assistant.telegram] enabled = false`, `[assistant] enabled = false`,
or `PANTHEON_ASSISTANT=off` in its environment. It checks the last two before every poll and
exits by itself.

## If it doesn't work

- "could not read the bot token": no credential under the `credential_target` name. Run the
  `cmdkey` line again with the same name.
- `getUpdates: HTTP 409` in its window: something else is reading this bot's messages (a second
  poller, or a webhook set on the bot). Only one reader per bot is allowed.
- It logs one line per message to its window. It never logs message text, and never logs the token.
