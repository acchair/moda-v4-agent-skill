# Moda Search Stack

This stack provides a local SearXNG endpoint for moda-v4. It is usable from Windows, Linux, and macOS through Docker Desktop or Docker Engine. OpenMinis should use a reachable remote endpoint instead of running Docker inside the Minis sandbox.

## Start SearXNG

```text
cd deploy/search-stack
copy .env.example .env       # Windows PowerShell: Copy-Item .env.example .env
docker compose up -d
```

On Linux/macOS, use `cp .env.example .env`. Replace `YOUR_SEARXNG_SECRET_HERE` with a local random value before starting. The default endpoint is `http://127.0.0.1:8888`.

For OpenMinis or another device to reach SearXNG, set `SEARXNG_BIND_ADDRESS=0.0.0.0` only on a trusted LAN, or put SearXNG behind an authenticated reverse proxy. Do not expose an unauthenticated public instance.

The configuration keeps Chinese-capable engines first: Baidu, 360 Search, Sogou, Quark, Bing, DuckDuckGo, and Brave. SearXNG is the aggregator; individual engine failures are isolated and temporarily suspended.

## Connect moda-v4

Set these variables in the moda-v4 `.env`:

```env
MODA_SEARCH_PROVIDER=auto
SEARXNG_URL=http://127.0.0.1:8888
DDG_MCP_URL=http://127.0.0.1:7070/mcp
DDG_HTML_URL=https://html.duckduckgo.com/html/
DDG_LITE_URL=https://lite.duckduckgo.com/lite/
```

Start the optional DuckDuckGo MCP with the portable Python launcher:

```text
python tools/search_stack.py start-ddg --json   # Windows
python3 tools/search_stack.py start-ddg --json  # Linux/macOS/OpenMinis
```

Check the stack:

```text
python tools/search_stack.py check --json
```

## OpenMinis

OpenMinis normally cannot host Docker or a long-running local MCP service. Point `SEARXNG_URL` and `DDG_MCP_URL` to services reachable from Minis. If they are unavailable, moda-v4 automatically uses public DuckDuckGo HTML and Lite, preserving `需人工确认` for failed or unverified evidence.
