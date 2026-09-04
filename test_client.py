"""Smoke-test the gemini-video MCP server over stdio, the way a real MCP client would.

Usage:
    uv run --with "mcp<2" python test_client.py --video clip.mp4 [tests...]
    uv run --with "mcp<2" python test_client.py --video clip.mp4 --reference real.mp4 --candidate ours.mp4

Tests: tools models ask review compare   (default: every test whose inputs were given)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = str(Path(__file__).with_name("server.py"))


async def progress(p, total, msg):
    print(f"   [progress] {p:.0f}/{total} {msg}", flush=True)


async def call(s, name, args):
    t0 = time.time()
    r = await s.call_tool(name, args, progress_callback=progress)
    txt = r.content[0].text
    print(f"== {name} ({time.time() - t0:.0f}s) isError={r.isError}")
    try:
        print(json.dumps(json.loads(txt), indent=1)[:4000])
    except Exception:
        print(txt[:4000])
    return txt


async def main(a):
    which = set(a.tests) if a.tests else {"tools", "models", "ask", "review", "compare"}
    params = StdioServerParameters(command="uv", args=["run", SERVER])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            if "tools" in which:
                tools = await s.list_tools()
                print("TOOLS:", [t.name for t in tools.tools])
            if "models" in which:
                await call(s, "list_video_models", {})
            if "ask" in which and a.video:
                await call(s, "ask_video", {
                    "video_path": a.video,
                    "question": "Describe what happens, and at what timestamp does all motion stop?",
                    "model": a.fast_model,
                })
            if "review" in which and a.video:
                await call(s, "review_video", {
                    "video_path": a.video,
                    "context": a.context,
                    "criteria": [
                        "The video is not blank, black, or frozen at any point",
                        "No object visibly clips through or sinks into another object or surface",
                        "All motion looks physically plausible for the scene",
                    ],
                })
            if "compare" in which and a.reference and a.candidate:
                await call(s, "compare_videos", {
                    "reference_path": a.reference,
                    "candidate_path": a.candidate,
                    "criteria": "same scene and camera framing; same objects present; comparable motion and timing",
                    "context": a.context,
                    "model": a.fast_model,
                })


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", help="clip for the ask and review tests")
    ap.add_argument("--reference", help="reference clip for the compare test")
    ap.add_argument("--candidate", help="candidate clip for the compare test")
    ap.add_argument("--context", default="", help="optional context passed to review/compare")
    ap.add_argument("--fast-model", default="gemini-3.8-flash-high", help="model for the quick tests")
    ap.add_argument("tests", nargs="*", help="subset of: tools models ask review compare")
    asyncio.run(main(ap.parse_args()))
