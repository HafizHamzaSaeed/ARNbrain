# ARNbrain

Downloads every video from a YouTube channel and transcribes the audio with Gemini.

## Setup

```bash
sudo apt-get install -y ffmpeg   # if not already installed
pip install -r requirements.txt
export GEMINI_API_KEY="your-gemini-api-key"
```

## Run

```bash
python3 arn_pipeline.py                 # full channel run
python3 arn_pipeline.py --limit 3       # test on the first 3 videos
```

Audio files are saved to `data/audio/`, transcripts to `data/transcripts/` named
after each video's title (e.g. `My Lecture Title [abc123XYZ].txt`) — the video
ID stays bracketed at the end so re-runs can still detect and skip videos that
were already transcribed. Re-running the script skips videos that were already
downloaded or transcribed, so an interrupted run can be safely resumed.

Options:

- `--channel <url>` — override the default channel
- `--output-dir <path>` — override the default `data/` output location
- `--model <name>` — Gemini model used for transcription (default `gemini-3.5-flash`)
- `--request-delay <seconds>` — delay between Gemini calls to avoid rate limits (default 4s)

The Gemini API key is read only from the `GEMINI_API_KEY` environment variable —
it is never written to disk or committed to this repo.

## Notes

- Audio is sent to Gemini inline in the request rather than via the Files API
  (`genai.upload_file`) — some API keys reject the older Files API with
  `API_KEY_INVALID` even though normal generation calls work fine. If your key
  hits that error on `generate_content` too, get a fresh key from Google AI
  Studio and confirm it starts with the usual `AIzaSy...` format.
- Audio longer than ~19MB (roughly >30-40 min at the extraction bitrate used
  here) is automatically split into 10-minute chunks and transcribed piece by
  piece, then stitched into one transcript file.

## Backups

Two backup destinations, covering different risk (and cost) profiles:

- **GitHub** — transcripts, `manifest.jsonl`, `failures.jsonl` (text only,
  small, full history). `data/audio/` is excluded from git via `.gitignore`
  on purpose — audio is large and doesn't need version history, just a copy
  somewhere safe.
- **Google Drive** (a folder in your official-email account, shared with
  your personal account through Drive's normal Share feature — no Claude
  connection needed for this part) — both audio and a mirror of the
  transcripts, synced with `rclone`.

### One-time rclone setup

1. Install rclone: https://rclone.org/downloads/ (just the Windows exe).
2. `rclone config` → `n` (new remote) → name it `arndrive` → choose
   `drive` (Google Drive) → follow the browser login prompt, signing in
   with your **official** Google account → accept the defaults for the
   rest (full access, not a shared drive, no advanced config).
3. In that Drive account, create a folder (e.g. `ARNBrain`) and share it
   with your personal email if you want to browse it from there too.
4. Test it once by hand: `rclone lsd arndrive:` should list your Drive
   folders.

### Daily automation

`daily_run.ps1` runs the pipeline, pushes new transcripts to GitHub, and
syncs audio + transcripts to Drive, in that order — resumable and
idempotent, so re-running it after a failure just picks up where it left
off. Point Windows Task Scheduler at it once:

```
powershell -ExecutionPolicy Bypass -File daily_run.ps1
```

Requirements: `GEMINI_API_KEY` set as a persistent user environment
variable (`setx`), and the one-time `rclone config` above already done.

A note on scheduling elsewhere: GitHub Actions could in principle run
this on a cron schedule without your laptop being on, since GitHub's
runners have normal internet access — but YouTube tends to block/challenge
requests from shared datacenter IP ranges (which Actions runners are) more
aggressively than a home connection, so treat that as a fallback option
rather than the primary path unless you're prepared to troubleshoot
cookie-based workarounds.
