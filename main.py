#!/usr/bin/env python3
"""
YouTube -> Podcast RSS -> Spotify sync script.

What it does:
  1. Reads your YouTube channel's RSS feed to find videos.
  2. Skips any video ID it has already processed (tracked in state.json).
  3. Downloads audio-only for each new video with yt-dlp.
  4. Uploads the audio file to a Cloudflare R2 bucket (public URL).
  5. Rebuilds a podcast RSS feed (feed.xml) listing every episode so far
     and uploads that to R2 too.
  6. Saves state.json so the next run knows what's already been done.

Spotify for Podcasters polls the feed.xml URL periodically and publishes
any new episodes automatically. You only submit that URL once, during setup.

All configuration comes from environment variables (see README.md).
"""

import json
import os
import sys
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import boto3
import feedparser
from feedgen.feed import FeedGenerator
from mutagen.mp3 import MP3

STATE_FILE = Path(__file__).parent / "state.json"

# ---- Config from environment -------------------------------------------

YOUTUBE_CHANNEL_ID = os.environ["YOUTUBE_CHANNEL_ID"]

R2_ACCOUNT_ID = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET = os.environ["R2_BUCKET"]
R2_PUBLIC_BASE_URL = os.environ["R2_PUBLIC_BASE_URL"].rstrip("/")  # e.g. https://pub-xxxx.r2.dev

PODCAST_TITLE = os.environ.get("PODCAST_TITLE", "My Podcast")
PODCAST_AUTHOR = os.environ.get("PODCAST_AUTHOR", "Unknown")
PODCAST_DESCRIPTION = os.environ.get("PODCAST_DESCRIPTION", "Audio from my YouTube channel.")
PODCAST_LINK = os.environ.get("PODCAST_LINK", f"https://www.youtube.com/channel/{YOUTUBE_CHANNEL_ID}")
PODCAST_IMAGE_URL = os.environ.get("PODCAST_IMAGE_URL", "")
PODCAST_LANGUAGE = os.environ.get("PODCAST_LANGUAGE", "en")
PODCAST_EXPLICIT = os.environ.get("PODCAST_EXPLICIT", "no")
PODCAST_EMAIL = os.environ.get("PODCAST_EMAIL", "")  # required by Spotify to verify ownership

MAX_NEW_VIDEOS_PER_RUN = int(os.environ.get("MAX_NEW_VIDEOS_PER_RUN", "5"))
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", "")  # optional path to a cookies.txt


def r2_client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"episodes": []}  # list of dicts, newest appended at the end


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fetch_youtube_entries(channel_id):
    """Return YouTube RSS entries, oldest first, using the public feed
    (no API key needed). Only the most recent ~15 videos are included by
    YouTube's feed, which is fine since we track what's already synced."""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    parsed = feedparser.parse(url)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"Could not read YouTube feed for channel {channel_id}: {parsed.bozo_exception}")
    entries = list(reversed(parsed.entries))  # oldest first, so episodes publish in order
    return entries


def download_audio(video_url, workdir, cookies_file=None):
    """Use yt-dlp to download best audio and convert to mp3. Returns the mp3 path."""
    out_template = str(workdir / "%(id)s.%(ext)s")
    cmd = [
        "yt-dlp",
        "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "-o", out_template,
        "--no-playlist",
        "--js-runtimes", "deno",  # required: YouTube now needs JS execution to unlock formats
        "--remote-components", "ejs:github",  # allow yt-dlp to fetch its JS challenge-solver script
    ]
    if cookies_file:
        cmd += ["--cookies", cookies_file]
    cmd.append(video_url)
    subprocess.run(cmd, check=True)
    mp3_files = list(workdir.glob("*.mp3"))
    if not mp3_files:
        raise RuntimeError(f"yt-dlp did not produce an mp3 for {video_url}")
    return mp3_files[0]


def upload_to_r2(client, local_path, key, content_type):
    client.upload_file(
        str(local_path),
        R2_BUCKET,
        key,
        ExtraArgs={"ContentType": content_type, "ACL": "public-read"},
    )
    return f"{R2_PUBLIC_BASE_URL}/{key}"


def build_feed(episodes):
    fg = FeedGenerator()
    fg.load_extension("podcast")

    fg.title(PODCAST_TITLE)
    fg.link(href=PODCAST_LINK, rel="alternate")
    fg.description(PODCAST_DESCRIPTION)
    fg.language(PODCAST_LANGUAGE)
    fg.podcast.itunes_author(PODCAST_AUTHOR)
    fg.podcast.itunes_explicit(PODCAST_EXPLICIT)
    if PODCAST_EMAIL:
        fg.podcast.itunes_owner(name=PODCAST_AUTHOR, email=PODCAST_EMAIL)
        fg.managingEditor(f"{PODCAST_EMAIL} ({PODCAST_AUTHOR})")
    if PODCAST_IMAGE_URL:
        fg.image(PODCAST_IMAGE_URL)
        fg.podcast.itunes_image(PODCAST_IMAGE_URL)

    # feedgen writes newest-first automatically if we add newest last;
    # simplest is to add in reverse (newest first) explicitly.
    for ep in sorted(episodes, key=lambda e: e["published"], reverse=True):
        fe = fg.add_entry()
        fe.id(ep["audio_url"])
        fe.title(ep["title"])
        fe.description(ep["description"])
        fe.enclosure(ep["audio_url"], str(ep["file_size_bytes"]), "audio/mpeg")
        fe.pubDate(ep["published"])
        fe.podcast.itunes_duration(ep["duration_seconds"])

    return fg


def main():
    state = load_state()
    seen_ids = {ep["video_id"] for ep in state["episodes"]}

    entries = fetch_youtube_entries(YOUTUBE_CHANNEL_ID)
    new_entries = [e for e in entries if e.yt_videoid not in seen_ids]

    if not new_entries:
        print("No new videos found.")
    new_entries = new_entries[:MAX_NEW_VIDEOS_PER_RUN]

    client = r2_client()
    succeeded = 0

    for entry in new_entries:
        video_id = entry.yt_videoid
        video_url = entry.link
        title = entry.title
        description = getattr(entry, "summary", "")[:4000]
        print(f"Processing new video: {title} ({video_id})")

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            try:
                mp3_path = download_audio(video_url, workdir, cookies_file=YTDLP_COOKIES_FILE or None)
            except subprocess.CalledProcessError as e:
                print(f"  yt-dlp failed for {video_id}, skipping this run: {e}", file=sys.stderr)
                continue

            audio = MP3(mp3_path)
            duration_seconds = int(audio.info.length)
            file_size_bytes = mp3_path.stat().st_size

            key = f"episodes/{video_id}.mp3"
            audio_url = upload_to_r2(client, mp3_path, key, "audio/mpeg")
            print(f"  Uploaded to {audio_url}")

        published = datetime.now(timezone.utc)
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

        state["episodes"].append({
            "video_id": video_id,
            "title": title,
            "description": description or title,
            "audio_url": audio_url,
            "duration_seconds": duration_seconds,
            "file_size_bytes": file_size_bytes,
            "published": published.isoformat(),
        })
        succeeded += 1

    if succeeded:
        fg = build_feed(state["episodes"])
        feed_path = Path(tempfile.gettempdir()) / "feed.xml"
        fg.rss_file(str(feed_path))
        feed_url = upload_to_r2(client, feed_path, "feed.xml", "application/rss+xml")
        print(f"Feed updated: {feed_url}")

        save_state(state)
        print(f"Done. {succeeded} new episode(s) added.")
        if succeeded < len(new_entries):
            failed = len(new_entries) - succeeded
            print(f"WARNING: {failed} video(s) attempted this run but failed to download. "
                  f"Check the log above for errors — they will be retried next run.", file=sys.stderr)
    elif new_entries:
        print(f"ERROR: All {len(new_entries)} video(s) attempted this run failed to download. "
              f"Nothing was added. Check the log above for errors.", file=sys.stderr)
        sys.exit(1)
    else:
        print("Nothing to do.")


if __name__ == "__main__":
    main()
