# YouTube → Podcast RSS → Spotify sync

Automatically turns new videos on your YouTube channel into podcast episodes
that show up on Spotify (and Apple Podcasts, and anywhere else that reads
podcast RSS feeds).

## How it works

```
YouTube channel  --(RSS, checked every 6h by GitHub Actions)-->
  new video found --> yt-dlp extracts audio (mp3) -->
  uploaded to Cloudflare R2 --> podcast feed.xml regenerated -->
  Spotify for Podcasters polls feed.xml --> episode appears on Spotify
```

You set this up once. After that it's fully automatic — no re-uploading,
no manual steps, nothing to pay for at normal podcast volumes.

## One-time setup

### 1. Find your YouTube channel ID
Go to your channel page → click "..." or your channel icon → "About" →
Share → Copy channel ID. It looks like `UCxxxxxxxxxxxxxxxxxxxxxx`.
(Not your @handle — the actual ID.)

### 2. Create a free Cloudflare R2 bucket
1. Sign up at https://dash.cloudflare.com (free, no card required for R2's
   free tier — 10GB storage, no egress fees).
2. Go to R2 → Create bucket. Name it e.g. `my-podcast-audio`.
3. In the bucket settings, enable public access — this gives you a public
   URL like `https://pub-xxxxxxxx.r2.dev`. That's your `R2_PUBLIC_BASE_URL`.
4. Go to R2 → Manage API tokens → create a token with **read & write**
   access to that bucket. Note the Access Key ID, Secret Access Key, and
   your Account ID (shown in the R2 dashboard sidebar).

### 3. Create a GitHub repo for this project
1. Create a new **private or public** repo (public is fine — audio files
   live in R2, not in the repo).
2. Push these files (`main.py`, `requirements.txt`, `state.json`,
   `.github/workflows/sync.yml`) to it.

### 4. Add secrets to the repo
Repo → Settings → Secrets and variables → Actions → **New repository secret**,
add each of these:

| Secret name | Value |
|---|---|
| `YOUTUBE_CHANNEL_ID` | from step 1 |
| `YOUTUBE_COOKIES` | see "YouTube cookies" section below |
| `R2_ACCOUNT_ID` | from step 2 |
| `R2_ACCESS_KEY_ID` | from step 2 |
| `R2_SECRET_ACCESS_KEY` | from step 2 |
| `R2_BUCKET` | your bucket name, e.g. `my-podcast-audio` |
| `R2_PUBLIC_BASE_URL` | e.g. `https://pub-xxxxxxxx.r2.dev` |

Then under the **Variables** tab (same page), add these (not secret, just config):

| Variable name | Value |
|---|---|
| `PODCAST_TITLE` | Your podcast's name |
| `PODCAST_AUTHOR` | Your name / brand |
| `PODCAST_DESCRIPTION` | A sentence describing the show |
| `PODCAST_LINK` | Your channel or website URL |
| `PODCAST_IMAGE_URL` | URL to a square cover image (min 1400x1400px), e.g. hosted in the same R2 bucket |
| `PODCAST_EMAIL` | An email you control — Spotify sends a verification code here when you import the feed |

### YouTube cookies (needed to avoid "Sign in to confirm you're not a bot")

YouTube increasingly bot-checks requests coming from datacenter IPs — which
is exactly what GitHub Actions runners are — so downloads will fail without
this. The fix is to give yt-dlp a copy of your browser's YouTube cookies so
requests look like they're coming from a logged-in session.

1. In Chrome or Firefox, log into youtube.com with any Google account (a
   plain viewer account is fine — it doesn't need to own the channel).
2. Install a "cookies.txt" export extension, e.g.
   [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
   for Chrome.
3. While on youtube.com, click the extension and export cookies for
   `youtube.com`. This downloads a `cookies.txt` file in Netscape format.
4. Open that file in a text editor, select all, copy it.
5. In your repo: Settings → Secrets and variables → Actions → New repository
   secret → name it `YOUTUBE_COOKIES` → paste the entire file contents as
   the value.

**Notes:**
- These cookies expire periodically (weeks to months depending on the
  account). If downloads start failing again with a sign-in error, just
  re-export and update the secret.
- Consider using a secondary/throwaway Google account for this rather than
  your primary one, since the cookies grant that account's browsing session
  to the automation.
- This pattern (automated tools using exported session cookies) sits in a
  gray area of YouTube's Terms of Service around automated access — it's
  extremely common practice for personal archiving/repurposing of your own
  content, but worth being aware of.

### 5. Run it once manually
Repo → Actions tab → "Sync YouTube audio to podcast feed" → Run workflow.
Check the logs. If it succeeds, visit `R2_PUBLIC_BASE_URL/feed.xml` in your
browser — you should see your podcast RSS feed with your latest video as
an episode.

### 6. Submit the feed to Spotify for Podcasters
1. Go to https://podcasters.spotify.com and sign in.
2. Choose "Already have a podcast hosted elsewhere?" / "Import feed"
   (Spotify's exact wording changes occasionally — look for the RSS import
   option).
3. Paste your feed URL: `R2_PUBLIC_BASE_URL/feed.xml`.
4. Verify ownership if asked, and submit.

From then on, every time the GitHub Action runs and finds a new video, it
adds it to feed.xml — Spotify picks it up automatically on its next poll
(usually within a few hours).

## Notes and limits

- The YouTube RSS feed only lists the ~15 most recent videos. `state.json`
  tracks what's already been synced, so this only matters if you go more
  than ~15 videos without the Action running — unlikely at a 6-hour
  schedule, but worth knowing.
- `MAX_NEW_VIDEOS_PER_RUN` (optional env var, default 5) caps how many
  videos are processed in a single run, so a backlog doesn't time out the
  Action.
- This downloads audio only — no video is stored or re-uploaded anywhere.
- Make sure you have the rights to redistribute the audio from your videos
  as a podcast (usually a non-issue for your own original content).
- Cron schedule is UTC and can be changed in `.github/workflows/sync.yml`.
- If a video is age-restricted, unlisted, or region-locked, yt-dlp may fail
  on it — the script logs the error and skips it, other videos still process.
