# Daily ARN Brain automation: fetch new videos, transcribe, back up.
#
# Wire this into Windows Task Scheduler as the one job that runs every day.
# It expects GEMINI_API_KEY to already be set as a persistent user env var
# (via setx), and rclone to be configured with a remote named "arndrive"
# pointing at the shared Drive folder under your official Google account
# (see README.md "Backups" section for the one-time rclone setup).
#
# Usage: powershell -ExecutionPolicy Bypass -File daily_run.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "=== ARN Brain daily run: $(Get-Date) ==="

# 1. Fetch + transcribe any new uploads (both channels, extra guest URLs).
#    Already-completed videos are skipped automatically via the manifest.
python arn_pipeline.py

# 2. Push new/updated text (transcripts + manifest + failures) to GitHub.
#    Audio is intentionally excluded from git (.gitignore) — it goes to
#    Drive instead in step 3.
git add data/transcripts data/manifest.jsonl data/failures.jsonl
$staged = git diff --cached --name-only
if ($staged) {
    git commit -m "Daily transcript update: $(Get-Date -Format 'yyyy-MM-dd')"
    git push -u origin claude/arn-pipeline-setup-38o0lb
    Write-Host "Pushed new transcripts to GitHub."
} else {
    Write-Host "No new transcripts to push today."
}

# 3. Mirror audio + transcripts to Google Drive (official account) via rclone.
rclone sync data/audio   arndrive:ARNBrain/audio      --progress
rclone sync data/transcripts arndrive:ARNBrain/transcripts --progress
Write-Host "Synced audio + transcripts to Drive."

Write-Host "=== Done: $(Get-Date) ==="
