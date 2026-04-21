# Music-files-fixer

Cleans up metadata and filenames on your music library so everything shows up
consistently in Rekordbox. Supports `.mp3`, `.flac`, and `.wav`.

What it does, per file:

1. Reads existing tags (ID3 for MP3/WAV, Vorbis for FLAC).
2. Falls back to parsing `Artist - Title` out of the filename when tags are
   missing or junk (strips track numbers, `(Official Video)`, `[HD]`, etc.).
3. Optionally calls the Groq free API to infer a genre.
4. Writes normalized tags (ID3v2.3 for MP3/WAV — best Rekordbox compatibility).
5. Renames the file to `Artist - Title.ext`.

## Setup

```bash
pip install -r requirements.txt
export GROQ_API_KEY=gsk_your_key_here   # from https://console.groq.com
```

## Usage

Dry-run first to preview changes without touching anything:

```bash
python music_fixer.py "/path/to/music" --dry-run
```

Run for real:

```bash
python music_fixer.py "/path/to/music"
```

Skip the Groq genre lookup:

```bash
python music_fixer.py "/path/to/music" --no-genre
```

Use a different Groq model:

```bash
python music_fixer.py "/path/to/music" --model llama-3.3-70b-versatile
```

## Notes

- The script recurses into subdirectories.
- Genre lookups are cached per `(artist, title)` within a run, so duplicate
  tracks only cost one API call.
- Existing genre tags are kept; Groq is only consulted when the genre is blank.
- If renaming would overwrite a different file that already has the canonical
  name, the rename is skipped and logged.
- Always back up your library before running it on the whole collection.
