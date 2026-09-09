# Daily ARN Brain automation: fetch new videos, transcribe, back up.
#
# Wire this into Windows Task Scheduler as the one job that runs every day.
# It expects GEMINI_API_KEY_FREE to be set as a persistent user env var
# (via setx) to a key from a project that has never been linked to billing
# — daily runs only transcribe a handful of new videos, so they run on the
# free tier instead of spending the paid balance reserved for big manual
# runs. It also expects rclone to be configured with a remote named
# "arndrive" pointing at the shared Drive folder under your official
# Google account (see README.md "Backups" section for the one-time setup).
#
# Usage: powershell -ExecutionPolicy Bypass -File daily_run.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "=== ARN Brain daily run: $(Get-Date) ==="

# Safety check: if data/audio or data/transcripts looks empty or missing
# (e.g. the folder got deleted or moved), stop immediately rather than
# let the backup steps below mirror that emptiness to GitHub/Drive.
$audioCount = (Get-ChildItem data/audio -File -ErrorAction SilentlyContinue).Count
$transcriptCount = (Get-ChildItem data/transcripts -File -ErrorAction SilentlyContinue).Count
if ($audioCount -lt 10 -or $transcriptCount -lt 10) {
    Write-Host "SAFETY STOP: data/audio ($audioCount files) or data/transcripts ($transcriptCount files) looks empty or missing. Not touching GitHub or Drive. Check D:\ARNbrain-git\data manually before running again."
    exit 1
}

# Use the free-tier key for this scheduled run only — the paid
# GEMINI_API_KEY stays untouched for manual/bulk runs in other windows.
$env:GEMINI_API_KEY = $env:GEMINI_API_KEY_FREE

# 1. Fetch + transcribe any new uploads (both channels, extra guest URLs).
#    Already-completed videos are skipped automatically via the manifest.
#    If a day's new videos exceed the free tier's daily request cap, the
#    remainder fail into failures.jsonl and retry automatically tomorrow
#    once the quota resets — no data lost, just a delay.
python arn_pipeline.py

# 2. Push new/updated text (transcripts + manifest + failures) to GitHub.
#    Audio is intentionally excluded from git (.gitignore) — it goes to
#    Drive instead in step 3. data/excluded/transcripts is included too,
#    so videos moved there by --cleanup-excluded stay backed up under
#    their new location instead of just disappearing from the repo.
#    --ignore-removal: only stage new/changed files, never deletions — a
#    file missing locally must never be pushed as "deleted" from GitHub.
git add --ignore-removal -- data/transcripts data/excluded/transcripts data/manifest.jsonl data/failures.jsonl
$staged = git diff --cached --name-only
if ($staged) {
    git commit -m "Daily transcript update: $(Get-Date -Format 'yyyy-MM-dd')"
    git push -u origin claude/arn-pipeline-setup-38o0lb
    Write-Host "Pushed new transcripts to GitHub."
} else {
    Write-Host "No new transcripts to push today."
}

# 3. Mirror audio + transcripts to Google Drive (official account) via rclone.
#    "copy" (not "sync"): only ever adds/updates files on Drive, never
#    deletes — so an accidental local deletion can't cascade into Drive.
#    Any new local subfolder (e.g. a future exclusion category) gets its
#    matching Drive folder created automatically on first copy into it.
rclone copy data/audio                 arndrive:ARNBrain/audio                 --progress
rclone copy data/transcripts           arndrive:ARNBrain/transcripts           --progress
rclone copy data/excluded/audio        arndrive:ARNBrain/excluded/audio        --progress
rclone copy data/excluded/transcripts  arndrive:ARNBrain/excluded/transcripts  --progress
Write-Host "Copied audio + transcripts to Drive (existing Drive files are never deleted by this script)."

Write-Host "=== Done: $(Get-Date) ==="
