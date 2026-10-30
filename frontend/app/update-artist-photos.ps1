# Rebuilds assets\artists\photos.json from the image files in assets\artists\
# Name each file after the artist (lower case, spaces -> "-"):  bts.jpg  nct-dream.png  blackpink.webp  a2o-lite.jpg
# Run from this folder (frontend\app):  powershell -ExecutionPolicy Bypass -File .\update-artist-photos.ps1
$dir = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "assets\artists"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$files = Get-ChildItem -Path $dir -File | Where-Object { $_.Extension -match '^\.(jpe?g|png|webp)$' } | Sort-Object Name | ForEach-Object { $_.Name }
$json = if ($files) { ConvertTo-Json @($files) -Compress } else { "[]" }
[IO.File]::WriteAllText((Join-Path $dir "photos.json"), $json)
Write-Host "photos.json -> $json"
Write-Host "Press Ctrl+F5 in the browser to see the photos."
