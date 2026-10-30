# Speak a voice-puck answer somewhere other than the puck (pantheon/voice_endpoint.py say_elsewhere).
#   -Target pc   this PC's speakers: Home Assistant Cloud makes the audio (AvaNeural, an Azure
#                voice), MediaPlayer plays it
#   -Target ha   a Home Assistant media player (-Entity media_player.x) through tts.speak, Cloud voice
# Both use Home Assistant Cloud rather than edge-tts: edge-tts 7.2.8 got a 403 from Microsoft
# when this was written.
# Home Assistant is reached at -HaBaseUrl with a long-lived access token saved by
# `Read-Host -AsSecureString | Export-Clixml <-HaTokenStore>` (readable only by the same Windows user).
# The words come in -TextFile, which this script deletes. It logs outcomes only, never the words, to
# %TEMP%\pantheon_voice_say.log. A newer answer on the PC stops the one still playing; with
# -NoPreempt (a status line) it is skipped instead while another answer plays. -StatusFile gets one
# word per stage for the endpoint to wait on: "playing" (sound started), "done" (sound finished),
# "skip" (left out under -NoPreempt) or "fail". Every step has a deadline, so the script always ends.
param(
  [Parameter(Mandatory)][ValidateSet('pc', 'ha')][string]$Target,
  [Parameter(Mandatory)][string]$TextFile,
  [string]$Entity = '',
  [string]$Voice = '',
  [string]$StatusFile = '',
  [switch]$NoPreempt,
  [string]$HaBaseUrl = 'http://homeassistant.local:8123',
  [string]$HaTokenStore = ''
)
$ErrorActionPreference = 'Stop'
# A caller with a stripped environment (an MSYS2 shell) may pass no TEMP or LOCALAPPDATA.
if (-not $env:TEMP) { $env:TEMP = [IO.Path]::GetTempPath() }
if (-not $env:LOCALAPPDATA) { $env:LOCALAPPDATA = [Environment]::GetFolderPath('LocalApplicationData') }
if (-not $HaTokenStore) { $HaTokenStore = Join-Path $env:LOCALAPPDATA 'pantheon\ha-api-token.clixml' }
$log = Join-Path $env:TEMP 'pantheon_voice_say.log'
function Note([string]$what) { "$(Get-Date -Format o) $Target $what" | Out-File -FilePath $log -Append -Encoding utf8 }
function Stage([string]$word) {
  if ($StatusFile) { try { [IO.File]::AppendAllText($StatusFile, "$word`n") } catch { } }
}

try {
  $text = Get-Content -LiteralPath $TextFile -Raw -Encoding UTF8
} catch {
  Note 'no text file'; Stage 'fail'; exit 1
} finally {
  Remove-Item -LiteralPath $TextFile -Force -ErrorAction SilentlyContinue
}
$text = ($text -replace [char]0x2014, ', ' -replace [char]0x2013, ', ').Trim()
if (-not $text) { Note 'empty text'; Stage 'fail'; exit 0 }

if (-not $Voice) { $Voice = 'AvaNeural' }
function Get-HaToken {
  $sec = Import-Clixml -LiteralPath $HaTokenStore
  $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
  try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}
function Invoke-Ha([string]$path, [hashtable]$body) {
  Invoke-RestMethod -Method Post -Uri "$HaBaseUrl$path" -TimeoutSec 20 `
    -Headers @{ Authorization = "Bearer $(Get-HaToken)" } -ContentType 'application/json; charset=utf-8' `
    -Body ([Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Compress)))
}
function Get-HaState([string]$entity) {
  try {
    return (Invoke-RestMethod -Uri "$HaBaseUrl/api/states/$entity" -TimeoutSec 10 `
      -Headers @{ Authorization = "Bearer $(Get-HaToken)" }).state
  } catch { return '' }
}

if ($Target -eq 'ha') {
  if ($Entity -notmatch '^media_player\.[a-z0-9_]+$') { Note 'bad entity'; Stage 'fail'; exit 1 }
  try {
    if ($NoPreempt -and (Get-HaState $Entity) -eq 'playing') { Note "skipped a status line, $Entity busy"; Stage 'skip'; exit 0 }
    Invoke-Ha '/api/services/tts/speak' @{ entity_id = 'tts.home_assistant_cloud'; media_player_entity_id = $Entity
      message = $text; options = @{ voice = $Voice } } | Out-Null
    Note "sent to $Entity"
    # Started = the speaker itself reports playing within 15 s; Home Assistant accepting the call
    # is not sound (an unavailable speaker accepts it too).
    $seen = $false
    for ($t = 0; $t -lt 15 -and -not $seen; $t++) {
      Start-Sleep -Seconds 1
      if ((Get-HaState $Entity) -eq 'playing') { $seen = $true }
    }
    if (-not $seen) { Note "$Entity never reported playing"; Stage 'fail'; exit 1 }
    Stage 'playing'
    # Finished = two readings in a row of a settled state. A failed read, buffering or the time cap
    # is never taken as finished: a question must not reopen the puck's mic while it still sounds.
    $settled = 0
    for ($t = 0; $t -lt 150; $t++) {
      Start-Sleep -Seconds 1
      if ((Get-HaState $Entity) -in @('idle', 'off', 'paused', 'standby', 'on')) { $settled++ } else { $settled = 0 }
      if ($settled -ge 2) { Stage 'done'; break }
    }
  } catch {
    Note "failed for ${Entity}: $($_.Exception.Message)"; Stage 'fail'; exit 1
  }
  exit 0
}

# -Target pc. A newer answer stops the one still playing.
# The pid file holds "<pid> <start time ticks>": a process is stopped only when both match and its
# command line runs this script, so a reused process id never ends someone else's PowerShell.
$pidFile = Join-Path $env:TEMP 'pantheon_voice_say.pid'
function Get-OwnPlayer {
  $line = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not ("$line" -match '^(\d+) (\d+)$')) { return $null }
  $id, $ticks = [int]$Matches[1], [long]$Matches[2]
  if ($id -eq $PID) { return $null }
  $p = Get-Process -Id $id -ErrorAction SilentlyContinue
  if (-not $p -or $p.StartTime.Ticks -ne $ticks) { return $null }
  $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $id" -ErrorAction SilentlyContinue).CommandLine
  if ($cmd -notlike '*voice_say.ps1*') { return $null }
  return $p
}
# One player at a time checks and registers (a named lock), and a status line (-NoPreempt) never
# registers, so the pid file always names the answer a newer answer must stop.
$lock = New-Object System.Threading.Mutex($false, 'Local\PantheonVoiceSay')
try { [void]$lock.WaitOne(5000) } catch [System.Threading.AbandonedMutexException] { }
try {
$old = Get-OwnPlayer
if ($old -and $NoPreempt) { Note 'skipped a status line, an answer is playing'; Stage 'skip'; exit 0 }
if ($old) {
  Stop-Process -Id $old.Id -Force -ErrorAction SilentlyContinue
  # A stopped player never reaches its own cleanup, so its recording goes here, once Windows has
  # released the file (it can stay locked for a moment after the process ends).
  [void]$old.WaitForExit(3000)
  $rec = Join-Path $env:TEMP "pantheon_voice_$($old.Id).mp3"
  for ($i = 0; $i -lt 20 -and (Test-Path -LiteralPath $rec); $i++) {
    Remove-Item -LiteralPath $rec -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $rec) { Start-Sleep -Milliseconds 100 }
  }
}
# Recordings left by a player that died some other way are removed after ten minutes.
Get-ChildItem -LiteralPath $env:TEMP -Filter 'pantheon_voice_*.mp3' -ErrorAction SilentlyContinue |
  Where-Object { $_.LastWriteTime -lt (Get-Date).AddMinutes(-10) } | Remove-Item -Force -ErrorAction SilentlyContinue
if (-not $NoPreempt) {
  "$PID $((Get-Process -Id $PID).StartTime.Ticks)" | Out-File -FilePath $pidFile -Encoding ascii -Force
}
} finally {
  try { $lock.ReleaseMutex() } catch { }
}

$audio = Join-Path $env:TEMP "pantheon_voice_$PID.mp3"
try {
  $made = Invoke-Ha '/api/tts_get_url' @{ engine_id = 'tts.home_assistant_cloud'; message = $text
    language = 'en-US'; options = @{ voice = $Voice } }
  $url = if ($made.url) { $made.url } else { "$HaBaseUrl$($made.path)" }
  Invoke-WebRequest -Uri $url -OutFile $audio -TimeoutSec 30 -UseBasicParsing
  if ((Get-Item -LiteralPath $audio).Length -eq 0) { Note 'Home Assistant made no audio'; Stage 'fail'; exit 1 }

  Add-Type -AssemblyName PresentationCore
  $mp = New-Object System.Windows.Media.MediaPlayer
  $mp.Open([Uri]::new($audio))
  $waited = 0
  while (-not $mp.NaturalDuration.HasTimeSpan -and $waited -lt 5000) { Start-Sleep -Milliseconds 50; $waited += 50 }
  # No length after 5 s means the file did not open as audio: say so, rather than claim sound.
  if (-not $mp.NaturalDuration.HasTimeSpan -or $mp.NaturalDuration.TimeSpan.TotalSeconds -le 0) {
    $mp.Close(); Note 'the audio did not open'; Stage 'fail'; exit 1
  }
  $dur = $mp.NaturalDuration.TimeSpan.TotalSeconds
  $mp.Volume = 1.0
  $mp.Play()
  Stage 'playing'
  Start-Sleep -Seconds ([math]::Min(180, [math]::Ceiling($dur + 0.3)))
  $mp.Stop(); $mp.Close()
  Note ("played {0:N1}s" -f $dur)
  Stage 'done'
} catch {
  Note "failed: $($_.Exception.Message)"; Stage 'fail'; exit 1
} finally {
  Remove-Item -LiteralPath $audio -Force -ErrorAction SilentlyContinue
  $cur = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($cur -like "$PID *") { Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue }
}
