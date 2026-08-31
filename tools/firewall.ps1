<#
.SYNOPSIS
    Block this app's Python from reaching the network, in both directions.

.DESCRIPTION
    The dictation app is local by design: the models sit in %USERPROFILE%\ai-models
    and nothing it does requires the internet. These rules make that structural
    rather than a promise, so a stray dependency cannot phone home with audio or
    transcripts, and faster-whisper cannot silently re-download a model.

    The worker inherits sys.executable from the supervisor, so whichever Python
    starts the app is also the one that loads the models. Both the console and
    the windowed interpreter are covered.

    Requires an elevated PowerShell.

.PARAMETER Remove
    Delete the rules instead of creating them.

.NOTES
    A Windows Firewall rule matches the *image path of the running process*, and
    uv builds a venv whose python.exe is a trampoline that re-execs the base
    interpreter. Under one of those, the process Windows sees is
    C:\Program Files\Python313\pythonw.exe and a rule naming the venv path
    never fires -- the rules look perfectly correct in the firewall UI and block
    nothing at all. This script refuses to run in that state rather than leave
    you believing you are protected; -ReplaceTrampolines swaps in real
    interpreter copies, which is what stdlib "venv --copies" produces.

    While these rules are on, pip cannot install into this venv -- pip runs
    through the same python.exe. Lift them for as long as you need with:

        .\tools\firewall.ps1 -Remove      (then re-run without -Remove)

    or, without deleting anything:

        Disable-NetFirewallRule -Group "Local Whisper STT"
        Enable-NetFirewallRule  -Group "Local Whisper STT"
#>
[CmdletBinding()]
param([switch]$Remove, [switch]$ReplaceTrampolines)

$ErrorActionPreference = "Stop"

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this from an elevated PowerShell (Windows Firewall rules need administrator)."
}

$group = "Local Whisper STT"
$venv  = Join-Path $PSScriptRoot "..\.venv\Scripts"
$exes  = @("python.exe", "pythonw.exe") | ForEach-Object {
    $full = Join-Path $venv $_
    if (-not (Test-Path $full)) { throw "Not found: $full" }
    (Resolve-Path $full).Path
}

# A trampoline carries an embedded payload and so is far bigger than the real
# interpreter it launches. Comparing against the base install is the reliable
# tell, and getting this wrong means rules that block nothing.
$cfg = Get-Content (Join-Path $venv "..\pyvenv.cfg") | Where-Object { $_ -match "^home\s*=" }
$home_ = ($cfg -split "=", 2)[1].Trim()
foreach ($exe in $exes) {
    $name = Split-Path $exe -Leaf
    $base = Join-Path $home_ $name
    if (-not (Test-Path $base)) { continue }
    if ((Get-Item $exe).Length -le (Get-Item $base).Length) { continue }
    if (-not $ReplaceTrampolines) {
        throw ("$name in the venv is a launcher, not an interpreter, so the " +
               "process Windows sees is $base and these rules would not " +
               "match it. Re-run with -ReplaceTrampolines to swap in real " +
               "interpreter copies.")
    }
    Copy-Item $exe "$exe.uv-trampoline" -Force
    Copy-Item $base $exe -Force
    Write-Host "replaced the $name launcher with a real interpreter"
}

# Always clear first: re-running must not stack duplicate rules, and a moved or
# rebuilt venv would otherwise leave rules pointing at a path that no longer
# exists while the new one is unprotected.
Get-NetFirewallRule -Group $group -ErrorAction SilentlyContinue | Remove-NetFirewallRule
if ($Remove) { Write-Host "Removed the '$group' rules."; return }

foreach ($exe in $exes) {
    $name = Split-Path $exe -Leaf
    foreach ($dir in @("Outbound", "Inbound")) {
        New-NetFirewallRule `
            -DisplayName "Local Whisper STT - block $dir ($name)" `
            -Group $group `
            -Program $exe `
            -Direction $dir `
            -Action Block `
            -Profile Any `
            -Enabled True | Out-Null
    }
}

Get-NetFirewallRule -Group $group |
    Select-Object DisplayName, Direction, Action, Enabled |
    Format-Table -AutoSize
Write-Host "Done. The app is now cut off from the network in both directions."
