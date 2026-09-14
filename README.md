# ARNbrain

Downloads every video from a YouTube channel and transcribes the audio with Gemini.

See **[ARCHITECTURE.md](ARCHITECTURE.md)** for why the system is built this
way, how to fully rebuild it on a new PC if this one is ever lost, and how
to make common changes (new exclusions, new channels, prompt/model
changes, etc.). This file covers day-to-day usage.

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

- `--channel <url>` — override the default channel(s)
- `--output-dir <path>` — override the default `data/` output location
- `--model <name>` — Gemini model used for transcription (default `gemini-3.5-flash`)
- `--request-delay <seconds>` — delay between Gemini calls to avoid rate limits (default 4s)
- `--cleanup-excluded` — retroactively move any already-downloaded video matching
  `EXCLUDE_TITLE_PATTERNS` (e.g. another uploader's videos) out of `data/audio` and
  `data/transcripts` into `data/excluded/`, and out of the manifest. Nothing is
  deleted — just set aside. Run this once after adding a new exclusion pattern.

The Gemini API key is read only from the `GEMINI_API_KEY` environment variable —
it is never written to disk or committed to this repo.

## Guest appearances on other channels

ARN's own channel scan only covers his own uploads. To include appearances as a
guest on other channels/podcasts, add video or **playlist** URLs (one per line,
`#` for comments) to `extra_urls.txt` — a playlist URL is automatically expanded
into every video it contains. To add a newly-found appearance later, just paste
its link on its own line in that file (or hand the link to Claude to add it for
you). Start with playlists ARN has already curated on his own channel before
branching out to searching other channels for more appearances.

Official-channel scanning and guest-appearance processing run on **separate**
free-tier API keys/quotas, so a big guest-appearance catch-up never competes
with the daily official-channel job for the same day's cap:

- `daily_run.ps1` (automated, official channels only) uses `GEMINI_API_KEY_FREE`
  and passes `--skip-extra-urls`.
- `guest_appearances_run.ps1` (run by hand whenever you add new guest links)
  uses a separate `GEMINI_API_KEY_GUESTS` and passes `--extra-urls-only`.

Both flags also work standalone on `arn_pipeline.py` directly if you want to
run either half manually outside the wrapper scripts.

## Asking questions (search / RAG)

Once transcripts exist, build a searchable index and query it:

```bash
python3 build_index.py             # one-time (and after new transcripts appear)
python3 ask.py "What has ARN said about investing in gold?"
python3 ask.py                     # interactive mode
```

`build_index.py` splits each transcript into overlapping chunks and embeds
them with Gemini (`models/gemini-embedding-001` — a separate, much higher
free-tier limit than generation), storing them in a local Chroma vector
database at `data/index/`. Resumable the same way as the main pipeline:
already-indexed videos are tracked in `data/index/indexed_videos.jsonl`
and skipped on re-run.

`data/index/` is **not** backed up to GitHub — it's a binary database that
doesn't diff well in git and would bloat the repo on every rebuild — but
it *is* copied to Drive (`ARNBrain/index`) automatically at the end of
every `build_index.py` run, so it isn't stuck only on this PC even though
it's excluded from git. Pass `--skip-drive-backup` to skip that step.

`ask.py` embeds your question, retrieves the most relevant transcript
chunks, and asks Gemini to answer using only those excerpts — citing the
date and video for each claim, and explicitly flagging when ARN's stated
view seems to have changed over time (weighting the more recent one).

## Public web app (unlisted link)

`app.py` is the same question-answering logic as `ask.py`, wrapped as a
small [Streamlit](https://streamlit.io) web app you can deploy for free on
Streamlit Community Cloud, giving you an unlisted link you can share
without anyone needing a Claude or Google account of their own.

It doesn't read the local Chroma database directly — the deployed app has
no access to your PC. Instead, `export_index.py` exports the built index
into a small, git-friendly format:

```bash
python3 export_index.py
# writes data/public_index/embeddings.npy + data/public_index/chunks.jsonl
git add data/public_index
git commit -m "Update public index"
git push
```

Run this after `build_index.py` picks up new videos, whenever you want
the deployed app to catch up.

**One-time deployment:**
1. Create a **4th** Gemini free-tier key, in its own never-billed project
   (same process as the other keys) — this one is just for the public
   app, so it never competes with the pipeline's own quotas.
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in with
   GitHub, and deploy this repo's `app.py` (branch:
   `claude/arn-pipeline-setup-38o0lb`).
3. In the app's **Settings → Secrets**, add:
   ```
   GEMINI_API_KEY = "your-new-key-here"
   ```
4. Streamlit gives you a URL — that's the unlisted link. Share it only
   with people you want using it (see ARCHITECTURE.md for why a fully
   public, ungated link risks exhausting the free-tier quota).

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
copies audio + transcripts to Drive, in that order — resumable and
idempotent, so re-running it after a failure just picks up where it left
off. Point Windows Task Scheduler at it once:

```
powershell -ExecutionPolicy Bypass -File daily_run.ps1
```

Requirements:
- `GEMINI_API_KEY_FREE` set as a persistent user environment variable (`setx`)
  to a key from a Google Cloud project that has **never** been linked to
  billing. Daily runs only pick up a handful of new videos, well within the
  free tier, so there's no reason to spend the paid balance (kept in
  `GEMINI_API_KEY`, untouched) on routine catch-up. If one day's new videos
  exceed the free tier's daily cap, the rest fail into `failures.jsonl` and
  retry automatically the next day once the quota resets.
- The one-time `rclone config` above already done.

For safety, the script refuses to touch GitHub or Drive at all if
`data/audio` or `data/transcripts` looks unexpectedly empty (e.g. the local
folder got deleted or moved) — and it uses `git add --ignore-removal` and
`rclone copy` (not `sync`) throughout, so it can only ever add or update
files in the backups, never delete them, even if local data disappears.

A note on scheduling elsewhere: GitHub Actions could in principle run
this on a cron schedule without your laptop being on, since GitHub's
runners have normal internet access — but YouTube tends to block/challenge
requests from shared datacenter IP ranges (which Actions runners are) more
aggressively than a home connection, so treat that as a fallback option
rather than the primary path unless you're prepared to troubleshoot
cookie-based workarounds.
