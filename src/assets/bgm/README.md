# Background Music

Drop a royalty-free MP3 here called **`default.mp3`** to enable BGM in avatar
videos. The track will be looped to match clip length and ducked to the BGM
volume you set in **Settings → Video Profile**.

## Two ways to provide music

1. **Bundled track (recommended for a single user)**
   Save a file at `src/assets/bgm/default.mp3`. Every generated clip uses it
   whenever *BGM enabled* is on.

2. **Per-user override**
   Set the `BGM_PATH` environment variable to an absolute path (or add it via
   Settings later). That path wins over the bundled file.

## Length & format

- Any duration works — FFmpeg loops short tracks via `-stream_loop -1`.
- Use MP3 or WAV (anything FFmpeg decodes).
- The final mix applies a 1s fade-out at the end of the clip.

## Royalty-free sources

- https://pixabay.com/music/search/cinematic/  (CC0, no attribution needed)
- https://freemusicarchive.org/  (check each track's license)
- https://www.youtube.com/audiolibrary  (free for YouTube, also fine for other platforms per Google's terms)

## What happens without a file

If no BGM file is present, the system skips mixing silently and uses the
avatar's narration track only. No error is logged.
