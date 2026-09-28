"""The three senders. Every one returns `(ok, sentence)` and
NEVER raises -- R2's "silence beats a broken alarm": a bad network, a missing pwsh, a Telegram
API error all come back as `(False, "<one plain sentence>")` so `runner.py` can log one line and
move on, never crash the loop over a notification that failed to leave the building.

Credential handling (Telegram): the bot token is read ONCE PER CALL straight out of Windows
Credential Manager via `pwsh -NoProfile -Command`, by the credential's target name -- it is never
read into a Python variable that outlives the call, never logged, and never written to any file
(`pantheon.toml`, `state/`, anywhere). `[notify.telegram] credential_target` in `pantheon.toml`
names which stored credential to read (look it up in Windows Credential Manager,
`control /name Microsoft.CredentialManager`). Empty target -> `telegram` refuses to
send and says so, rather than guessing a name and silently failing against the wrong credential.
"""
from __future__ import annotations

import json
import logging
import subprocess
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger("pantheon.notify.channels")

NTFY_TIMEOUT_SECONDS = 5
TELEGRAM_TIMEOUT_SECONDS = 10
CREDENTIAL_READ_TIMEOUT_SECONDS = 10
TOAST_SCRIPT = "hooks/pantheon_toast.ps1"

# A single line of PowerShell (no external module dependency -- `CredentialManager` from the
# Gallery may not be installed) that reads one generic Windows credential's password via the
# same Win32 CredRead the CredentialManager module itself wraps, and prints ONLY the password to
# stdout. `-Command` (not `-File`): short enough to pass inline, and nothing here ever touches a
# file. `__TARGET__` is substituted by `_read_credential` via plain `str.replace` (never
# `str.format`, which would choke on every literal `{`/`}` in the C# block below) with a properly
# quoted PowerShell string literal -- never raw text from a config file spliced into shell syntax.
_CRED_TARGET_PLACEHOLDER = "__TARGET__"
_CRED_READ_SCRIPT = """
$ErrorActionPreference = 'Stop'
Add-Type -Namespace PantheonCred -Name Native -MemberDefinition @"
[StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
public struct CREDENTIAL {
    public int Flags;
    public int Type;
    public string TargetName;
    public string Comment;
    public long LastWritten;
    public int CredentialBlobSize;
    public IntPtr CredentialBlob;
    public int Persist;
    public int AttributeCount;
    public IntPtr Attributes;
    public string TargetAlias;
    public string UserName;
}
[DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
public static extern bool CredRead(string target, int type, int flags, out IntPtr credentialPtr);
[DllImport("advapi32.dll", SetLastError = true)]
public static extern void CredFree(IntPtr cred);
"@
$target = __TARGET__
$credPtr = [IntPtr]::Zero
$ok = [PantheonCred.Native]::CredRead($target, 1, 0, [ref]$credPtr)
if (-not $ok) { exit 1 }
try {
    $credType = [PantheonCred.Native+CREDENTIAL]
    $cred = [Runtime.InteropServices.Marshal]::PtrToStructure($credPtr, $credType)
    $bytes = New-Object byte[] $cred.CredentialBlobSize
    [Runtime.InteropServices.Marshal]::Copy($cred.CredentialBlob, $bytes, 0, $cred.CredentialBlobSize)
    [Console]::Out.Write([Text.Encoding]::Unicode.GetString($bytes))
} finally {
    [PantheonCred.Native]::CredFree($credPtr)
}
"""


def _ps_quote(value: str) -> str:
    """A single-quoted PowerShell string literal -- doubling embedded `'` is the whole escaping
    rule for single-quoted strings, and it is never interpreted, so this is safe for any target
    name without needing to reason about shell metacharacters."""
    return "'" + str(value).replace("'", "''") + "'"


def _read_credential(target: str, pwsh: str, run=subprocess.run) -> Optional[str]:
    """The stored password for `target`, or `None` on ANY failure (no credential by that name,
    pwsh missing, the read timed out) -- never raised, never logged (the value itself never is,
    either)."""
    if not target:
        return None
    script = _CRED_READ_SCRIPT.replace(_CRED_TARGET_PLACEHOLDER, _ps_quote(target))
    try:
        result = run([pwsh, "-NoProfile", "-Command", script],
                     capture_output=True, text=True, timeout=CREDENTIAL_READ_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or "")
    return value if value else None


# --------------------------------------------------------------------------- 1. toast


def toast(title: str, body: str, pwsh: str, script_path: str = TOAST_SCRIPT,
          popen=subprocess.Popen) -> tuple[bool, str]:
    """Start the toast script and move on -- `Popen`, never `.wait`/`.communicate`, so a
    slow or hung PowerShell process never stalls the 2-second loop."""
    try:
        popen(
            [pwsh, "-NoProfile", "-File", script_path, "-Title", title, "-Body", body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        return False, f"toast failed to start: {exc}"
    return True, "toast started"


# --------------------------------------------------------------------------- 2. ntfy


def ntfy(url: str, topic: str, title: str, body: str, priority: str = "default",
        timeout: int = NTFY_TIMEOUT_SECONDS, opener=urllib.request.urlopen) -> tuple[bool, str]:
    """POST to a self-hosted ntfy server: `url` is the server's
    own address (e.g. the Tailscale IP), `topic` the channel the user's phone is subscribed to.
    Empty `url` means ntfy is not configured -- refuse cleanly rather than trying `http:///...`."""
    if not url:
        return False, "ntfy: no server url configured"
    target = url.rstrip("/") + "/" + topic.lstrip("/")
    req = urllib.request.Request(
        target, data=body.encode("utf-8"), method="POST",
        headers={"Title": title, "Priority": priority, "Tags": "pantheon"},
    )
    try:
        with opener(req, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
            return ok, ("ntfy sent" if ok else f"ntfy returned HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        return False, f"ntfy HTTP error {exc.code}"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, f"ntfy failed: {exc.reason if hasattr(exc, 'reason') else exc}"


# --------------------------------------------------------------------------- 3. telegram


def telegram(text: str, chat_id: str, credential_target: str, pwsh: str,
             timeout: int = TELEGRAM_TIMEOUT_SECONDS, run=subprocess.run,
             opener=urllib.request.urlopen) -> tuple[bool, str]:
    """Bot API `sendMessage`. The token is read fresh from Windows Credential
    Manager for this call only (see module docstring) and is never assigned to anything the
    caller can see -- it lives in this function's local scope for exactly as long as the HTTP
    request needs it."""
    if not chat_id:
        return False, "telegram: no chat_id configured"
    if not credential_target:
        return False, "telegram: no credential_target configured (see channels.py docstring)"
    token = _read_credential(credential_target, pwsh, run=run)
    if not token:
        return False, f"telegram: could not read credential '{credential_target}'"
    body = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body, method="POST", headers={"Content-Type": "application/json"},
    )
    del token  # out of scope for the rest of this call on purpose; nothing below needs it again
    try:
        with opener(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
            ok = 200 <= resp.status < 300 and bool(payload.get("ok"))
            return ok, ("telegram sent" if ok else f"telegram API error: {payload.get('description', '?')}")
    except urllib.error.HTTPError as exc:
        return False, f"telegram HTTP error {exc.code}"
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        return False, f"telegram failed: {exc}"
