# Build Windows EXE

## Option 1: GitHub Actions (recommended)

1. Push this repo to GitHub.
2. Open `Actions` -> `Build Windows EXE`.
3. Click `Run workflow`.
4. Download artifact: `TorrentBatchDownloader-windows`.

Output file:
- `TorrentBatchDownloader.exe`

## Option 2: Build on a Windows machine

Run in PowerShell from project root:

```powershell
python -m pip install --upgrade pip
pip install pyinstaller
./build_windows.ps1
```

Output:
- `dist/TorrentBatchDownloader.exe`
