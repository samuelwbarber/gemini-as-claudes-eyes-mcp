# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.10,<2"]
# ///
"""
gemini-video: an MCP server that lets Claude Code review video files against
criteria using Google's Gemini models, driven through the Antigravity CLI (agy).

How it works
------------
Every tool shells out to `agy -p` (headless print mode) with a prompt that tells
Gemini to view the given video file(s) and answer.  The structured tools pass a
JSON schema via --json-schema so the answer comes back as parsed JSON.

Permissions
-----------
Headless agy cannot prompt for tool permissions, and its allow-rules only match
exact paths (or the bare `*`).  So, for the duration of each run, the server adds
an exact-path `read_file(<video>)` allow-rule to the agy settings file and removes
it again afterwards.  Nothing else is ever granted.

Configuration (environment variables, all optional)
---------------------------------------------------
AGY_BIN               path to agy           default: agy on PATH, else %LOCALAPPDATA%\\agy\\bin\\agy.exe
AGY_SETTINGS          agy settings.json     default: ~/.gemini/antigravity-cli/settings.json
AGY_WORKDIR           cwd for agy runs      default: the user's home (must be a trusted agy workspace)
GEMINI_VIDEO_MODEL    default model id      default: gemini-3.1-pro-high
GEMINI_VIDEO_TIMEOUT  per-call timeout (s)  default: 900
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

# --------------------------------------------------------------------------- config

HOME = Path.home()


def _default_agy_bin() -> str:
    found = shutil.which("agy")
    if found:
        return found
    return str(Path(os.environ.get("LOCALAPPDATA", str(HOME / "AppData" / "Local"))) / "agy" / "bin" / "agy.exe")


AGY_BIN = os.environ.get("AGY_BIN") or _default_agy_bin()
AGY_SETTINGS = Path(os.environ.get("AGY_SETTINGS") or (HOME / ".gemini" / "antigravity-cli" / "settings.json"))
AGY_WORKDIR = os.environ.get("AGY_WORKDIR") or str(HOME)
DEFAULT_MODEL = os.environ.get("GEMINI_VIDEO_MODEL", "gemini-3.1-pro-high")
DEFAULT_TIMEOUT = int(os.environ.get("GEMINI_VIDEO_TIMEOUT", "900"))

VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".wmv", ".flv", ".mpg", ".mpeg", ".m4v", ".3gp"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

FAST_MODEL_HINT = "gemini-3.8-flash-high"

mcp = FastMCP(
    "gemini-video",
    instructions=(
        "Tools for reviewing local video files (renders, recordings, captured feeds) with Gemini's native "
        "video understanding. Use review_video to check one video against a list of criteria, compare_videos "
        "to judge a candidate against reference footage, and ask_video for open questions. Write criteria as "
        "short, observable, one-behaviour statements (e.g. 'each die bounces at least twice before settling', "
        "'no object clips through the table'). Give context (what the video shows, which region/object matters). "
        f"Default model {DEFAULT_MODEL} is the most thorough; {FAST_MODEL_HINT} is several times faster for "
        "quick checks or long clips. Live streams must be recorded to a file first. Gemini samples video at "
        "about 1 frame per second, so for fast motion (bounces, impacts) prefer a slow-motion export."
    ),
)

# --------------------------------------------------------------------------- helpers

_GITBASH = re.compile(r"^/([a-zA-Z])/(.*)$")
_WSL = re.compile(r"^/mnt/([a-zA-Z])/(.*)$")


def _log(msg: str) -> None:
    print(f"[gemini-video] {msg}", file=sys.stderr, flush=True)


def normalize_path(p: str) -> Path:
    """Accept Windows, Git-Bash (/c/...), WSL (/mnt/c/...) or ~ paths; return an existing absolute Path."""
    p = p.strip().strip('"').strip("'")
    m = _GITBASH.match(p) or _WSL.match(p)
    if m:
        p = f"{m.group(1).upper()}:/{m.group(2)}"
    path = Path(os.path.expanduser(p)).resolve()
    if not path.is_file():
        raise ValueError(f"File not found: {path}")
    if path.suffix.lower() not in VIDEO_EXTS | IMAGE_EXTS:
        raise ValueError(
            f"Unsupported file type '{path.suffix}'. Supported: {', '.join(sorted(VIDEO_EXTS | IMAGE_EXTS))}"
        )
    return path


def _file_info(path: Path) -> dict[str, Any]:
    return {"path": str(path), "size_mb": round(path.stat().st_size / 1_048_576, 2)}


# ----- agy permission rules ------------------------------------------------------

_rules_lock = threading.Lock()
_inflight: Counter[str] = Counter()


def _read_settings() -> dict[str, Any]:
    try:
        return json.loads(AGY_SETTINGS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _write_settings(d: dict[str, Any]) -> None:
    AGY_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = AGY_SETTINGS.with_name(AGY_SETTINGS.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    os.replace(tmp, AGY_SETTINGS)


def _rules_for(paths: list[Path]) -> list[str]:
    rules: list[str] = []
    for p in paths:
        for form in (str(p), p.as_posix()):
            rules.append(f"read_file({form})")
    return rules


@contextmanager
def allow_read(paths: list[Path]):
    """Temporarily add exact-path read_file allow-rules for `paths` to the agy settings."""
    rules = _rules_for(paths)
    with _rules_lock:
        d = _read_settings()
        allow = d.setdefault("permissions", {}).setdefault("allow", [])
        for r in rules:
            _inflight[r] += 1
            if r not in allow:
                allow.append(r)
        _write_settings(d)
    try:
        yield
    finally:
        with _rules_lock:
            d = _read_settings()
            allow = d.get("permissions", {}).get("allow", [])
            for r in rules:
                _inflight[r] -= 1
                if _inflight[r] <= 0:
                    del _inflight[r]
                    while r in allow:
                        allow.remove(r)
            # leave the settings file as we found it: drop empty containers we created
            if not allow and "allow" in d.get("permissions", {}):
                del d["permissions"]["allow"]
            if "permissions" in d and not d["permissions"]:
                del d["permissions"]
            _write_settings(d)


# ----- running agy -------------------------------------------------------------

def _parse_agy_json(out: str, err: str) -> dict[str, Any]:
    for stream in (out, err):
        for line in reversed(stream.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
    raise RuntimeError(f"agy produced no JSON result.\n--- stdout ---\n{out[-3000:]}\n--- stderr ---\n{err[-3000:]}")


async def run_agy(
    ctx: Context | None,
    prompt: str,
    model: str,
    paths: list[Path],
    schema: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    if not Path(AGY_BIN).is_file():
        raise RuntimeError(f"agy binary not found at {AGY_BIN}. Set AGY_BIN.")
    timeout = timeout or DEFAULT_TIMEOUT
    args = [
        AGY_BIN, "-p", prompt,
        "--model", model,
        "--output-format", "json",
        "--print-timeout", f"{timeout}s",
        "--disable-slash-commands",
    ]
    schema_file: str | None = None
    if schema is not None:
        fd, schema_file = tempfile.mkstemp(prefix="gemini-video-schema-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(schema, f)
        args += ["--json-schema", schema_file]

    env = {**os.environ, "NO_COLOR": "1"}
    t0 = time.monotonic()
    _log(f"running {model} on {[p.name for p in paths]}")
    try:
        with allow_read(paths):
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=AGY_WORKDIR,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            comm = asyncio.ensure_future(proc.communicate())
            while not comm.done():
                await asyncio.wait({comm}, timeout=5)
                elapsed = time.monotonic() - t0
                if ctx is not None:
                    try:
                        await ctx.report_progress(
                            min(elapsed, timeout), timeout, f"Gemini ({model}) is watching the video... {elapsed:.0f}s"
                        )
                    except Exception:
                        pass
                if elapsed > timeout + 30 and not comm.done():
                    proc.kill()
                    await comm
                    raise RuntimeError(f"agy did not finish within {timeout}s (+30s grace) and was killed.")
            out_b, err_b = await comm
    finally:
        if schema_file:
            try:
                os.unlink(schema_file)
            except OSError:
                pass

    out = out_b.decode("utf-8", errors="replace")
    err = err_b.decode("utf-8", errors="replace")
    raw = _parse_agy_json(out, err)
    raw["_wall_seconds"] = round(time.monotonic() - t0, 1)
    raw["_stderr_tail"] = err[-800:].strip()
    return raw


def _shape(raw: dict[str, Any], model: str, files: list[Path], structured: bool) -> dict[str, Any]:
    denied = raw.get("denied_actions") or []
    ok = raw.get("status") == "SUCCESS" and not denied
    res: dict[str, Any] = {
        "ok": ok,
        "model": model,
        "files": [_file_info(p) for p in files],
        "status": raw.get("status"),
        "seconds": raw.get("_wall_seconds"),
        "usage": raw.get("usage"),
        "agy_conversation_id": raw.get("conversation_id"),
    }
    if denied:
        res["error"] = (
            "agy auto-denied a tool action in headless mode: "
            + ", ".join(f"{a.get('action')} ({a.get('display_name')})" for a in denied)
            + ". If the denied action is read_file, the temporary allow-rule did not match the path the model "
            f"used; check {AGY_SETTINGS}. Other actions are intentionally not permitted."
        )
    if structured:
        review = raw.get("structured_output")
        if review is None:
            text = (raw.get("response") or "").strip()
            try:
                review = json.loads(text)
            except json.JSONDecodeError:
                res["raw_response"] = text
        res["review"] = review
        if ok and review is None:
            res["ok"] = False
            res["error"] = res.get("error") or "Model returned no structured output. See raw_response."
    else:
        res["answer"] = (raw.get("response") or "").strip()
        if ok and not res["answer"]:
            res["ok"] = False
            res["error"] = "Model returned an empty answer. stderr: " + raw.get("_stderr_tail", "")
    return res


def _criteria_list(criteria: str | list[str]) -> list[str]:
    if isinstance(criteria, str):
        parts = [c.strip(" \t-*") for c in re.split(r"\r?\n|;", criteria)]
        items = [re.sub(r"^\d+[.)]\s*", "", c) for c in parts if c.strip()]
    else:
        items = [str(c).strip() for c in criteria if str(c).strip()]
    if not items:
        raise ValueError("At least one criterion is required.")
    return items


def _files_block(paths: list[Path], labels: list[str] | None = None) -> str:
    lines = []
    for i, p in enumerate(paths):
        label = f" ({labels[i]})" if labels else ""
        lines.append(f"{i + 1}. {p}{label}")
    return "\n".join(lines)


GROUND_RULES = (
    "RULES:\n"
    "- Open each listed file with your file-viewing tool and watch it in full before judging. Do NOT run shell "
    "commands, do NOT search or list directories, do NOT read or edit any other file.\n"
    "- Judge only what is visible. Never assume or invent details; if the footage does not let you decide, say so.\n"
    "- Cite concrete evidence with timestamps in mm:ss form (mm:ss.s if precision matters).\n"
    "- Refer to files by their file name in plain text (no markdown links)."
)

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "overall_verdict": {"type": "string", "enum": ["pass", "fail", "partial"]},
        "summary": {"type": "string", "description": "2-4 sentence verdict summary for the requester"},
        "video_description": {"type": "string", "description": "Neutral 2-3 sentence description of what the video shows"},
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "criterion": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["pass", "fail", "partial", "not_assessable"]},
                    "confidence": {"type": "number", "description": "0.0-1.0"},
                    "evidence": {"type": "string", "description": "What was observed, with timestamps"},
                    "timestamps": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "criterion", "verdict", "confidence", "evidence", "timestamps"],
            },
        },
        "issues_found": {
            "type": "array",
            "description": "Notable problems seen that the criteria do not cover",
            "items": {
                "type": "object",
                "properties": {
                    "issue": {"type": "string"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "timestamps": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["issue", "severity", "timestamps"],
            },
        },
        "suggestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["overall_verdict", "summary", "video_description", "criteria", "issues_found", "suggestions"],
}

COMPARE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "overall_verdict": {"type": "string", "enum": ["pass", "fail", "partial"]},
        "summary": {"type": "string"},
        "reference_description": {"type": "string"},
        "candidate_description": {"type": "string"},
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "criterion": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["pass", "fail", "partial", "not_assessable"]},
                    "confidence": {"type": "number"},
                    "reference_observation": {"type": "string"},
                    "candidate_observation": {"type": "string"},
                    "timestamps": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "id", "criterion", "verdict", "confidence",
                    "reference_observation", "candidate_observation", "timestamps",
                ],
            },
        },
        "notable_differences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "difference": {"type": "string"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "timestamps": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["difference", "severity", "timestamps"],
            },
        },
        "suggestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "overall_verdict", "summary", "reference_description", "candidate_description",
        "criteria", "notable_differences", "suggestions",
    ],
}


# --------------------------------------------------------------------------- tools

@mcp.tool()
async def review_video(
    video_path: str,
    criteria: str | list[str],
    context: str = "",
    model: str = "",
    additional_video_paths: list[str] | None = None,
    timeout_seconds: int = 0,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Review a local video file against a list of acceptance criteria using Gemini's native video understanding.

    Args:
        video_path: Path to the video (mp4, webm, mov, mkv, avi, ...). Images (png/jpg) are also accepted.
        criteria: The things to check. A list of short, observable statements, or a newline/semicolon-separated string.
        context: Optional background for the reviewer: what the video is, what is being tested, which region or object matters.
        model: agy model id (see list_video_models). Default: the server's default (gemini-3.1-pro-high).
        additional_video_paths: Optional extra files to view alongside the main one (e.g. other camera angles).
        timeout_seconds: Override the per-call timeout (default 900 s).

    Returns a dict with ok, review {overall_verdict, summary, video_description, criteria[...], issues_found[...],
    suggestions[...]}, model, seconds, usage and files. Each criterion has verdict pass/fail/partial/not_assessable,
    confidence, evidence and timestamps.
    """
    paths = [normalize_path(video_path)] + [normalize_path(p) for p in (additional_video_paths or [])]
    items = _criteria_list(criteria)
    model = model or DEFAULT_MODEL
    crit_block = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(items))
    prompt = (
        "You are a meticulous, sceptical QA reviewer for video content (renders, simulations, recordings, camera feeds).\n\n"
        f"FILES TO VIEW:\n{_files_block(paths)}\n\n"
        + (f"CONTEXT FROM THE REQUESTER:\n{context.strip()}\n\n" if context.strip() else "")
        + f"CRITERIA TO EVALUATE (evaluate every one, keep the same numbering as `id`):\n{crit_block}\n\n"
        f"{GROUND_RULES}\n"
        "- For each criterion give verdict pass / fail / partial / not_assessable with a 0-1 confidence, the evidence "
        "you saw, and the timestamps that support it. Be strict: a criterion that is only mostly met is 'partial'.\n"
        "- Then list any notable problems the criteria do not cover (issues_found) and concrete suggestions to fix "
        "every fail/partial.\n"
        "- overall_verdict is 'pass' only if every criterion passes, 'fail' if any criterion fails, otherwise 'partial'."
    )
    raw = await run_agy(ctx, prompt, model, paths, schema=REVIEW_SCHEMA, timeout=timeout_seconds or None)
    res = _shape(raw, model, paths, structured=True)
    res["criteria_sent"] = items
    return res


@mcp.tool()
async def compare_videos(
    reference_path: str,
    candidate_path: str,
    criteria: str | list[str],
    context: str = "",
    model: str = "",
    timeout_seconds: int = 0,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Compare a candidate video (e.g. your render or simulation) against a reference video (e.g. real footage)
    on a list of criteria, using Gemini. Both files are viewed in one session so the model can judge them side by side.

    Args:
        reference_path: The ground-truth / target video.
        candidate_path: The video being judged.
        criteria: What must match, as a list of short statements or a newline/semicolon-separated string
            (e.g. "dice bounce height and count match", "settle time within 20%", "camera framing is equivalent").
        context: Optional background (what the videos show, which region/object matters, known differences to ignore).
        model: agy model id (see list_video_models). Default: the server's default.
        timeout_seconds: Override the per-call timeout (default 900 s).

    Returns a dict with ok and review {overall_verdict, summary, reference_description, candidate_description,
    criteria[... with reference_observation / candidate_observation], notable_differences[...], suggestions[...]}.
    """
    ref = normalize_path(reference_path)
    cand = normalize_path(candidate_path)
    items = _criteria_list(criteria)
    model = model or DEFAULT_MODEL
    crit_block = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(items))
    prompt = (
        "You are a meticulous, sceptical QA reviewer comparing a CANDIDATE video against a REFERENCE video.\n\n"
        f"FILES TO VIEW:\n{_files_block([ref, cand], ['REFERENCE - the target behaviour', 'CANDIDATE - the one being judged'])}\n\n"
        + (f"CONTEXT FROM THE REQUESTER:\n{context.strip()}\n\n" if context.strip() else "")
        + f"CRITERIA - for each one, judge whether the CANDIDATE matches the REFERENCE (keep the numbering as `id`):\n{crit_block}\n\n"
        f"{GROUND_RULES}\n"
        "- Watch the reference first, then the candidate. For each criterion describe what you observed in each "
        "(with timestamps), then give verdict pass / fail / partial / not_assessable with a 0-1 confidence.\n"
        "- List notable differences the criteria do not cover, and concrete suggestions to bring the candidate "
        "closer to the reference.\n"
        "- overall_verdict is 'pass' only if every criterion passes, 'fail' if any fails, otherwise 'partial'."
    )
    raw = await run_agy(ctx, prompt, model, [ref, cand], schema=COMPARE_SCHEMA, timeout=timeout_seconds or None)
    res = _shape(raw, model, [ref, cand], structured=True)
    res["criteria_sent"] = items
    return res


@mcp.tool()
async def ask_video(
    video_path: str,
    question: str,
    model: str = "",
    additional_video_paths: list[str] | None = None,
    timeout_seconds: int = 0,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Ask Gemini an open question about a local video (or image), e.g. "describe what happens", "at what
    timestamp does the ball stop?", "list every time a die leaves the plate". Returns free-form text with timestamps.

    Args:
        video_path: Path to the video or image file.
        question: What you want to know.
        model: agy model id (see list_video_models). Default: the server's default.
        additional_video_paths: Optional extra files to view alongside the main one.
        timeout_seconds: Override the per-call timeout (default 900 s).
    """
    paths = [normalize_path(video_path)] + [normalize_path(p) for p in (additional_video_paths or [])]
    model = model or DEFAULT_MODEL
    prompt = (
        f"FILES TO VIEW:\n{_files_block(paths)}\n\n"
        f"QUESTION:\n{question.strip()}\n\n"
        f"{GROUND_RULES}\n"
        "- Answer the question directly and concretely, with timestamps where relevant."
    )
    raw = await run_agy(ctx, prompt, model, paths, schema=None, timeout=timeout_seconds or None)
    return _shape(raw, model, paths, structured=False)


@mcp.tool()
async def list_video_models() -> dict[str, Any]:
    """List the model ids available through the Antigravity CLI. Gemini models are video-capable; the others are
    listed for completeness but should not be used for video. Also reports the server's current default."""
    if not Path(AGY_BIN).is_file():
        raise RuntimeError(f"agy binary not found at {AGY_BIN}. Set AGY_BIN.")
    proc = await asyncio.create_subprocess_exec(
        AGY_BIN, "models",
        cwd=AGY_WORKDIR,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "NO_COLOR": "1"},
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("`agy models` timed out after 120 s (not logged in?).")
    models = []
    for line in out_b.decode("utf-8", errors="replace").splitlines():
        if "\t" in line:
            mid, name = line.split("\t", 1)
            mid = mid.strip()
            models.append({"id": mid, "name": name.strip(), "video_capable": mid.startswith("gemini")})
    if not models:
        raise RuntimeError("Could not list models. stderr: " + err_b.decode("utf-8", errors="replace")[-800:])
    return {"default_model": DEFAULT_MODEL, "fast_model": FAST_MODEL_HINT, "models": models}


if __name__ == "__main__":
    mcp.run()
