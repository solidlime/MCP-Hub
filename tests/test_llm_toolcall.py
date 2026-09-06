"""実LLM（OpenRouter・無料モデル固定）によるツールコール往復テスト。

README の「ツールコール成功率」を LLM 実呼び出しで検証する:
LLM エージェントに Hub のメタツール (search_tools / execute_tool /
list_upstream_tools) を渡し、2-hop 発見フロー (search_tools → execute_tool)
を LLM 自身に生成させ、実行まで Hub 経由で往復させる。

実 API を叩くため OPENROUTER_API_KEY がない場合はスキップ:

    OPENROUTER_API_KEY=sk-or-... python -m pytest tests/test_llm_toolcall.py -q
    # モデル変更: OPENROUTER_MODEL=<model_id> で上書き可
"""

import asyncio
import json
import os
import sys
import tempfile

import httpx
import pytest
from fastmcp import FastMCP

from mcp_hub.meta_provider import create_meta_app
from mcp_hub.proxy_manager import ProxyManager
from mcp_hub.store import JsonStore
from mcp_hub.streamable_http_patch import apply_patch

apply_patch()

_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
_BASE = "https://openrouter.ai/api/v1"
# openrouter/free エイリアスは routed 先が変わるため、tools 対応の
# 特定無料モデルに固定（2026-09 時点の動作確認済み）。
_MODEL = os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")

pytestmark = pytest.mark.skipif(not _API_KEY, reason="OPENROUTER_API_KEY not set")

_SCRIPT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "test_servers", "stdio_echo_server.py")
)

# Hub メタツールの OpenAI 形式ツール定義（meta_provider.py のシグネチャと一致）
META_TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "search_tools",
            "description": (
                "Search upstream tools available on the hub. "
                "Always call FIRST before execute_tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": 'What you want to do (e.g. "echo a message")',
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
            "name": "list_upstream_tools",
            "description": (
                "List all upstream tools grouped by server. "
                "Use for orientation, then search_tools."
            ),
            "parameters": {"type": "object", "properties": {}},
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
                    "server": {
                        "type": "string",
                        "description": "From search_tools results",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "From search_tools results",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Use inputSchema from search_tools results",
                    },
                },
                "required": ["server", "tool_name", "arguments"],
            },
        },
    },
]


async def _hub_with_echo(name: str = "stdio-echo"):
    """Echo サーバーを接続済みの Hub を作る（test_llm_agent.py と同型）。"""
    mcp = FastMCP("hub")
    tmpdir = tempfile.mkdtemp()
    registry = JsonStore(data_dir=tmpdir)
    await registry.init()
    pm = ProxyManager(mcp, registry)
    meta_app = create_meta_app(pm)
    await meta_app.rebuild_index()
    pm.on_change(lambda: meta_app.rebuild_index())

    await pm.register_server(name, {"command": sys.executable, "args": [_SCRIPT]})
    for _ in range(60):
        s = pm.get_all_status().get(name)
        if s != "connecting":
            break
        await asyncio.sleep(0.2)
    assert pm.get_all_status()[name] == "connected", pm.get_all_status()
    await meta_app.rebuild_index()
    return pm, meta_app


async def _dispatch(meta_app, name: str, args: dict) -> str:
    """LLM の tool_call を Hub メタツールに振り分ける。"""
    if name == "search_tools":
        return await meta_app.meta_tools.search_tools(args["query"])
    if name == "list_upstream_tools":
        return await meta_app.meta_tools.list_upstream_tools()
    if name == "execute_tool":
        out = await meta_app.meta_tools.execute_tool(
            server=args.get("server", ""),
            tool_name=args.get("tool_name", ""),
            arguments=args.get("arguments") or {},
        )
        return str(out)
    return json.dumps({"error": f"unknown tool: {name}"})


async def _run_agent(meta_app, prompt: str, max_turns: int = 8):
    """OpenRouter LLM にメタツールを与えてループ実行。

    Returns (final_text, calls): calls は [(tool_name, args), ...]。
    """
    messages: list = [{"role": "user", "content": prompt}]
    calls: list[tuple[str, dict]] = []
    headers = {
        "Authorization": f"Bearer {_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        for _ in range(max_turns):
            resp = await client.post(
                f"{_BASE}/chat/completions",
                headers=headers,
                json={"model": _MODEL, "messages": messages, "tools": META_TOOL_DEFS},
            )
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return msg.get("content") or "", calls
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": tool_calls,
                }
            )
            for call in tool_calls:
                name = call["function"]["name"]
                args = json.loads(call["function"].get("arguments") or "{}")
                calls.append((name, args))
                out = await _dispatch(meta_app, name, args)
                messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": out}
                )
    return "", calls


class TestLLMToolCall:
    """実LLMによる 2-hop ツールコール往復。"""

    @pytest.mark.asyncio
    async def test_meta_tool_call_roundtrip(self):
        pm, meta_app = await _hub_with_echo("stdio-echo")

        prompt = (
            "You are an agent connected to an MCP-Hub. "
            "Find a tool that echoes a message (use search_tools first), "
            "then execute it with the exact message 'hello from llm'. "
            "Report the tool's raw output in your final answer."
        )
        final, calls = await _run_agent(meta_app, prompt)

        names = [n for n, _ in calls]
        assert "search_tools" in names, calls
        assert "execute_tool" in names, calls
        exec_args = [a for n, a in calls if n == "execute_tool"]
        assert any(a.get("tool_name") == "echo" for a in exec_args), exec_args
        assert "ECHO" in final
        assert "hello from llm" in final

        await pm.unregister_server("stdio-echo")
