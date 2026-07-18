#!/usr/bin/env python3
"""
Benchmark: meta_mode ON vs OFF — tool call success rate comparison.

Usage:
    pip install openai
    export OPENROUTER_API_KEY="sk-or-v1-..."
    python .benchmarks/meta_mode_benchmark.py

Requirements: Python 3.12+, openai
"""

import os
import sys
import json
import time
import statistics
from datetime import datetime, timezone
from typing import Any

from openai import OpenAI


# ── Configuration ──────────────────────────────────────────────────────────

API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "tencent/hy3:free"
TIMEOUT = 60  # seconds per request
RUNS = 3      # number of runs per test case

# ── Test Cases ─────────────────────────────────────────────────────────────

# Each entry: (query, primary_tool, alt_tools_off, keywords_on)
#   query           — Japanese prompt sent to the model
#   primary_tool    — expected tool name for OFF-mode display
#   alt_tools_off   — alternative tool names that also count as success in OFF mode
#   keywords_on     — keywords to find in search_tools() query argument (ON mode)

TEST_CASES: list[tuple[str, str, list[str], list[str]]] = [
    (
        "最新のAIニュースを検索して",
        "brave_web_search",
        ["web_search_exa"],
        ["search", "web", "brave"],
    ),
    (
        "example.comをブラウザで開いて",
        "puppeteer_navigate",
        [],
        ["browser", "navigate", "puppeteer"],
    ),
    (
        "東京のピザ屋を探して",
        "brave_local_search",
        ["brave_web_search"],
        ["local", "search", "brave"],
    ),
    (
        "現在のページのスクリーンショットを撮って",
        "puppeteer_screenshot",
        [],
        ["screenshot", "puppeteer"],
    ),
    (
        "フォームの国選択ドロップダウンから「日本」を選んで",
        "puppeteer_select",
        ["puppeteer_click"],
        ["select", "dropdown", "puppeteer", "form", "choose"],
    ),
    (
        "ログインボタンをクリックして",
        "puppeteer_click",
        [],
        ["click", "puppeteer"],
    ),
    (
        "検索ボックスに「MCP Hub」と入力して",
        "puppeteer_fill",
        ["puppeteer_click"],
        ["fill", "input", "puppeteer", "form"],
    ),
]

# ── Tool Definitions ───────────────────────────────────────────────────────

TOOLS_OFF: list[dict[str, Any]] = [
    # brave-search
    {
        "type": "function",
        "function": {
            "name": "brave_web_search",
            "description": "Performs a web search using the Brave Search API, ideal for general queries, news, articles, and online content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (max 400 chars, 50 words)",
                    },
                    "count": {
                        "type": "number",
                        "description": "Number of results (1-20, default 10)",
                    },
                    "offset": {
                        "type": "number",
                        "description": "Pagination offset (max 9, default 0)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "brave_local_search",
            "description": "Searches for local businesses and places using Brave's Local Search API. Best for queries related to physical locations, businesses, restaurants, services, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Local search query (e.g. 'pizza near Central Park')",
                    },
                    "count": {
                        "type": "number",
                        "description": "Number of results (1-20, default 5)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    # puppeteer
    {
        "type": "function",
        "function": {
            "name": "puppeteer_navigate",
            "description": "Navigate to a URL",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to navigate to",
                    },
                    "launchOptions": {
                        "type": "object",
                        "description": "PuppeteerJS LaunchOptions. Default null.",
                    },
                    "allowDangerous": {
                        "type": "boolean",
                        "description": "Allow dangerous LaunchOptions. Default false.",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "puppeteer_screenshot",
            "description": "Take a screenshot of the current page or a specific element",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name for the screenshot",
                    },
                    "selector": {
                        "type": "string",
                        "description": "CSS selector for element to screenshot",
                    },
                    "width": {
                        "type": "number",
                        "description": "Width in pixels (default: 800)",
                    },
                    "height": {
                        "type": "number",
                        "description": "Height in pixels (default: 600)",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "puppeteer_click",
            "description": "Click an element on the page",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector for element to click",
                    },
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "puppeteer_fill",
            "description": "Fill out an input field",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector for input field",
                    },
                    "value": {
                        "type": "string",
                        "description": "Value to fill",
                    },
                },
                "required": ["selector", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "puppeteer_hover",
            "description": "Hover an element on the page",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector for element to hover",
                    },
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "puppeteer_select",
            "description": "Select an element on the page with Select tag",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector for element to select",
                    },
                    "value": {
                        "type": "string",
                        "description": "Value to select",
                    },
                },
                "required": ["selector", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "puppeteer_evaluate",
            "description": "Execute JavaScript in the browser console",
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "description": "JavaScript code to execute",
                    },
                },
                "required": ["script"],
            },
        },
    },
    # sequential-thinking
    {
        "type": "function",
        "function": {
            "name": "sequentialthinking",
            "description": "A detailed tool for dynamic and reflective problem-solving through thoughts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought": {
                        "type": "string",
                        "description": "Your current thinking step",
                    },
                    "nextThoughtNeeded": {
                        "type": "boolean",
                        "description": "Whether another thought step is needed",
                    },
                    "thoughtNumber": {
                        "type": "integer",
                        "description": "Current thought number",
                    },
                    "totalThoughts": {
                        "type": "integer",
                        "description": "Estimated total thoughts needed",
                    },
                    "isRevision": {
                        "type": "boolean",
                    },
                    "revisesThought": {
                        "type": "integer",
                    },
                    "branchFromThought": {
                        "type": "integer",
                    },
                    "branchId": {
                        "type": "string",
                    },
                    "needsMoreThoughts": {
                        "type": "boolean",
                    },
                },
                "required": ["thought", "thoughtNumber", "totalThoughts"],
            },
        },
    },
    # websearch/exa
    {
        "type": "function",
        "function": {
            "name": "web_search_exa",
            "description": "Search the web for any topic and get clean, ready-to-use content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query",
                    },
                    "numResults": {
                        "type": "number",
                        "description": "Number of search results to return (default: 10)",
                    },
                },
                "required": ["query"],
            },
        },
    },
]

TOOLS_ON: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_tools",
            "description": (
                "Search across all upstream server tools by keyword or capability.\n\n"
                "Returns matching tools with their full inputSchema — use execute_tool "
                "directly with the returned server/name and inputSchema parameters. "
                "Always search first before trying to use any tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Natural language description of what you want to do "
                            "(e.g. 'read files', 'search web')"
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of results to return",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tool_schema",
            "description": (
                "Get the full input schema for a specific tool on a specific server.\n\n"
                "Always call this after search_tools to learn the exact parameters "
                "needed before calling execute_tool."
            ),
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
                },
                "required": ["server", "tool_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_tool",
            "description": "Execute a tool on any upstream server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "server": {
                        "type": "string",
                        "description": "Server name from search_tools results",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": (
                            "Tool name from search_tools results "
                            "(use get_tool_schema first)"
                        ),
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Tool parameters matching the tool's input schema",
                    },
                },
                "required": ["server", "tool_name", "arguments"],
            },
        },
    },
]

TOOLS_ON_NEW: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_tools",
            "description": "Search upstream tools. Always call FIRST before execute_tool.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What you want to do (e.g. 'read files', 'search web')",
                    },
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
            "name": "execute_tool",
            "description": "Execute a tool discovered via search_tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "server": {"type": "string", "description": "From search_tools results"},
                    "tool_name": {"type": "string", "description": "From search_tools results"},
                    "arguments": {"type": "object", "description": "Use inputSchema from search_tools results"},
                },
                "required": ["server", "tool_name", "arguments"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_upstream_tools",
            "description": "List all upstream tools grouped by server. Use for orientation, then search_tools.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]

# ── Core Logic ─────────────────────────────────────────────────────────────


def check_tool_call_off(
    tool_calls: list[Any] | None,
    primary_tool: str,
    alt_tools: list[str],
) -> bool:
    """Check if any tool call matches the expected tool name (OFF mode)."""
    if not tool_calls:
        return False
    acceptable = {primary_tool, *alt_tools}
    for tc in tool_calls:
        if tc.function.name in acceptable:
            return True
    return False


def check_tool_call_on(
    tool_calls: list[Any] | None,
    keywords: list[str],
) -> bool:
    """Check if search_tools was called with relevant keywords (ON mode)."""
    if not tool_calls:
        return False
    for tc in tool_calls:
        if tc.function.name == "search_tools":
            try:
                args = json.loads(tc.function.arguments)
                query_arg = args.get("query", "")
            except (json.JSONDecodeError, TypeError):
                continue
            query_lower = query_arg.lower()
            if any(kw.lower() in query_lower for kw in keywords):
                return True
    return False


def test_single(
    client: OpenAI,
    query: str,
    primary_tool: str,
    alt_tools_off: list[str],
    keywords_on: list[str],
    tools: list[dict[str, Any]],
    mode: str,
) -> bool:
    """Send one query, return whether tool call matched expectations."""
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": query}],
            tools=tools,
            temperature=0.0,
            timeout=TIMEOUT,
        )
    except Exception as exc:
        print(f"  [API ERROR] {type(exc).__name__}: {exc}")
        return False

    msg = response.choices[0].message
    tool_calls = msg.tool_calls

    if mode == "OFF":
        return check_tool_call_off(tool_calls, primary_tool, alt_tools_off)
    else:
        return check_tool_call_on(tool_calls, keywords_on)


def run_benchmark(
    client: OpenAI,
    mode: str,
    tools: list[dict[str, Any]],
    runs: int = RUNS,
) -> dict[str, Any]:
    """Run all test cases N times, return summary stats."""
    results: dict[str, Any] = {
        "mode": mode,
        "model": MODEL,
        "runs_per_case": runs,
        "cases": [],
        "summary": {},
    }
    total_passed = 0
    total_attempts = 0

    for idx, (query, primary_tool, alt_tools_off, keywords_on) in enumerate(
        TEST_CASES
    ):
        passed = 0
        errors = 0
        for i in range(runs):
            ok = test_single(
                client, query, primary_tool, alt_tools_off, keywords_on, tools, mode
            )
            if ok:
                passed += 1
            # Simple progress indicator
            sys.stdout.write("." if ok else "x")
            sys.stdout.flush()
        print()  # end line for this test case

        case_name = f"search_tools → {primary_tool}"
        total_passed += passed
        total_attempts += runs
        success_rate = round(passed / runs * 100, 1)

        results["cases"].append(
            {
                "query": query,
                "expected_tool": primary_tool,
                "passed": passed,
                "total": runs,
                "success_rate": success_rate,
            }
        )
        print(
            f"  {case_name:<42s}  {passed}/{runs} ({success_rate}%)"
        )

    overall_rate = round(total_passed / total_attempts * 100, 1) if total_attempts else 0.0
    results["summary"] = {
        "total_passed": total_passed,
        "total_attempts": total_attempts,
        "overall_success_rate": overall_rate,
    }
    return results


# ── Display ────────────────────────────────────────────────────────────────


def print_header() -> None:
    print()
    print("=" * 60)
    print("  MCP-Hub meta_mode Benchmark")
    print("=" * 60)
    print(f"  Model:     {MODEL}")
    print(f"  Runs/case: {RUNS}")
    print(f"  Base URL:  {BASE_URL}")
    print()


def print_comparison(
    results_off: dict[str, Any],
    results_on: dict[str, Any],
    results_new: dict[str, Any],
) -> None:
    """Print formatted comparison table with 3 columns."""
    print()
    header = f"{'Test Case':<42s} | {'meta_mode OFF':<13s} | {'OLD meta':<13s} | {'NEW meta':<13s}"
    print(header)
    sep = "-" * 42 + "-+-" + "-" * 13 + "-+-" + "-" * 13 + "-+-" + "-" * 13
    print(sep)

    for c_off, c_on, c_new in zip(results_off["cases"], results_on["cases"], results_new["cases"]):
        assert c_off["expected_tool"] == c_on["expected_tool"] == c_new["expected_tool"], "case mismatch"
        case_name = f"search_tools → {c_off['expected_tool']}"
        off_str = f"{c_off['passed']}/{c_off['total']} ({c_off['success_rate']}%)"
        on_str = f"{c_on['passed']}/{c_on['total']} ({c_on['success_rate']}%)"
        new_str = f"{c_new['passed']}/{c_new['total']} ({c_new['success_rate']}%)"
        print(f"{case_name:<42s} | {off_str:>13s} | {on_str:>13s} | {new_str:>13s}")

    print(sep)
    so = results_off["summary"]
    sn = results_on["summary"]
    sn_new = results_new["summary"]
    off_total = f"{so['total_passed']}/{so['total_attempts']} ({so['overall_success_rate']}%)"
    on_total = f"{sn['total_passed']}/{sn['total_attempts']} ({sn['overall_success_rate']}%)"
    new_total = f"{sn_new['total_passed']}/{sn_new['total_attempts']} ({sn_new['overall_success_rate']}%)"

    off_label = f"meta_mode OFF:  {so['total_passed']}/{so['total_attempts']} ({so['overall_success_rate']}%)"
    on_label = f"OLD meta:       {sn['total_passed']}/{sn['total_attempts']} ({sn['overall_success_rate']}%)"
    new_label = f"NEW meta:       {sn_new['total_passed']}/{sn_new['total_attempts']} ({sn_new['overall_success_rate']}%)"

    print(f"{'OVERALL':<42s} | {off_total:>13s} | {on_total:>13s} | {new_total:>13s}")
    print()
    print(f"  {off_label}")
    print(f"  {on_label}")
    print(f"  {new_label}")
    print()


def save_results(
    results_off: dict[str, Any],
    results_on: dict[str, Any],
    results_new: dict[str, Any],
    path: str = ".benchmarks/results.json",
) -> None:
    """Save benchmark results to JSON."""
    payload = {
        "benchmark": "meta_mode_tool_call_benchmark",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "runs_per_case": RUNS,
        "meta_mode_off": results_off,
        "meta_mode_on": results_on,
        "meta_mode_new": results_new,
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"  Results saved → {path}")


# ── Main ───────────────────────────────────────────────────────────────────


def main() -> None:
    # Validate API key
    if not API_KEY:
        print("ERROR: OPENROUTER_API_KEY environment variable not set.", file=sys.stderr)
        print("       export OPENROUTER_API_KEY='sk-or-v1-...'", file=sys.stderr)
        sys.exit(1)

    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

    print_header()

    # ── meta_mode OFF benchmark ──
    print(">>> meta_mode OFF (15 tools directly available)")
    print("-" * 60)
    results_off = run_benchmark(client, "OFF", TOOLS_OFF)
    print()

    # ── OLD meta benchmark ──
    print(">>> OLD meta (3 progressive discovery tools: search_tools + get_tool_schema + execute_tool)")
    print("-" * 60)
    results_on = run_benchmark(client, "ON", TOOLS_ON)
    print()

    # ── NEW meta benchmark ──
    print(">>> NEW meta (3 slim tools: search_tools + execute_tool + list_upstream_tools)")
    print("-" * 60)
    results_new = run_benchmark(client, "ON", TOOLS_ON_NEW)
    print()

    # ── comparison ──
    print_comparison(results_off, results_on, results_new)
    save_results(results_off, results_on, results_new)

    print("Done.")


if __name__ == "__main__":
    main()
