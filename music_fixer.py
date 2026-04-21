#!/usr/bin/env python3
"""Clean up music file metadata and filenames for Rekordbox.

Supports .mp3, .flac, .wav files:
  - Normalizes artist/title/album tags (ID3v2.3 for MP3, Vorbis for FLAC,
    ID3 chunk for WAV) so Rekordbox reads them consistently.
  - Falls back to parsing the filename when tags are missing or wrong.
  - Optionally queries the Groq free API to infer a genre for each track.
  - Renames files to "Artist - Title.ext".

Usage:
    export GROQ_API_KEY=gsk_...
    python music_fixer.py /path/to/music
    python music_fixer.py /path/to/music --dry-run
    python music_fixer.py /path/to/music --no-genre
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from mutagen import File as MutagenFile
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3, ID3NoHeaderError
    from mutagen.flac import FLAC
    from mutagen.wave import WAVE
    from mutagen.mp3 import MP3
except ImportError:
    sys.exit("Missing dependency. Run: pip install mutagen requests")

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run: pip install mutagen requests")


SUPPORTED_EXTS = {".mp3", ".flac", ".wav"}

# Characters that are invalid or problematic on common filesystems.
_INVALID_FN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")

# Patterns stripped from filenames before splitting on " - ".
_NOISE_PATTERNS = [
    re.compile(r"\(official\s*(music\s*)?video\)", re.I),
    re.compile(r"\(official\s*audio\)", re.I),
    re.compile(r"\(lyric(s)?\s*video\)", re.I),
    re.compile(r"\(audio\)", re.I),
    re.compile(r"\(hd\)", re.I),
    re.compile(r"\(4k\)", re.I),
    re.compile(r"\[official.*?\]", re.I),
    re.compile(r"\[hd\]", re.I),
    re.compile(r"\[4k\]", re.I),
    re.compile(r"\[lyrics?\]", re.I),
    re.compile(r"\[audio\]", re.I),
]

# Leading track numbers: "01 - ", "01. ", "1) ", "01_"
_LEADING_TRACK = re.compile(r"^\s*\d{1,3}\s*[-._)\.]\s*")


GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.1-8b-instant"


@dataclass
class TrackInfo:
    artist: str
    title: str
    album: Optional[str] = None
    genre: Optional[str] = None
    year: Optional[str] = None


# -------------------- filename parsing --------------------

def clean_component(text: str) -> str:
    """Tidy whitespace, underscores, and trailing junk on a name component."""
    text = text.replace("_", " ")
    for pat in _NOISE_PATTERNS:
        text = pat.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip(" -_.")
    return text


def parse_from_filename(stem: str) -> tuple[Optional[str], Optional[str]]:
    """Best-effort extraction of (artist, title) from a filename stem."""
    name = _LEADING_TRACK.sub("", stem)
    for pat in _NOISE_PATTERNS:
        name = pat.sub(" ", name)
    name = name.replace("_", " ")
    name = _WHITESPACE.sub(" ", name).strip()

    # Prefer " - " as separator; fall back to single "-" with spaces.
    parts = re.split(r"\s+-\s+", name, maxsplit=1)
    if len(parts) == 2:
        artist = clean_component(parts[0])
        title = clean_component(parts[1])
        if artist and title:
            return artist, title

    # Try a lone hyphen without spaces as a last resort.
    if "-" in name:
        a, _, t = name.partition("-")
        artist = clean_component(a)
        title = clean_component(t)
        if artist and title:
            return artist, title

    return None, clean_component(name) or None


def safe_filename(name: str) -> str:
    name = _INVALID_FN_CHARS.sub("", name)
    name = _WHITESPACE.sub(" ", name).strip(" .")
    return name[:180]  # keep under typical filesystem name limits


# -------------------- tag reading & writing --------------------

def read_tags(path: Path) -> TrackInfo:
    """Read existing tags from a file, falling back to empty strings."""
    ext = path.suffix.lower()
    artist = title = album = genre = year = ""

    try:
        if ext == ".mp3":
            try:
                audio = EasyID3(path)
            except ID3NoHeaderError:
                audio = {}
            artist = (audio.get("artist") or [""])[0]
            title = (audio.get("title") or [""])[0]
            album = (audio.get("album") or [""])[0]
            genre = (audio.get("genre") or [""])[0]
            year = (audio.get("date") or [""])[0]
        elif ext == ".flac":
            audio = FLAC(path)
            artist = (audio.get("artist") or [""])[0]
            title = (audio.get("title") or [""])[0]
            album = (audio.get("album") or [""])[0]
            genre = (audio.get("genre") or [""])[0]
            year = (audio.get("date") or [""])[0]
        elif ext == ".wav":
            audio = WAVE(path)
            tags = audio.tags  # ID3 chunk, may be None
            if tags is not None:
                artist = str(tags.get("TPE1", "") or "")
                title = str(tags.get("TIT2", "") or "")
                album = str(tags.get("TALB", "") or "")
                genre = str(tags.get("TCON", "") or "")
                year = str(tags.get("TDRC", "") or "")
    except Exception as e:
        print(f"  ! could not read tags: {e}")

    return TrackInfo(
        artist=clean_component(artist),
        title=clean_component(title),
        album=clean_component(album) or None,
        genre=clean_component(genre) or None,
        year=clean_component(year) or None,
    )


def write_tags(path: Path, info: TrackInfo) -> None:
    """Write normalized tags. Uses ID3v2.3 for MP3/WAV for Rekordbox."""
    ext = path.suffix.lower()

    if ext == ".mp3":
        try:
            audio = EasyID3(path)
        except ID3NoHeaderError:
            mp3 = MP3(path)
            mp3.add_tags()
            mp3.save()
            audio = EasyID3(path)
        audio["artist"] = info.artist
        audio["title"] = info.title
        if info.album:
            audio["album"] = info.album
        if info.genre:
            audio["genre"] = info.genre
        if info.year:
            audio["date"] = info.year
        # v2.3 is the most widely compatible with Rekordbox.
        audio.save(v2_version=3)

    elif ext == ".flac":
        audio = FLAC(path)
        audio["artist"] = info.artist
        audio["title"] = info.title
        if info.album:
            audio["album"] = info.album
        if info.genre:
            audio["genre"] = info.genre
        if info.year:
            audio["date"] = info.year
        audio.save()

    elif ext == ".wav":
        audio = WAVE(path)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        from mutagen.id3 import TIT2, TPE1, TALB, TCON, TDRC
        tags["TPE1"] = TPE1(encoding=3, text=info.artist)
        tags["TIT2"] = TIT2(encoding=3, text=info.title)
        if info.album:
            tags["TALB"] = TALB(encoding=3, text=info.album)
        if info.genre:
            tags["TCON"] = TCON(encoding=3, text=info.genre)
        if info.year:
            tags["TDRC"] = TDRC(encoding=3, text=info.year)
        audio.save(v2_version=3)


# -------------------- Groq genre lookup --------------------

class GenreClient:
    def __init__(self, api_key: str, model: str = GROQ_MODEL):
        self.api_key = api_key
        self.model = model
        self.cache: dict[tuple[str, str], str] = {}
        self.session = requests.Session()

    def lookup(self, artist: str, title: str) -> Optional[str]:
        key = (artist.lower(), title.lower())
        if key in self.cache:
            return self.cache[key]

        prompt = (
            "Return the single most accurate music genre for this track. "
            "Respond with only the genre (e.g. House, Techno, Hip Hop, Drum and Bass, "
            "Pop, Rock, Disco, Trance). No punctuation, no extra words.\n"
            f"Artist: {artist}\nTitle: {title}"
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are a music genre classifier."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": 16,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        for attempt in range(3):
            try:
                r = self.session.post(
                    GROQ_ENDPOINT, headers=headers, json=payload, timeout=20
                )
                if r.status_code == 429:
                    wait = 2 ** attempt
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                data = r.json()
                genre = data["choices"][0]["message"]["content"].strip()
                genre = genre.strip('"').strip("'").strip(".").strip()
                # Keep it short; if the model returned a sentence, take the first line.
                genre = genre.splitlines()[0].strip()
                if len(genre) > 40:
                    genre = genre[:40].strip()
                self.cache[key] = genre
                return genre
            except requests.RequestException as e:
                if attempt == 2:
                    print(f"  ! Groq lookup failed: {e}")
                    return None
                time.sleep(2 ** attempt)
        return None


# -------------------- main pipeline --------------------

def process_file(
    path: Path,
    genre_client: Optional[GenreClient],
    dry_run: bool,
) -> None:
    print(f"\n{path.name}")
    existing = read_tags(path)

    artist = existing.artist
    title = existing.title

    if not artist or not title:
        fn_artist, fn_title = parse_from_filename(path.stem)
        artist = artist or (fn_artist or "")
        title = title or (fn_title or "")

    if not title:
        print("  ! could not determine title; skipping")
        return
    if not artist:
        artist = "Unknown Artist"

    genre = existing.genre
    if genre_client and not genre:
        genre = genre_client.lookup(artist, title)
        if genre:
            print(f"  genre (groq): {genre}")

    info = TrackInfo(
        artist=artist,
        title=title,
        album=existing.album,
        genre=genre,
        year=existing.year,
    )

    print(f"  artist: {info.artist}")
    print(f"  title:  {info.title}")
    if info.album:
        print(f"  album:  {info.album}")
    if info.genre:
        print(f"  genre:  {info.genre}")

    new_stem = safe_filename(f"{info.artist} - {info.title}")
    new_path = path.with_name(new_stem + path.suffix.lower())

    if dry_run:
        if new_path != path:
            print(f"  [dry-run] would rename -> {new_path.name}")
        else:
            print("  [dry-run] filename already correct")
        print("  [dry-run] would rewrite tags")
        return

    try:
        write_tags(path, info)
    except Exception as e:
        print(f"  ! failed to write tags: {e}")
        return

    if new_path != path:
        if new_path.exists():
            # Don't clobber a different file that already has the canonical name.
            print(f"  ! target exists, skipping rename: {new_path.name}")
            return
        try:
            path.rename(new_path)
            print(f"  renamed -> {new_path.name}")
        except OSError as e:
            print(f"  ! rename failed: {e}")


def iter_music_files(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
            yield p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", type=Path, help="Folder containing music files")
    ap.add_argument("--dry-run", action="store_true", help="Preview changes only")
    ap.add_argument("--no-genre", action="store_true", help="Skip Groq genre lookup")
    ap.add_argument("--api-key", help="Groq API key (else $GROQ_API_KEY)")
    ap.add_argument("--model", default=GROQ_MODEL, help="Groq model name")
    args = ap.parse_args()

    if not args.path.exists():
        print(f"Path does not exist: {args.path}")
        return 1

    genre_client: Optional[GenreClient] = None
    if not args.no_genre:
        key = args.api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            print(
                "No Groq API key found. Set $GROQ_API_KEY, pass --api-key, "
                "or run with --no-genre.",
                file=sys.stderr,
            )
            return 1
        genre_client = GenreClient(key, model=args.model)

    files = list(iter_music_files(args.path)) if args.path.is_dir() else [args.path]
    if not files:
        print("No supported audio files found.")
        return 0

    print(f"Processing {len(files)} file(s)...")
    for f in files:
        try:
            process_file(f, genre_client, args.dry_run)
        except Exception as e:
            print(f"  ! error on {f}: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
