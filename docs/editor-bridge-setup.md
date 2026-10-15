# VS Code from the deck: one-time setup

`V` on the deck opens the highlighted session's folder (or, from the task list, the row's note)
in VS Code on your monitor. The deck can't open a window on your screen by itself (it runs in the
SSH side of Windows), so it asks a Scheduled Task that runs in your desktop session to do it.
You register that task once.

## Register it (once)

Paste this into a normal PowerShell window on the desk (not inside the deck's tmux), with
`<pantheon folder>` replaced by where you cloned Pantheon. No admin needed; the task only runs
when the deck asks, never on a timer:

```
Register-ScheduledTask -TaskPath '\Pantheon\' -TaskName 'OpenEditor' -Action (New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "<pantheon folder>\bin\open_editor.ps1"') -Principal (New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive) -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries)
```

Then press `V` on the deck. A PowerShell window may flash for a moment before VS Code comes up.

## If it doesn't open

- The deck's bottom line says "VS Code bridge isn't set up": the task isn't registered, or you
  aren't logged on at the desk. Check with `Get-ScheduledTask -TaskPath '\Pantheon\'`.
- It says "VS Code's command line (code) isn't on this PC": add
  `vscode_cli = "C:\\path\\to\\code.cmd"` under `[editor]` in `pantheon.toml`.

## Remove it

```
Unregister-ScheduledTask -TaskPath '\Pantheon\' -TaskName 'OpenEditor' -Confirm:$false
```

How it works: the deck writes the VS Code command line to `state/editor/pending_open.json` and
runs the task (`pantheon/editor_bridge.py`); the task runs `bin/open_editor.ps1`, which runs only a
`code` command from that file and deletes it.
