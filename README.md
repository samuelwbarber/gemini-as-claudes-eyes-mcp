# gemini-video-mcp

An MCP server that lets Claude Code (or any MCP client) **review video files against
criteria** using Gemini's native video understanding, driven through Google's Antigravity
CLI (`agy`). No Gemini API key is needed: it reuses the Google login you already have in `agy`.

Claude can't watch video. Gemini can. This server lets Claude hand a render, a screen
recording, or a captured camera feed to Gemini together with a list of acceptance criteria,
and get back a structured verdict with per-criterion evidence and timestamps.

## Requirements

- The Antigravity CLI (`agy`) installed and logged in. `agy models` should list Gemini models.
- [uv](https://docs.astral.sh/uv/). The server declares its single dependency inline, so
  `uv run server.py` installs it on first start.
- Python 3.11 or newer (uv fetches one if needed).

Developed and tested on Windows 11. The code uses portable paths, but macOS and Linux are
untested.

## Install

```
git clone https://github.com/<you>/gemini-video-mcp
claude mcp add --scope user gemini-video -- uv run /path/to/gemini-video-mcp/server.py
claude mcp list          # should show gemini-video: Connected
```

Optional `/video-review` skill for Claude Code: copy or link `skill/` to
`~/.claude/skills/video-review` (Windows: `mklink /J %USERPROFILE%\.claude\skills\video-review <repo>\skill`).
It teaches Claude how to turn a vague ask into observable criteria and how to report results.

Remove with `claude mcp remove -s user gemini-video`.

## Tools

| Tool | What it does |
|------|--------------|
| `review_video(video_path, criteria, context?, model?, additional_video_paths?)` | One video against a list of criteria. Returns `overall_verdict`, `summary`, per-criterion `verdict` (pass / fail / partial / not_assessable), `confidence`, `evidence`, `timestamps`, plus `issues_found` and `suggestions`. |
| `compare_videos(reference_path, candidate_path, criteria, context?, model?)` | Judge a candidate (your render or simulation) against reference footage. Both files are viewed in one session; each criterion gets a reference and a candidate observation. |
| `ask_video(video_path, question, model?, additional_video_paths?)` | Open questions: describe what happens, find a timestamp, count events. |
| `list_video_models()` | Model ids available through `agy`, and the server default. |

`criteria` can be a list of strings or one string separated by newlines or semicolons.
Paths may be Windows (`C:\...`), Git Bash (`/c/...`), WSL (`/mnt/c/...`) or `~` style.
Supported inputs: mp4, webm, mov, mkv, avi, wmv, flv, mpg, m4v, 3gp, and png/jpg/gif/webp images.

Example, from a Claude Code session:

> Use review_video on renders/dice_take3.mp4 with criteria: each die bounces at least twice
> before settling; no die passes through the table; all dice are at rest by the last frame.
> Context: Blender/PyBullet simulation of a Sic Bo shaker, judge physical plausibility.

## Models

`agy` exposes several Gemini models; all of them accept video. The server default is the
thorough one and can be switched per call with the `model` argument:

- `gemini-3.1-pro-high` (default): most careful reviews, roughly 60 to 90 s for a 10 s clip
- `gemini-3.8-flash-high`: several times faster, good for quick checks and long clips

Change the default with the `GEMINI_VIDEO_MODEL` environment variable on the server entry.

## How it works

Each tool call runs `agy -p <prompt> --output-format json` (headless print mode). The prompt
tells Gemini to open the file(s) with its file-viewing tool and answer; the structured tools
also pass a JSON schema with `--json-schema`, so the result comes back as parsed JSON.
Long calls are fine: the server sends MCP progress notifications every 5 s, which keeps
Claude Code's idle timeout from firing.

### Permissions

Headless `agy` cannot prompt, so it auto-denies every file read unless an allow-rule matches.
Its `permissions.allow` rules only match an exact path (or the bare `*`; globs are not
honoured). The server therefore adds `read_file(<exact path>)` to `~/.gemini/antigravity-cli/settings.json`
immediately before each run and removes it afterwards, leaving the file as it found it.
Gemini is never granted shell, search, or write access.

## Configuration

All optional, set as environment variables on the MCP server entry (`claude mcp add -e KEY=value ...`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `AGY_BIN` | `agy` on PATH, else `%LOCALAPPDATA%\agy\bin\agy.exe` | Path to the Antigravity CLI |
| `AGY_SETTINGS` | `~/.gemini/antigravity-cli/settings.json` | agy settings file (for the temporary allow-rules) |
| `AGY_WORKDIR` | your home directory | Working directory for agy runs; must be a trusted agy workspace |
| `GEMINI_VIDEO_MODEL` | `gemini-3.1-pro-high` | Default model id |
| `GEMINI_VIDEO_TIMEOUT` | `900` | Per-call timeout in seconds |

## Testing

`test_client.py` drives the server over stdio like a real MCP client:

```
uv run --with "mcp<2" python test_client.py --video clip.mp4
uv run --with "mcp<2" python test_client.py --video clip.mp4 --reference real.mp4 --candidate ours.mp4 compare
```

## Limitations

- Gemini samples video at roughly 1 frame per second. Fast events (bounces, impacts, 120 fps
  captures) can fall between samples. For physics or motion checks, export a slow-motion
  clip (3x to 5x) so each event spans several sampled frames.
- Local files only. A live stream must be recorded to a file first.
- Each call creates a conversation in agy's history (visible with `agy --continue`).
- Token cost and time scale with clip length; trim to the relevant segment.

## License

MIT
