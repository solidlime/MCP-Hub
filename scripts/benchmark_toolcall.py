#!/usr/bin/env python3
"""Benchmark: reproduce README "ツール呼び出し成功率" table with real LLM calls.

Runs each server row (stdio / Streamable HTTP / SSE / 202-async) through the
Hub meta tools (Meta ON) and direct upstream tools (Meta OFF), measuring
per-cell success rate with OpenRouter chat.completions.

Usage:
    .venv\\Scripts\\python.exe -u scripts/benchmark_toolcall.py --trials 3

Env:
    OPENROUTER_API_KEY  (required)
    OPENROUTER_MODEL    (default: deepseek/deepseek-v4-flash-0731)
    EXA_API_KEY         (optional; exa row is skipped without it)
"""

# ruff: noqa: E402  (sys.path bootstrap must precede mcp_hub imports)

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import httpx
from fastmcp import FastMCP

from mcp_hub.meta_provider import create_meta_app
from mcp_hub.proxy_manager import ProxyManager
from mcp_hub.store import JsonStore
from mcp_hub.streamable_http_patch import apply_patch

apply_patch()

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
MODEL = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash-0731")
MAX_TURNS = 8
RATE_LIMIT_DELAYS = [2, 4, 8, 16, 32]  # seconds, max 5 retries on 429

SYSTEM_META_ON = (
    "You are connected to MCP-Hub. Only the hub meta tools are available "
    "(search_tools, list_upstream_tools, execute_tool). Use search_tools first "
    "to discover the right upstream tool, then execute_tool to run it. "
    "Once the task is done, answer briefly."
)
SYSTEM_META_OFF = (
    "You are an assistant with direct MCP tool access. "
    "Use the appropriate tool to complete the task, then answer briefly."
)

META_TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "search_tools",
            "description": "Search upstream tools by query. Always call FIRST before execute_tool.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What you want to do"},
                    "top_k": {
                        "type": "integer",
                        "description": "Max results (default 10)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_upstream_tools",
            "description": "List all upstream tools grouped by server. Use for orientation, then search_tools.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_tool",
            "description": "Execute a tool discovered via search_tools. "
            "Pass server and tool_name from search_tools results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "server": {
                        "type": "string",
                        "description": "Server name from search_tools results",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "Tool name from search_tools results",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Tool arguments (from inputSchema)",
                    },
                },
                "required": ["server", "tool_name"],
            },
        },
    },
]


class RateLimited(Exception):
    """OpenRouter kept returning 429 after all retries."""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Server matrix
# ---------------------------------------------------------------------------


@dataclass
class Row:
    key: str
    protocol: str
    server: str
    declared_tools: int
    config: dict
    prompt: str
    expected_tool: str
    expect: str
    launch: tuple[str, str, int] | None = (
        None  # (script_relpath, port_env, default_port)
    )
    require_env: str | None = None
    # results
    on: list[str] = field(default_factory=list)  # pass / fail / rate_limited
    off: list[str] = field(default_factory=list)
    actual_tools: int | None = None
    error: str | None = None


def build_rows() -> list[Row]:
    npx = shutil.which("npx") or "npx"
    docs = str(REPO_ROOT / "docs")
    return [
        Row(
            key="filesystem",
            protocol="stdio",
            server="filesystem",
            declared_tools=14,
            config={
                "command": npx,
                "args": ["-y", "@modelcontextprotocol/server-filesystem", docs],
            },
            prompt="docsディレクトリのファイル一覧を取得して",
            expected_tool="list_directory",
            expect="architecture.md",
        ),
        Row(
            key="sequential-thinking",
            protocol="stdio",
            server="sequential-thinking",
            declared_tools=1,
            config={
                "command": npx,
                "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
            },
            prompt="sequentialthinkingツールを使って、2+3を段階的に考えて",
            expected_tool="sequentialthinking",
            expect="thoughtHistoryLength",
        ),
        Row(
            key="exa",
            protocol="Streamable HTTP",
            server="exa",
            declared_tools=2,
            config={"url": os.environ.get("EXA_MCP_URL", "https://mcp.exa.ai/mcp")},
            prompt="Webで'openrouter'を検索して",
            expected_tool="web_search",
            expect="openrouter",
            require_env="EXA_API_KEY",
        ),
        Row(
            key="sse-echo",
            protocol="SSE",
            server="sse-echo",
            declared_tools=1,
            launch=("tests/test_servers/sse_echo_server.py", "SSE_ECHO_PORT", 18766),
            config={"url": "http://localhost:18766/sse"},
            prompt="echoツールでhello-benchを返して",
            expected_tool="sse_echo",
            expect="hello-bench",
        ),
        Row(
            key="async-mcp",
            protocol="Streamable HTTP (202 async)",
            server="async-mcp",
            declared_tools=1,
            launch=("tests/test_servers/async_mcp_server.py", "ASYNC_MCP_PORT", 18765),
            config={"url": "http://localhost:18765/mcp"},
            prompt="async-mcpサーバーのsearch_asyncツールでクエリ'hello'を検索して",
            expected_tool="search_async",
            expect="async result",
        ),
    ]


# ---------------------------------------------------------------------------
# Hub fixture (same shape as tests/test_llm_agent.py::_hub_with_echo)
# ---------------------------------------------------------------------------


class Hub:
    def __init__(self) -> None:
        self.mcp = FastMCP("bench-hub")
        self.tmpdir = tempfile.mkdtemp(prefix="mcp-bench-")
        self.registry = JsonStore(data_dir=self.tmpdir)
        self.pm: Any = None  # ProxyManager, set in start()
        self.meta_app: Any = None

    async def start(self) -> None:
        await self.registry.init()
        self.pm = ProxyManager(self.mcp, self.registry)
        self.meta_app = create_meta_app(self.pm)
        await self.meta_app.rebuild_index()

    async def register(self, name: str, config: dict) -> None:
        assert self.pm is not None
        await self.pm.register_server(name, config)
        status = None
        for _ in range(360):  # up to 180s (first npx run downloads the package)
            await asyncio.sleep(0.5)
            status = self.pm.get_all_status().get(name)
            if status != "connecting":
                break
        if status != "connected":
            raise RuntimeError(f"server '{name}' failed to connect (status={status})")
        await self.meta_app.rebuild_index()

    async def unregister(self, name: str) -> None:
        try:
            await self.pm.unregister_server(name)
        except Exception as exc:  # best effort
            log(f"  warn: unregister {name} failed: {exc}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def to_text(out: object) -> str:
    if out is None:
        return ""
    if isinstance(out, str):
        return out
    content = getattr(out, "content", None)
    if content is not None:
        parts = [getattr(c, "text", None) or str(c) for c in content]
        return "\n".join(parts)
    if isinstance(out, list):
        return "\n".join(getattr(c, "text", None) or str(c) for c in out)
    return str(out)


def looks_error(text: str) -> bool:
    try:
        d = json.loads(text)
        return isinstance(d, dict) and "error" in d
    except (ValueError, TypeError):
        return False


async def chat(messages: list[dict], tools: list[dict]) -> dict:
    """One OpenRouter chat completion with backoff on 429 / error bodies.
    Returns assistant message."""
    async with httpx.AsyncClient(timeout=180.0) as client:
        for attempt in range(1 + len(RATE_LIMIT_DELAYS)):
            resp = await client.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {API_KEY}"},
                json={"model": MODEL, "messages": messages, "tools": tools},
            )
            if resp.status_code == 429:
                if attempt >= len(RATE_LIMIT_DELAYS):
                    raise RateLimited("429 persisted after all retries")
                delay = RATE_LIMIT_DELAYS[attempt]
                log(f"  429 rate-limited, retrying in {delay}s ...")
                await asyncio.sleep(delay)
                continue
            resp.raise_for_status()
            body = resp.json()
            if "choices" not in body:
                # OpenRouter sometimes returns 2xx with an error body
                # (provider failure, moderation, ...). Retry with backoff.
                err = json.dumps(body.get("error") or body, ensure_ascii=False)[:300]
                if attempt >= len(RATE_LIMIT_DELAYS):
                    raise RuntimeError(f"OpenRouter error response: {err}")
                delay = RATE_LIMIT_DELAYS[attempt]
                log(f"  OpenRouter error body, retrying in {delay}s: {err}")
                await asyncio.sleep(delay)
                continue
            return body["choices"][0]["message"]
    raise RateLimited("unreachable")


async def tool_defs_from_proxy(pm: ProxyManager, server: str) -> list[dict]:
    """Convert upstream tools to OpenAI function defs (Meta OFF)."""
    proxy = pm.get_proxy(server)
    if proxy is None:
        raise RuntimeError(f"no proxy for {server}")
    defs = []
    for t in await _list_tools(proxy):
        schema = getattr(t, "parameters", None)
        if schema is None:
            raw = getattr(t, "inputSchema", None)
            schema = (
                raw
                if isinstance(raw, dict)
                else (
                    raw.model_dump()
                    if raw is not None
                    else {"type": "object", "properties": {}}
                )
            )
        defs.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": schema,
                },
            }
        )
    return defs


async def _list_tools(proxy) -> list:
    try:
        tools = await proxy.get_tools()
        return list(tools.values())
    except AttributeError:
        return list(await proxy.list_tools())


# ---------------------------------------------------------------------------
# Trial runners
# ---------------------------------------------------------------------------


def _snippet(m: object) -> str:
    c = m.get("content") if isinstance(m, dict) else None
    return (str(c) or "")[:150].replace("\n", " ")


async def run_meta_on(hub: Hub, row: Row) -> tuple[bool, str]:
    executed: list[tuple[str, str]] = []
    searched = False
    messages = [
        {"role": "system", "content": SYSTEM_META_ON},
        {"role": "user", "content": row.prompt},
    ]
    for _ in range(MAX_TURNS):
        msg = await chat(messages, META_TOOL_DEFS)
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            break
        for tc in calls:
            fn = tc["function"]
            name = fn["name"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            if name == "search_tools":
                out = await hub.meta_app.meta_tools.search_tools(
                    args.get("query", ""), int(args.get("top_k") or 10)
                )
                searched = True
            elif name == "list_upstream_tools":
                out = await hub.meta_app.meta_tools.list_upstream_tools()
            elif name == "execute_tool":
                out = await hub.meta_app.meta_tools.execute_tool(
                    server=args.get("server", ""),
                    tool_name=args.get("tool_name", ""),
                    arguments=args.get("arguments"),
                )
                executed.append((args.get("tool_name", ""), to_text(out)))
            else:
                out = json.dumps({"error": f"unknown tool {name}"})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": to_text(out)[:4000],
                }
            )
    detail = f"search={'Y' if searched else 'N'} exec={[t for t, _ in executed]}"
    ok_tool = any(t.lower() == row.expected_tool.lower() for t, _ in executed)
    ok_text = any(row.expect.lower() in txt.lower() for _, txt in executed)
    no_err = not any(looks_error(txt) for _, txt in executed)
    ok = searched and ok_tool and ok_text and no_err
    if not ok:
        detail += f" last={_snippet(messages[-1])}"
    return (ok, detail)


async def run_meta_off(hub: Hub, row: Row, tool_defs: list[dict]) -> tuple[bool, str]:
    names = {d["function"]["name"] for d in tool_defs}
    called: list[tuple[str, str, str | None]] = []  # (tool, text, error)
    messages = [
        {"role": "system", "content": SYSTEM_META_OFF},
        {"role": "user", "content": row.prompt},
    ]
    for _ in range(MAX_TURNS):
        msg = await chat(messages, tool_defs)
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            break
        for tc in calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            if name in names:
                try:
                    result = await hub.pm.call_tool(row.server, name, args)
                    text, err = to_text(result), None
                except Exception as exc:
                    text, err = "", str(exc)
                called.append((name, text, err))
                out = text if err is None else f"error: {err}"
            else:
                out = f"error: unknown tool {name}; available: {sorted(names)}"
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": out[:4000],
                }
            )
    detail = f"calls={[(t, e) for t, _, e in called]}"
    ok = any(
        t.lower() == row.expected_tool.lower()
        and err is None
        and row.expect.lower() in txt.lower()
        for t, txt, err in called
    )
    if not ok:
        detail += f" last={_snippet(messages[-1])}"
    return (ok, detail)


# ---------------------------------------------------------------------------
# External test-server lifecycle
# ---------------------------------------------------------------------------


def wait_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("localhost", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def start_external(row: Row) -> subprocess.Popen | None:
    if row.launch is None:
        return None
    script, port_env, default_port = row.launch
    port = int(os.environ.get(port_env, str(default_port)))
    env = dict(os.environ)
    env[port_env] = str(port)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / script)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not wait_port(port, timeout=30):
        proc.terminate()
        raise RuntimeError(f"{row.server}: external server did not open port {port}")
    return proc


def stop_external(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def fmt_cell(statuses: list[str]) -> str:
    ok = statuses.count("pass")
    rl = statuses.count("rate_limited")
    total = ok + statuses.count("fail")  # rate_limited excluded
    pct = f"{100 * ok / total:.0f}%" if total else "n/a"
    return f"{pct} ({ok}/{total})" + (f" +{rl}rl" if rl else "")


async def main_async(trials: int) -> None:
    if not API_KEY:
        log("ERROR: OPENROUTER_API_KEY is not set")
        sys.exit(2)
    log(f"model={MODEL} trials={trials}")

    rows = build_rows()
    hub = Hub()
    await hub.start()

    for row in rows:
        if row.require_env and not os.environ.get(row.require_env):
            log(f"[{row.key}] skipped (no {row.require_env})")
            row.on = row.off = ["skipped"]
            continue

        proc = None
        registered = False
        try:
            proc = start_external(row)
            await hub.register(row.server, row.config)
            registered = True

            tool_defs = await tool_defs_from_proxy(hub.pm, row.server)
            row.actual_tools = len(tool_defs)
            log(f"[{row.key}] connected, {row.actual_tools} upstream tools")

            for mode, runner in (("Meta ON", None), ("Meta OFF", None)):
                for trial in range(1, trials + 1):
                    try:
                        if mode == "Meta ON":
                            ok, detail = await run_meta_on(hub, row)
                        else:
                            ok, detail = await run_meta_off(hub, row, tool_defs)
                    except RateLimited:
                        log(
                            f"[{row.key} | {mode}] trial {trial}/{trials}: rate_limited"
                        )
                        (row.on if mode == "Meta ON" else row.off).append(
                            "rate_limited"
                        )
                        continue
                    except Exception as exc:
                        ok, detail = False, f"{type(exc).__name__}: {exc}"
                    (row.on if mode == "Meta ON" else row.off).append(
                        "pass" if ok else "fail"
                    )
                    log(
                        f"[{row.key} | {mode}] trial {trial}/{trials}: {'PASS' if ok else 'FAIL'} ({detail})"
                    )
        except Exception as exc:
            row.error = f"{type(exc).__name__}: {exc}"
            log(f"[{row.key}] ERROR: {row.error}")
        finally:
            if registered:
                await hub.unregister(row.server)
            stop_external(proc)

    # -- Markdown table (stdout) ------------------------------------------
    rate_limited_any = any("rate_limited" in s for r in rows for s in r.on + r.off)

    print("| プロトコル | サーバー | ツール数 | Meta ON | Meta OFF |")
    print("|-----------|--------|:---:|:---:|:---:|")
    tot = {"on": [0, 0], "off": [0, 0]}  # ok, total
    for row in rows:
        if row.on == row.off == ["skipped"]:
            print(
                f"| {row.protocol} | {row.key} | {row.declared_tools} | skipped (no {row.require_env}) | skipped (no {row.require_env}) |"
            )
            continue
        if row.error and not row.on and not row.off:
            print(
                f"| {row.protocol} | {row.key} | {row.declared_tools} | error | error |"
            )
            continue
        print(
            f"| {row.protocol} | {row.key} | {row.declared_tools} | {fmt_cell(row.on)} | {fmt_cell(row.off)} |"
        )
        for key, statuses in (("on", row.on), ("off", row.off)):
            tot[key][0] += statuses.count("pass")
            tot[key][1] += statuses.count("fail") + statuses.count("pass")
    tot_on = (
        f"{100 * tot['on'][0] / tot['on'][1]:.0f}% ({tot['on'][0]}/{tot['on'][1]})"
        if tot["on"][1]
        else "n/a"
    )
    tot_off = (
        f"{100 * tot['off'][0] / tot['off'][1]:.0f}% ({tot['off'][0]}/{tot['off'][1]})"
        if tot["off"][1]
        else "n/a"
    )
    print(f"| **合計** | | **19** | **{tot_on}** | **{tot_off}** |")

    if rate_limited_any:
        print()
        print(
            "> 一部の試行は OpenRouter の 429 (rate limit) で rate_limited となり、集計から除外しています。"
        )
    print()
    print(f"<!-- model={MODEL} trials={trials} -->")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trials", type=int, default=3, help="trials per cell (default 3)")
    args = ap.parse_args()
    asyncio.run(main_async(args.trials))


if __name__ == "__main__":
    main()
