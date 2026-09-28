# pantheon_event.ps1 -- Claude Code hook. Appends ONE JSON line per hook event to
# this checkout's state/agents/events.jsonl and nothing else. Finds its own checkout via
# $PSScriptRoot (this file's own folder) rather than a baked-in path, since a hook has no config
# file to read and Claude Code registers it by absolute path in <your ~/.claude>/settings.json.
# Never prints. Always exits 0 (fail-open). Registered for SessionStart, PostToolUse, Stop,
# SessionEnd and Notification (permission_prompt, idle_prompt, agent_needs_input,
# agent_completed, quota_auto_resume_*). Also meant for PermissionRequest:
# there it records the full tool_input (the exact command, or the path and edit) so the deck can
# show what is being asked, and it returns NO decision -- printing nothing leaves Claude Code's own
# dialog on screen exactly as it would be without Pantheon. Register it async so it never delays
# the prompt.
$ErrorActionPreference = 'SilentlyContinue'
try {
    $raw = [Console]::In.ReadToEnd()
    $j = $null
    if ($raw -and $raw.Trim().Length -gt 0) { $j = $raw | ConvertFrom-Json }
    $projectRoot = Split-Path -Parent $PSScriptRoot
    $dir = Join-Path $projectRoot 'state/agents'
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $msg = $null
    if ($j -and $j.message) { $msg = [string]$j.message; if ($msg.Length -gt 200) { $msg = $msg.Substring(0, 200) } }
    $detail = $null
    if ($j) { if ($j.source) { $detail = [string]$j.source } elseif ($j.reason) { $detail = [string]$j.reason } }
    $o = [ordered]@{
        ts                = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
        source            = 'claude'
        event             = $(if ($j) { $j.hook_event_name } else { 'unknown' })
        session_id        = $(if ($j) { $j.session_id } else { $null })
        cwd               = $(if ($j) { $j.cwd } else { $null })
        tool_name         = $(if ($j) { $j.tool_name } else { $null })
        notification_type = $(if ($j) { $j.notification_type } else { $null })
        message           = $msg
        detail            = $detail
        agent_id          = $(if ($j) { $j.agent_id } else { $null })
        permission_mode   = $(if ($j) { $j.permission_mode } else { $null })
        tmux_pane         = $env:TMUX_PANE
        transcript_path   = $(if ($j) { $j.transcript_path } else { $null })
    }
    if ($j -and $j.hook_event_name -eq 'PermissionRequest' -and $null -ne $j.tool_input) {
        $o['tool_input'] = $j.tool_input
    }
    $line = ($o | ConvertTo-Json -Compress -Depth 12)
    [System.IO.File]::AppendAllText("$dir/events.jsonl", $line + "`n", (New-Object System.Text.UTF8Encoding $false))
} catch { }
exit 0
