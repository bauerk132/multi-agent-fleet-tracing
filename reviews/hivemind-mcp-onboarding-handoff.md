# Handoff: Connect Muse to HiveMind MCP (Tailscale funnel blocker)

**To:** Antigravity
**From:** Muse (via Kyle)
**Date:** 2026-11-06
**Goal:** Get Muse connected to the HiveMind MCP server on Kyle's laptop (DESKTOP-QQ98CIM), so Muse becomes a first-class participant in the multi-agent coordination system (AgentScope / Fleet Tracing project).

## Current state (verified)

- HiveMind MCP server **is running** on the laptop: `hivemind serve --http --host 0.0.0.0 --port 8766` → `Streamable HTTP on http://0.0.0.0:8766/mcp (read-write)`, session manager started.
- Auth: `HIVEMIND_TOKEN` is set (random GUID) in Kyle's PowerShell session env var. Server accepts `Authorization: Bearer <token>` or path token (`/mcp/<token>`).
- Port 8765 is taken by AgentScope (`agentscope.cli serve`, PID 10136, loopback only) — **do NOT kill it**; that is why HiveMind is on 8766.
- Muse's VM is on Kyle's Tailscale tailnet (joined 2026-11-06, VM IP 100.83.107.109). The laptop is reachable by name `desktop-qq98cim` through the tunnel.
- Muse probed `http://desktop-qq98cim:8766/mcp` (MCP initialize POST, no token) → empty reply (curl 52). Expected without a token; not proof of failure.
- Muse's secure token capture requires a **public DNS hostname**. The tailnet name `desktop-qq98cim` was rejected. That is why the funnel is needed: a `https://<machine>.<tailnet>.ts.net` URL qualifies.

## The blocker (your job)

`tailscale funnel --bg 8766` cannot run: `tailscale` is not on PATH in PowerShell, and `C:\Program Files\Tailscale\tailscale.exe` does not exist. Tailscale **is** running on the laptop (the machine is on the tailnet), but the CLI is not where expected. Last diagnostic asked of Kyle (pending at handoff time): `Get-CimInstance Win32_Service -Filter "Name='Tailscale'" | Select-Object Name, PathName, State` — to find the real install path.

## What to do

1. Find the Tailscale CLI on the laptop (service path, Program Files (x86), per-user install, or Store app location). If no CLI exists, install the standalone Tailscale client (which ships the CLI) or enable funnel from the Tailscale admin console — whichever is least disruptive. Do **not** uninstall the running Tailscale.
2. Start the funnel for port 8766 (`tailscale funnel --bg 8766` or equivalent). Confirm it serves the HiveMind server.
3. Report back the public funnel URL (e.g. `https://<machine>.<tailnet>.ts.net`) and confirm the endpoint responds.
4. Note: the `HIVEMIND_TOKEN` currently lives **only** in Kyle's PowerShell session env var. If that window closes, the server and token go with it. Consider persisting the token (e.g. HiveMind secrets store or a stored env var) so the server survives a restart — but do **not** change the token without telling Kyle, and **never** put the token value in chat, a file, or a handoff.
5. Do **not** kill the AgentScope process on 8765. Do **not** change the HiveMind port/host (`0.0.0.0:8766`) without a reason.

## What happens next (Muse's part, not yours)

Once the funnel URL is known, Muse will open the secure token box for Kyle (token never in chat), then verify with an authenticated MCP initialize + tools/list call, then build the connector doc (custom connector, MCP client template) and confirm the connection is live.

## Key facts

- Laptop: DESKTOP-QQ98CIM, user kbaue. HiveMind install: `C:\Users\kbaue\.hivemind-app` (venv python), data `C:\Users\kbaue\.hivemind`, source `C:\Users\kbaue\hivemind-src\hivemind`.
- HiveMind: FastMCP server, stdio by default; HTTP mode flags: `--http --host --port --path-token --read-only --insecure-localhost`.
- Muse VM tailnet IP: 100.83.107.109. Laptop MagicDNS: `desktop-qq98cim`.
- AgentScope repo: `bauerk132/agentscope` (private); Sprint 1 on `ags/sprint1-staging`. Fleet tracing repo: `bauerk132/multi-agent-fleet-tracing`.
