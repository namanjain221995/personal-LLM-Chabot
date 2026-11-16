"""``python -m sf_assistant.server``: run the MCP server (:mod:`sf_assistant.server.app`).

    python -m sf_assistant.server --stdio      # Claude Desktop / an IDE starts it as a local process (no token)
    python -m sf_assistant.server --http       # Streamable HTTP on SFA_MCP_BIND:SFA_MCP_PORT/mcp (bearer token)
    python -m sf_assistant.server --health     # GET /healthz of the running HTTP server: exit 0 when it answers

Options: ``--knowledge FILE`` (one object: the 15 one-object knowledge tools; default every file of the knowledge
folder, 18 tools, 21 with the flow components), ``--knowledge-dir DIR`` (default ``SFA_KNOWLEDGE_DIR`` /
``knowledge/objects``), ``--components-dir DIR`` (the flow components, default ``SFA_COMPONENTS_DIR`` /
``knowledge/components``; a folder without ``index.json``: no component tools), ``--warehouse FILE``
(default ``OKEVAL_WAREHOUSE`` / ``SFA_WAREHOUSE``, else the local snapshot ``.private/okeval_run/warehouse.duckdb``; the
container sets the live read copy ``/data/warehouse.read.duckdb``), ``--no-records`` (knowledge tools only).

Environment: ``SFA_MCP_BIND`` (default 127.0.0.1), ``SFA_MCP_PORT`` (8700), ``SFA_MCP_ALLOWED_HOSTS`` (more Host
names, comma separated), ``SFA_MCP_LOG`` (audit log, default ``.private/mcp_log.jsonl``), ``SFA_MCP_TOKEN_FILE``
(default ``.private/mcp_token.json``), ``SFA_MCP_AUDIT_KEY`` (the audit log's HMAC key file, default
``.private/mcp_audit_key``; created when missing and the folder is writable, else a key of this process only),
``SFA_MCP_RATE_PER_MIN`` (tool calls per client per minute, default 30, resource reads and prompt gets 4 times that;
0 = no limit). Start-up lines go to stderr; stdout carries only the protocol in ``--stdio`` mode.
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import sys
import urllib.request


#: The same values as ``sf_assistant.server.app.DEFAULT_BIND`` / ``DEFAULT_PORT`` (tests/test_mcp_server.py checks).
DEFAULT_BIND, DEFAULT_PORT = "127.0.0.1", 8700


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, not {raw!r}") from None


def _health(bind: str, port: int) -> int:
    host = "127.0.0.1" if bind in ("0.0.0.0", "") else bind
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=5) as resp:   # noqa: S310 - fixed URL
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"unhealthy: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(body))
    return 0 if body.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sf_assistant.server", description="The sf_assistant MCP server.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stdio", action="store_true", help="serve over stdin/stdout (a local client starts it)")
    mode.add_argument("--http", action="store_true", help="serve Streamable HTTP on SFA_MCP_BIND:SFA_MCP_PORT/mcp")
    mode.add_argument("--health", action="store_true", help="check the running HTTP server's /healthz")
    parser.add_argument("--knowledge", metavar="FILE", help="one knowledge file (the one-object knowledge tools)")
    parser.add_argument("--knowledge-dir", metavar="DIR", help="the knowledge folder (default knowledge/objects)")
    parser.add_argument("--components-dir", metavar="DIR",
                        help="the flow components folder (default SFA_COMPONENTS_DIR / knowledge/components)")
    parser.add_argument("--warehouse", metavar="FILE", help="the snapshot file the record tools read")
    parser.add_argument("--no-records", action="store_true", help="knowledge tools only")
    args = parser.parse_args(argv)

    bind = (os.environ.get("SFA_MCP_BIND") or "").strip() or DEFAULT_BIND
    port = _env_int("SFA_MCP_PORT", 0) or DEFAULT_PORT
    if args.health:   # the container's healthcheck: no package import beyond this module
        return _health(bind, port)

    from sf_assistant.server import app as app_mod

    from sf_assistant.config import load_settings
    from sf_assistant.server.audit import AuditLog, RateLimiter, load_key
    from sf_assistant.server.auth import TokenStore
    from sf_assistant.server.data import Records, load_knowledge

    log = lambda line: print(f"sf-assistant-mcp: {line}", file=sys.stderr, flush=True)   # noqa: E731
    try:
        settings = load_settings(records=not args.no_records, warehouse=args.warehouse,
                                 knowledge_dir=args.knowledge_dir, components_dir=args.components_dir)
    except (ValueError, TypeError) as exc:
        log(f"bad setting: {exc}")
        return 2
    key, key_how = load_key(os.environ.get("SFA_MCP_AUDIT_KEY") or str(app_mod.DEFAULT_KEY_FILE))
    if key_how == "ephemeral":
        log("the audit key file could not be read or created: argument hashes of this run use a key of this process "
            "only (they match no other run)")
    audit = AuditLog(os.environ.get("SFA_MCP_LOG") or str(app_mod.DEFAULT_LOG), key=key)
    try:
        audit.check()
    except OSError as exc:
        log(f"cannot append to the audit log {audit.path}: {exc}; not starting without it")
        return 2
    try:
        knowledge = load_knowledge(settings.knowledge_dir, args.knowledge, resolver=settings.resolver,
                                   components=settings.components_dir)
    except (OSError, ValueError) as exc:
        log(f"knowledge not loaded: {exc}")
        return 2
    for line in knowledge.resolver_lines:   # the spelling help of the knowledge tools (none when it is off)
        log(line)
    records = (Records(settings.records, knowledge.folder, log=log, resolver=settings.resolver)
               if settings.records is not None else None)
    tokens = TokenStore(os.environ.get("SFA_MCP_TOKEN_FILE") or str(app_mod.DEFAULT_TOKEN_FILE)) if args.http else None
    limiter = RateLimiter(_env_int("SFA_MCP_RATE_PER_MIN", app_mod.DEFAULT_RATE))
    # HTTP: a request without the token id BearerAuth sets is refused (no stdio default identity)
    sfa = app_mod.SfaServer(knowledge, records, audit, limiter=limiter, tokens=tokens,
                            default_client="stdio" if args.stdio else None)
    log(f"{len(sfa.kinds)} tools ({len(knowledge.rows)} knowledge objects, "
        + "".join(f"{n} {k} components, " for k, n in knowledge.components.items()) + "records "
        f"{'on' if records is not None and records.on else 'off'}), audit log {audit.path}")
    atexit.register(sfa.close)   # the private knowledge copy goes away however the process ends
    try:
        if args.stdio:
            import anyio

            # a client that stops the server sends SIGTERM: end like at EOF (the finally below cleans up)
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            anyio.run(app_mod.serve_stdio, sfa)
        else:
            state = tokens.state()   # type: ignore[union-attr]
            if state != "ok":
                log(f"no valid MCP token in {tokens.path} ({state}): every request is refused until the owner runs "
                    "./run.sh new-mcp-token")
            hosts = tuple(h.strip() for h in (os.environ.get("SFA_MCP_ALLOWED_HOSTS") or "").split(",") if h.strip())
            log(f"Streamable HTTP on http://{bind}:{port}/mcp (rate {limiter.limit or 'unlimited'} calls/min/client)")
            app_mod.run_http(sfa, bind=bind, port=port, allowed_hosts=hosts)
    except KeyboardInterrupt:
        pass
    finally:
        sfa.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
