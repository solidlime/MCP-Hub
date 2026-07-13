# Progress Notification Forwarding — Limitation

## Status: Not Implemented

MCP-Hub does **not** forward `notifications/progress` from child MCP servers to connected clients.

## Root Cause

FastMCP's `FastMCPProxy.call_tool(name, arguments)` does not accept `_meta`/`progressToken`.
`ProxyProvider` has zero notification forwarding code.

## Reference Implementation

[hatago-mcp-hub](https://github.com/himorishige/hatago-mcp-hub) supports this via `onprogress` callback in `client.callTool()`:

1. Extracts `progressToken` from `tools/call._meta`
2. Passes `onprogress` callback to child server
3. Forwards `notifications/progress` via:
   - STDIO: `onNotification` callback
   - StreamableHTTP: `sendProgressNotification()`
   - SSE: `SSEManager.sendProgress()`

## Future Implementation Paths

### Option 1: Upstream FastMCP PR (preferred)
Expose `onprogress` callback in `FastMCPProxy.call_tool()`.

### Option 2: Custom Proxy Layer
Subclass or replace `ProxyProvider` with a hand-rolled MCP client proxy that supports progress callbacks.

### Option 3: Tool Call Wrapper
Intercept `tools/call` in the hub's MCP server:
1. Extract `progressToken` from `_meta`
2. Inject `onprogress` callback
3. Call child server
4. Forward notifications back to client

## MCP Spec Context

- Protocol defines `notifications/progress` in JSON-RPC notification layer
- `progressToken` is passed via `_meta` in request params
- `_meta` becomes mandatory for all requests in 2026-07-28 spec
- MCP-Hub's proxy layer (FastMCP) currently doesn't expose this pipeline
