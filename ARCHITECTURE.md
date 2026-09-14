# ARN Brain — Architecture, Disaster Recovery, and Change Guide

This document explains **why** the system is built the way it is, **how to
rebuild it from nothing** if this PC is ever lost, and **how to make the
common changes** you're likely to need later. `README.md` covers day-to-day
usage; this file is the deeper reference. Keep it up to date — whenever the
system changes in a way that affects why/how something works, this file
should be updated in the same commit.

## What this system does, in one paragraph

Downloads every video from ARN's channel(s) (official uploads + guest
appearances elsewhere), transcribes the audio into a dated, searchable
knowledge base, backs that up in two independent places, runs itself daily
with no laptop babysitting required, and — on top of that — turns the
transcripts into a question-answering system that knows *when* ARN said
what, so his views can be tracked as they evolve over time rather than
treated as one flat, undated blob of opinions.

## Why it's built this way

**Transcribed in Roman Urdu, with Arabic/English kept in original script.**
Roman Urdu is what ARN's audience reads and searches in; transliterating
Quranic recitation or English portions into Roman Urdu would lose accuracy
and meaning, so those are kept as-is instead.

**Every video is tagged with its publish date, and files are named by date.**
The original goal wasn't just "what did ARN say" but "when, and how often" —
so that a later system can tell whether an opinion is current or outdated,
and weight recent statements over old ones when they conflict. This is why
`manifest.jsonl` stores `publish_date` on every record, and `ask.py`
explicitly reasons about which of several conflicting excerpts is newer.

**A JSONL manifest (`data/manifest.jsonl`) is the source of truth for what's
already done, not the filesystem.** This is what makes the whole pipeline
resumable across any number of stop/restarts without re-spending API cost —
every run checks the manifest first, never blindly re-downloads or
re-transcribes. `data/failures.jsonl` is a separate, append-only log of
what went wrong, so failures are visible without polluting the "done" list.

**Audio is sent to Gemini inline instead of via the Files API.** The
original API key rejected `genai.upload_file()` with `API_KEY_INVALID`
even though normal `generate_content` calls worked. Inline requests have a
~19MB size limit, so longer audio is split into overlapping 10-minute
chunks (`CHUNK_SECONDS` / `CHUNK_OVERLAP_SECONDS` in `arn_pipeline.py`) and
the resulting text is merged back together, with the overlap used to avoid
losing words that fall on a chunk boundary.

**Two official channels are scanned by default** (`DEFAULT_CHANNEL_URLS`) —
ARN has two channels, and both are treated as first-class sources.

**Another uploader's videos are excluded, not deleted, when found.**
`EXCLUDE_TITLE_PATTERNS` (currently `["shafy"]`) filters out another
creator's content that appears on a shared channel. Matching videos are
never downloaded from a fresh channel scan; `--cleanup-excluded` moves any
*already*-downloaded matches out of the main folders into `data/excluded/`
instead of deleting them, so they stay available if ever wanted, without
polluting ARN's own knowledge base. The same "move, never delete" rule is
enforced all the way into backups (see below), not just on disk.

**Guest appearances live in a separate file (`extra_urls.txt`), not the
channel scan.** ARN's own channels only list his own uploads. Podcast/guest
appearances have to be found and added manually — the file accepts both
single video URLs and playlist URLs (a playlist is auto-expanded into every
video it contains), so a whole curated playlist can be added as one line.

**Official-channel scanning and guest-appearance catch-up run on separate
API keys/quotas** (`daily_run.ps1` with `--skip-extra-urls` vs.
`guest_appearances_run.ps1` with `--extra-urls-only`). A big one-off manual
push through the guest backlog would otherwise compete with — and could
starve — the small, steady daily official-channel job for the same day's
free-tier cap.

**GitHub stores transcripts + manifest + failures, but never audio.** Git
is built for diffing text; audio is large binary data that would bloat the
repo's history on every commit with no benefit (no meaningful "diff" of an
MP3). Text is cheap to store forever in git; audio isn't.

**Google Drive stores both audio and a mirror of the transcripts,** under
your official-email account (which has free space) rather than your
personal one (nearly full) — Drive attributes storage usage to whoever
*uploads* a file, not whoever owns the containing folder, which is why
`rclone` is specifically configured to authenticate as the official
account, not the personal one. The folder is shared to the personal
account afterward via Drive's own sharing, independent of anything
Claude-related.

**The daily automation can only ever add/update backups, never delete
them.** `rclone copy` (not `sync`) and `git add --ignore-removal` are used
throughout `daily_run.ps1` and `guest_appearances_run.ps1` specifically so
that if the local `data/` folder were ever lost, corrupted, or accidentally
deleted, the next automated run cannot mirror that loss into GitHub or
Drive. A sanity check at the top of both scripts also refuses to run at
all if `data/audio` or `data/transcripts` looks suspiciously close to
empty, rather than silently proceeding.

**The vector index (`data/index/`) is excluded from git but backed up to
Drive.** It's a binary database (Chroma/SQLite-style) that doesn't diff
well in git and would bloat the repo on every rebuild — but it's also
fully regenerable from the transcripts, which *are* safe in both GitHub
and Drive. Even so, "regenerable" isn't the same as "backed up": rebuilding
costs time and (free-tier) API quota, so `build_index.py` copies the built
index to Drive (`ARNBrain/index`) automatically after every run, giving
you a standing copy without needing git at all.

**Embeddings reuse the same free-tier key as transcription** rather than a
dedicated project — embeddings and generation are billed/rate-limited as
separate quota buckets even on the same key, so there's no real quota
conflict to avoid here (unlike the official-channel vs. guest-appearance
split above, which genuinely did need separate keys).

## Disaster recovery — rebuilding on a brand new PC

If this PC is ever lost, wiped, or replaced, here's the full recovery path.
Nothing in this list is optional-but-convenient — every piece is what makes
the "PC-independent" claim actually true.

1. **Install prerequisites**: Python 3, `ffmpeg`, [Deno](https://deno.land)
   (needed by `yt-dlp` to solve YouTube's JS challenge), `rclone`
   (https://rclone.org/downloads/), and Git.
2. **Clone the repo**:
   ```
   git clone -b claude/arn-pipeline-setup-38o0lb https://github.com/HafizHamzaSaeed/ARNbrain.git
   cd ARNbrain
   pip install -r requirements.txt
   ```
   This alone recovers every transcript, the manifest, and the failure log —
   the actual knowledge base is intact at this point.
3. **Re-create your API keys as environment variables** (`setx` on Windows):
   - `GEMINI_API_KEY` — your main/paid key (only needed for big manual runs)
   - `GEMINI_API_KEY_FREE` — the never-billed key used by the daily official-channel job
   - `GEMINI_API_KEY_GUESTS` — the never-billed key used for guest-appearance catch-up
   (If you still have the old keys, reuse them — they're tied to Google Cloud
   projects, not this PC. If not, follow the "Setup" steps in `README.md` to
   create fresh ones — it costs nothing to create a new free-tier project.)
4. **Re-authenticate rclone to Drive**: `rclone config`, name the remote
   `arndrive`, choose Google Drive, sign in as your **official** account.
   Full steps are in `README.md` under "Backups".
5. **Recover audio** (optional — only needed if you want the audio files
   locally again, not just transcripts): `rclone copy arndrive:ARNBrain/audio data/audio`.
6. **Recover the vector index** (optional, saves re-embedding time/cost):
   `rclone copy arndrive:ARNBrain/index data/index`. If skipped, just run
   `python build_index.py` and it rebuilds from the transcripts instead.
7. **Re-create the two Windows Task Scheduler entries** (see README's
   "Daily automation" section for exact settings):
   - "ARN Brain Daily Backup" → runs `daily_run.ps1` daily
   - Guest-appearance catch-up stays manual (`guest_appearances_run.ps1`),
     no scheduled task needed for it.

At the end of this, you're back to exactly where you were — same GitHub
repo, same Drive folder, same automation — just on new hardware.

## How to make common changes

**Add a new guest-appearance link or playlist.** Paste the URL on its own
line in `extra_urls.txt` (or hand it to Claude to add), then run:
```
powershell -ExecutionPolicy Bypass -File guest_appearances_run.ps1
```

**Exclude a new uploader's content.** Add a lowercase substring of their
name/title pattern to `EXCLUDE_TITLE_PATTERNS` in `arn_pipeline.py` (near
the top of the file). Then run `python arn_pipeline.py --cleanup-excluded`
once to retroactively move anything already downloaded, and commit/push +
sync to Drive as usual so the change is backed up.

**Add another official channel to scan.** Add its `/videos` URL to
`DEFAULT_CHANNEL_URLS` in `arn_pipeline.py`, or pass `--channel <url1> <url2> ...`
on the command line for a one-off run.

**Rotate or replace an exhausted/expired API key.** No code change needed —
just `setx` the environment variable (`GEMINI_API_KEY`, `GEMINI_API_KEY_FREE`,
or `GEMINI_API_KEY_GUESTS`) to the new value and open a new terminal window.

**Change the daily automation's run time.** Task Scheduler → "ARN Brain
Daily Backup" → Triggers tab → Edit.

**Change how transcripts are chunked for the vector index.** `CHUNK_SIZE`
and `CHUNK_OVERLAP` constants near the top of `build_index.py`. Note: this
only affects *newly*-indexed videos going forward — to re-chunk everything
that's already indexed, delete `data/index/` entirely and re-run
`python build_index.py` from scratch (it'll re-embed everything, which
costs free-tier quota/time but no money).

**Change the transcription or answer prompt.** `TRANSCRIBE_PROMPT` in
`arn_pipeline.py`, or `ANSWER_PROMPT` in `ask.py`. Prompt changes only
affect videos transcribed/questions asked *after* the change — existing
transcripts/index entries aren't retroactively updated.

**Change which Gemini model is used.** `DEFAULT_MODEL` in `arn_pipeline.py`
(transcription), `ANSWER_MODEL` in `ask.py` (question-answering), or
`EMBED_MODEL` in `build_index.py` (indexing) — or pass `--model <name>` for
a one-off run of `arn_pipeline.py`.
