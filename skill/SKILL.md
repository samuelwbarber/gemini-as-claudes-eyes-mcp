---
name: video-review
description: Review a video, render, or recorded feed against acceptance criteria using Gemini's native video understanding (gemini-video MCP server). Use when the user asks to check, QA, review, grade, or compare a video/render/clip against requirements, a spec, or reference footage, or asks what happens in a video.
---

# Video review with Gemini

The `gemini-video` MCP server exposes four tools that send a local video file to Gemini
(through the Antigravity CLI) and return its judgement. Gemini watches the actual footage, so
use it whenever a question needs eyes on the video rather than on the code that produced it.

## Tools

| Tool | Use it for |
|------|-----------|
| `review_video(video_path, criteria, context?, model?)` | one video vs a list of criteria, structured pass/fail per criterion |
| `compare_videos(reference_path, candidate_path, criteria, context?, model?)` | our render/sim vs reference footage, both viewed in one session |
| `ask_video(video_path, question, model?)` | open questions: describe, find a timestamp, count events |
| `list_video_models()` | current model ids and the server default |

Arguments: `$ARGUMENTS` may be given as `<video path> :: <criterion>; <criterion>; ...`.
If only a path is given, ask the user what to check, or infer criteria from the surrounding task.

## Workflow

1. **Resolve the file.** Local files only (mp4, webm, mov, mkv, avi, images). Windows, `/c/...`
   and `~` paths are all accepted. A live feed or stream must be recorded to a clip first.
2. **Write criteria, not vibes.** Turn the ask into 3 to 8 short, observable, one-behaviour
   statements. Good: "each die bounces at least twice before settling", "no object passes through
   the table surface", "the wheel stops within 4 s of launch", "text stays legible in every frame".
   Bad: "looks realistic", "is good".
3. **Give context.** Say what the video shows, what is being tested, which region or object
   matters, and anything to ignore (e.g. "left half is real footage, judge the right half").
4. **Call the tool.** Default model is the thorough one (`gemini-3.1-pro-high`, roughly 60 to 90 s
   for a 10 s clip). Pass `model="gemini-3.8-flash-high"` for quick checks, long clips, or when
   the user wants speed. Long calls are fine; the server streams progress.
5. **Report.** Lead with `overall_verdict` and `summary`, then a table:
   criterion | verdict | confidence | evidence (with timestamps). Then `issues_found` and
   `suggestions`. Keep Gemini's timestamps so the user can scrub to them.
6. **Iterate.** If a verdict is `not_assessable` or low confidence, suggest a better angle,
   a slow-motion export, or a trimmed clip, and offer to re-run. When a fix is made to the
   source (Blender scene, physics params), re-render and re-run the same criteria so the
   before/after is comparable.

## Notes

- Results are Gemini's opinion of what it saw; treat borderline verdicts as a prompt to look
  yourself, not as ground truth.
- Gemini samples video at roughly 1 frame per second. Fast events (dice bounces, impacts,
  120 fps captures) can fall between samples, so for physics or motion checks export a
  slow-motion clip (3x to 5x) so each bounce spans several sampled frames.
- Token cost scales with clip length. Trim to the relevant segment when the video is long.
- The server only ever grants Gemini read access to the exact files you pass; it never runs
  shell commands or edits anything.
