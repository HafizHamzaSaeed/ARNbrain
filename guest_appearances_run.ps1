# Manual guest-appearance catch-up: processes extra_urls.txt only (ARN's
# appearances on other channels/podcasts), leaving the official channel(s)
# untouched. Run this by hand whenever you add new guest-appearance links
# to extra_urls.txt, or to work through the remaining backlog.
#
# Uses its own separate free-tier key (GEMINI_API_KEY_GUESTS) so this never
# competes with the daily automated job (official channels only) for the
# same day's free-tier quota.
#
# Usage: powershell -ExecutionPolicy Bypass -File guest_appearances_run.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "=== ARN Brain guest-appearance run: $(Get-Date) ==="

$audioCount = (Get-ChildItem data/audio -File -ErrorAction SilentlyContinue).Count
$transcriptCount = (Get-ChildItem data/transcripts -File -ErrorAction SilentlyContinue).Count
if ($audioCount -lt 10 -or $transcriptCount -lt 10) {
    Write-Host "SAFETY STOP: data/audio ($audioCount files) or data/transcripts ($transcriptCount files) looks empty or missing. Not touching GitHub or Drive. Check D:\ARNbrain-git\data manually before running again."
    exit 1
}

$env:GEMINI_API_KEY = $env:GEMINI_API_KEY_GUESTS

# 1. Process extra_urls.txt only — official channels are untouched here.
python arn_pipeline.py --extra-urls-only

# 2. Push new transcripts to GitHub (same safe, deletion-proof approach as daily_run.ps1).
git add --ignore-removal -- data/transcripts data/excluded/transcripts data/manifest.jsonl data/failures.jsonl
$staged = git diff --cached --name-only
if ($staged) {
    git commit -m "Guest-appearance transcript update: $(Get-Date -Format 'yyyy-MM-dd')"
    git push -u origin claude/arn-pipeline-setup-38o0lb
    Write-Host "Pushed new transcripts to GitHub."
} else {
    Write-Host "No new transcripts to push."
}

# 3. Copy new audio + transcripts to Drive (never deletes, same as daily_run.ps1).
rclone copy data/audio                 arndrive:ARNBrain/audio                 --progress
rclone copy data/transcripts           arndrive:ARNBrain/transcripts           --progress
rclone copy data/excluded/audio        arndrive:ARNBrain/excluded/audio        --progress
rclone copy data/excluded/transcripts  arndrive:ARNBrain/excluded/transcripts  --progress
Write-Host "Copied audio + transcripts to Drive."

Write-Host "=== Done: $(Get-Date) ==="
