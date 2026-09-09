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
git add data/transcripts data/excluded/transcripts data/manifest.jsonl data/failures.jsonl
$staged = git diff --cached --name-only
if ($staged) {
    git commit -m "Daily transcript update: $(Get-Date -Format 'yyyy-MM-dd')"
    git push -u origin claude/arn-pipeline-setup-38o0lb
    Write-Host "Pushed new transcripts to GitHub."
} else {
    Write-Host "No new transcripts to push today."
}

# 3. Mirror audio + transcripts to Google Drive (official account) via rclone.
#    data/excluded/* is synced to its own Drive subfolder so excluded
#    videos (e.g. Shafy Butt's) stay backed up, not deleted, when they
#    disappear from data/audio and data/transcripts.
rclone sync data/audio             arndrive:ARNBrain/audio             --progress
rclone sync data/transcripts       arndrive:ARNBrain/transcripts       --progress
rclone sync data/excluded/audio       arndrive:ARNBrain/excluded/audio       --progress
rclone sync data/excluded/transcripts arndrive:ARNBrain/excluded/transcripts --progress
Write-Host "Synced audio + transcripts to Drive."

Write-Host "=== Done: $(Get-Date) ==="
