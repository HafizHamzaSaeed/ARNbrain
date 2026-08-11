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

Audio files are saved to `data/audio/`, transcripts to `data/transcripts/`.
Re-running the script skips videos that were already downloaded or transcribed,
so an interrupted run can be safely resumed.

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
