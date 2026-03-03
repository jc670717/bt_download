# Auto Release By Tag

This repository now supports auto release on tag push.

## Rule

- Tag format must start with `v` (example: `v1.0.0`).
- On tag push, GitHub Actions will:
  - build `TorrentBatchDownloader.exe` on `windows-latest`
  - create SHA256 file
  - publish both files to GitHub Releases

## Steps

```bash
git add .
git commit -m "chore: add release-on-tag workflow"
git push origin main

git tag v1.0.0
git push origin v1.0.0
```

## Re-run

If a tag release failed, delete and recreate the tag:

```bash
git tag -d v1.0.0
git push origin :refs/tags/v1.0.0

git tag v1.0.0
git push origin v1.0.0
```
