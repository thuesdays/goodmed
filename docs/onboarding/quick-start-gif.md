# README hero GIF — storyboard

A 15-second silent loop for the top of the README. Goal: someone
who lands on the GitHub repo sees the GIF and instantly understands
what the tool does without reading anything.

## Frame plan

15 seconds, ~30 fps, optimized for ~3MB final file. Compressed
heavily.

| Frame range | Visual |
|---|---|
| 0.0 – 1.0s | Dashboard Profiles page loaded. Cursor visible, idle. |
| 1.0 – 2.5s | Cursor moves to a row's ▶ Start button. Click. Button highlights. |
| 2.5 – 4.5s | Real Chromium window pops open in front of the dashboard. URL bar shows google.com. |
| 4.5 – 7.0s | The Chromium window types a query, hits enter, shows the SERP with ad detection highlights. |
| 7.0 – 9.0s | Cut back to dashboard. Self-test panel updates: green check, "13/13", coherence 91. |
| 9.0 – 11.0s | Cut to Fingerprint editor. Cursor hovers desktop/mobile toggle. Click. Fields rewrite live. |
| 11.0 – 13.0s | Cut to Extensions page showing pool with ~12 extension tiles. |
| 13.0 – 15.0s | Cut to closing frame: small "github.com/thuesdays/ghost_shell_browser" tag bottom-right, dashboard idle. Loop. |

## Production

**Recording**: OBS at 1080p60 → ScreenToGif (free) or Gifski for
the GIF compression. ffmpeg pipeline:

```bat
ffmpeg -i raw.mp4 -vf "fps=20,scale=1280:-1:flags=lanczos,palettegen=stats_mode=diff" palette.png
ffmpeg -i raw.mp4 -i palette.png -lavfi "fps=20,scale=1280:-1:flags=lanczos[v];[v][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" out.gif
```

This gives roughly 2.5-4 MB for 15 seconds at 1280px wide.

**Don't**:

- Don't go above 5 MB — GitHub READMEs render slowly with big
  GIFs.
- Don't go above 1280px wide — wastes bytes; most readers see it
  at ~900px effective width.
- Don't include audio; GIFs can't carry it anyway, and if the
  README hero is mistakenly an `<video>` with audio, it'll
  autoplay-blast people who land on the repo from a meeting.

## Where it goes

Top of `README.md`, immediately after the title:

```markdown
# Ghost Shell Anty

> Self-hosted antidetect browser…

<p align="center">
  <img src="docs/hero.gif" alt="Ghost Shell Anty quick demo" />
</p>

| | |
|---|---|
| **Engine** | …
```

Save the GIF as `docs/hero.gif` so it lives in-repo (don't host on
external CDN — those break, and GitHub's render path for in-repo
images is faster).

## Alternative: video instead of GIF

GitHub now supports `<video>` tags directly in markdown for
in-repo `.mp4` files:

```html
<video src="docs/hero.mp4" autoplay loop muted playsinline />
```

Better quality, smaller file size for the same length. Downside:
doesn't work outside GitHub's renderer (e.g., npm, dev.to,
crates.io won't render it). If the README is GitHub-only, video.
If you copy the README content elsewhere, GIF.

For maximum compatibility, embed both — GIF as fallback inside
the `<video>` tag:

```html
<video src="docs/hero.mp4" autoplay loop muted playsinline>
  <img src="docs/hero.gif" alt="Ghost Shell Anty demo" />
</video>
```
