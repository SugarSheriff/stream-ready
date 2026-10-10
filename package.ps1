# Builds the installable extension zip into dist/ (blender_manifest.toml at the zip root).
# With Blender on PATH, `blender --command extension build --source-dir stream_ready --output-dir dist`
# produces the same zip and validates the manifest while it's at it.

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$dist = Join-Path $root 'dist'
$stage = Join-Path $dist 'stage\stream_ready'

Remove-Item -Recurse -Force $dist -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item -Recurse (Join-Path $root 'stream_ready\*') $stage
Get-ChildItem $stage -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force

Compress-Archive -Path (Join-Path $stage '*') -DestinationPath (Join-Path $dist 'stream-ready-extension.zip')
Remove-Item -Recurse -Force (Join-Path $dist 'stage')

Get-ChildItem $dist -Filter *.zip | ForEach-Object { "{0}  {1:N0} bytes" -f $_.Name, $_.Length }
