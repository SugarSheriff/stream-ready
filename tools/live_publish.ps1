# Publishes a test chair to the real Miris with your saved sign-in.
#   ./tools/live_publish.ps1 -Blender "C:\path\to\blender.exe"
# Blender's user config is sandboxed; the sign-in is read from the OS vault.

param([Parameter(Mandatory)][string]$Blender)

$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
& (Join-Path $repo 'package.ps1') | Out-Null

$sandbox = Join-Path ([IO.Path]::GetTempPath()) ("stream-ready-live-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force $sandbox | Out-Null
$env:BLENDER_USER_RESOURCES = $sandbox
$env:BLENDER_USER_CONFIG = Join-Path $sandbox 'config'
$env:BLENDER_USER_SCRIPTS = Join-Path $sandbox 'scripts'
$env:BLENDER_USER_EXTENSIONS = Join-Path $sandbox 'extensions'
$env:MIRIS_API_KEY = ''
$env:STREAM_READY_TOKEN_SERVICE = ''

try {
    $ErrorActionPreference = 'Continue'
    & $Blender --background --factory-startup --online-mode --python-exit-code 1 --python (Join-Path $PSScriptRoot 'live_publish.py')
    $code = $LASTEXITCODE
} finally {
    Remove-Item -Recurse -Force $sandbox -ErrorAction SilentlyContinue
}
exit $code
