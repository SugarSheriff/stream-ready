# Runs tests/blender_smoke.py inside a real Blender, headless.
#   ./tests/run_blender_smoke.ps1 -Blender "C:\path\to\blender.exe"
# Blender's user config, scripts, and extensions are pointed at a throwaway folder,
# so installing the add-on here never touches your real Blender setup.

param([Parameter(Mandatory)][string]$Blender)

$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
& (Join-Path $repo 'package.ps1') | Out-Null

$sandbox = Join-Path ([IO.Path]::GetTempPath()) ("stream-ready-smoke-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force $sandbox | Out-Null
$env:BLENDER_USER_RESOURCES = $sandbox
$env:BLENDER_USER_CONFIG = Join-Path $sandbox 'config'
$env:BLENDER_USER_SCRIPTS = Join-Path $sandbox 'scripts'
$env:BLENDER_USER_EXTENSIONS = Join-Path $sandbox 'extensions'
$env:MIRIS_API_KEY = ''  # the smoke test sets its own key; never pick up a real one
# Sign-in tokens go to a throwaway vault entry, never the real "stream-ready/miris-oauth".
$env:STREAM_READY_TOKEN_SERVICE = "stream-ready/smoke-" + [guid]::NewGuid().ToString('N').Substring(0, 8)

try {
    # Blender prints Python warnings to stderr; don't let PowerShell treat them as fatal.
    $ErrorActionPreference = 'Continue'
    & $Blender --background --factory-startup --online-mode --python-exit-code 1 --python (Join-Path $PSScriptRoot 'blender_smoke.py')
    $code = $LASTEXITCODE
} finally {
    Remove-Item -Recurse -Force $sandbox -ErrorAction SilentlyContinue
}
exit $code
