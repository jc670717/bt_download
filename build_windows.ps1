$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path "torrent_batch_gui.py")) {
  throw "torrent_batch_gui.py not found in: $root"
}

if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }

pyinstaller `
  --noconfirm `
  --clean `
  --onefile `
  --windowed `
  --name "TorrentBatchDownloader" `
  "torrent_batch_gui.py"

Write-Host "Done: dist/TorrentBatchDownloader.exe"
