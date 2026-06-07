#!/home/ty/.local/share/uv/tools/headroom-ai/bin/python
# -*- coding: utf-8 -*-
"""Wrapper script to monkeypatch Headroom proxy and MCP server
without modifying the stock package.
"""

# pylint: disable=import-error, protected-access, broad-exception-caught
# pylint: disable=import-outside-toplevel

import os
import sys

# 1. Apply monkeypatches before importing cli.main
try:
    # Set default TTL from environment variable, defaulting to 1 hour (3600s)
    custom_ttl = int(os.environ.get("HEADROOM_CCR_TTL", "3600"))

    # Import headroom modules
    import headroom.cache.compression_store as cs  # type: ignore
    import headroom.ccr.mcp_server as ms  # type: ignore
    import headroom.config as hc  # type: ignore

    # Patch get_compression_store default argument
    orig_get_compression_store = cs.get_compression_store

    def patched_get_compression_store(
        max_entries=1000, default_ttl=custom_ttl, backend=None
    ):
        """Patched version of get_compression_store to override the default TTL."""
        return orig_get_compression_store(
            max_entries=max_entries, default_ttl=default_ttl, backend=backend
        )

    cs.get_compression_store = patched_get_compression_store

    # Patch CompressionEntry field default
    if hasattr(cs, "CompressionEntry"):
        cs.CompressionEntry.__dataclass_fields__["ttl"].default = custom_ttl  # type: ignore # pylint: disable=no-member

    # Patch CCRConfig default
    if hasattr(hc, "CCRConfig"):
        hc.CCRConfig.store_ttl_seconds = custom_ttl

    # Patch MCP Retrieve handler to output raw strings on success and clean strings on error
    async def patched_handle_retrieve(self, arguments: dict) -> list:
        """Patched handler for the retrieval tool to prevent raw JSON responses."""
        import json
        import re

        from mcp.types import TextContent  # type: ignore

        hash_key = arguments.get("hash")
        if not hash_key:
            return [TextContent(type="text", text="Error: hash parameter is required")]

        query = arguments.get("query")

        visited = set()
        current_hash = hash_key
        result = {}

        # Chase nested CCR markers (read-side self-ref resolution)
        while current_hash and current_hash not in visited:
            visited.add(current_hash)
            result = await self._retrieve_content(current_hash, query)

            # Success case for direct retrieve (no query)
            if "error" not in result and not query and "original_content" in result:
                content = result["original_content"]
                ccr_match = re.fullmatch(r"\s*<<ccr:([a-fA-F0-9]+)[^>]*>>\s*", content)
                if ccr_match:
                    current_hash = ccr_match.group(1).lower()
                    continue  # Resolve the inner hash
                return [TextContent(type="text", text=content)]
            break

        # Error case - output clean error text so agent doesn't parse it as a JSON self-reference
        if "error" in result:
            return [TextContent(type="text", text=f"Error: {result['error']}")]

        # Standard search query case or fallback - return JSON formatting
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if hasattr(ms, "HeadroomMCPServer"):
        ms.HeadroomMCPServer._handle_retrieve = patched_handle_retrieve

    print(
        f"[headroom-wrapper] Patched Headroom default TTL to {custom_ttl}s "
        "and MCP retrieve output format",
        file=sys.stderr,
    )

    # Patch CompressionStore to prevent self-referential hash loops
    if hasattr(cs, "CompressionStore") and hasattr(cs.CompressionStore, "store"):
        orig_store = cs.CompressionStore.store

        def patched_store(self, original: str, compressed: str, **kwargs):
            """Patched CompressionStore.store to avoid self-referential hash loops."""
            import re

            # Check if original is just a CCR marker
            # E.g., <<ccr:abc12345,string,NB>> or <<ccr:abc 100_rows_offloaded>>
            ccr_match = re.fullmatch(r"\s*<<ccr:([a-fA-F0-9]+)[^>]*>>\s*", original)
            if ccr_match:
                # Bypass double compression, just return the existing hash.
                # The proxy will use this hash to format a new marker,
                # leaving it effectively unchanged.
                return ccr_match.group(1).lower()
            return orig_store(self, original, compressed, **kwargs)

        cs.CompressionStore.store = patched_store

    # Patch MCP Server methods to support salt in headroom_compress
    if hasattr(ms, "HeadroomMCPServer"):

        def patched_compress_content(
            self, content: str, salt: str | None = None
        ) -> dict:
            """Patched _compress_content to support salt parameter."""
            import hashlib
            import json

            from headroom.compress import compress

            messages = [{"role": "tool", "content": content}]
            result = compress(messages, model="claude-sonnet-4-5-20250929")

            compressed_content = result.messages[0].get("content", content)
            input_tokens = result.tokens_before
            output_tokens = result.tokens_after

            store = self._get_local_store()

            explicit_hash = None
            if salt:
                explicit_hash = hashlib.sha256((salt + content).encode()).hexdigest()[
                    :24
                ]

            hash_key = store.store(
                original=content,
                compressed=(
                    compressed_content
                    if isinstance(compressed_content, str)
                    else json.dumps(compressed_content)
                ),
                original_tokens=input_tokens,
                compressed_tokens=output_tokens,
                compression_strategy="mcp_compress",
                ttl=ms.MCP_SESSION_TTL,
                explicit_hash=explicit_hash,
            )

            strategy = (
                ", ".join(result.transforms_applied)
                if result.transforms_applied
                else "passthrough"
            )
            self._stats.record_compression(input_tokens, output_tokens, strategy)

            savings_pct = (
                round((1 - result.compression_ratio) * 100, 1)
                if result.compression_ratio < 1.0
                else 0
            )

            return {
                "compressed": compressed_content,
                "hash": hash_key,
                "original_tokens": input_tokens,
                "compressed_tokens": output_tokens,
                "tokens_saved": max(0, input_tokens - output_tokens),
                "savings_percent": savings_pct,
                "transforms": result.transforms_applied,
                "note": (
                    f"Original stored with hash={hash_key}. "
                    "Use mcp__headroom__headroom_retrieve to get full content later."
                ),
            }

        ms.HeadroomMCPServer._compress_content = patched_compress_content

        async def patched_handle_compress(self, arguments: dict) -> list:
            """Patched handler for the compress tool."""
            import asyncio
            import json

            from mcp.types import TextContent

            content = arguments.get("content")
            salt = arguments.get("salt")
            if not content:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"error": "content parameter is required"}),
                    )
                ]

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, self._compress_content, content, salt
            )

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        ms.HeadroomMCPServer._handle_compress = patched_handle_compress

        # Patch _setup_handlers to add `salt` to the schema
        def patched_setup_handlers(self) -> None:
            """Patched _setup_handlers to add salt to inputSchema."""
            import json
            import time

            from headroom.ccr.mcp_server import (
                _READ_ENABLED,
                CCR_TOOL_NAME,
                COMPRESS_TOOL_NAME,
                READ_TOOL_NAME,
                STATS_TOOL_NAME,
                logger,
            )
            from mcp.types import TextContent, Tool

            @self.server.list_tools()
            async def list_tools() -> list[Tool]:
                tools = [
                    Tool(
                        name=COMPRESS_TOOL_NAME,
                        description=(
                            "Compress content to save context window space. "
                            "Use this on large tool outputs, file contents, search results, "
                            "or any content you want to shrink before reasoning over it. "
                            "The original is stored and can be retrieved later "
                            f"via mcp__headroom__{CCR_TOOL_NAME}. "
                            "Returns compressed text + a hash for retrieval."
                        ),
                        inputSchema={
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "description": (
                                        "The content to compress. Can be any text: file contents, "
                                        "JSON, search results, logs, code, etc."
                                    ),
                                },
                                "salt": {
                                    "type": "string",
                                    "description": (
                                        "Optional salt to force a unique hash. Use this "
                                        "when compressing distinct instances of identical "
                                        "content so they don't deduplicate to the same hash."
                                    ),
                                },
                            },
                            "required": ["content"],
                        },
                    ),
                    Tool(
                        name=CCR_TOOL_NAME,
                        description=(
                            "Retrieve original uncompressed content by hash. "
                            "Use this when you need full details from "
                            "previously compressed content. "
                            "The hash comes from headroom_compress results or from compression "
                            "markers like [N items compressed... hash=abc123]."
                        ),
                        inputSchema={
                            "type": "object",
                            "properties": {
                                "hash": {
                                    "type": "string",
                                    "description": (
                                        "Hash key from compression "
                                        "(e.g., 'abc123' from hash=abc123)"
                                    ),
                                },
                                "query": {
                                    "type": "string",
                                    "description": (
                                        "Optional search query to filter results. "
                                        "If provided, returns only items matching the query."
                                    ),
                                },
                            },
                            "required": ["hash"],
                        },
                    ),
                    Tool(
                        name=STATS_TOOL_NAME,
                        description=(
                            "Show compression statistics for this session: "
                            "total compressions, tokens saved, estimated cost savings, "
                            "and recent compression events."
                        ),
                        inputSchema={
                            "type": "object",
                            "properties": {},
                        },
                    ),
                ]

                if _READ_ENABLED:
                    tools.append(
                        Tool(
                            name=READ_TOOL_NAME,
                            description=(
                                "Read a file with smart caching. First read returns full content "
                                "and caches it. Subsequent reads of the same unchanged file return "
                                "a lightweight cache marker (~20 tokens instead of thousands). "
                                "Use mcp__headroom__"
                                f"{CCR_TOOL_NAME} with the hash to get "
                                "full content if needed. "
                                "Use this INSTEAD of the built-in Read "
                                "tool for significant token savings."
                            ),
                            inputSchema={
                                "type": "object",
                                "properties": {
                                    "file_path": {
                                        "type": "string",
                                        "description": "Absolute path to the file to read.",
                                    },
                                    "fresh": {
                                        "type": "boolean",
                                        "description": (
                                            "Force a fresh read, bypassing "
                                            "cache. Use after context "
                                            "compaction, in subagents, or when you need guaranteed "
                                            "current content."
                                        ),
                                    },
                                },
                                "required": ["file_path"],
                            },
                        )
                    )

                return tools

            @self.server.call_tool()
            async def call_tool(name: str, arguments: dict) -> list[TextContent]:
                started = time.perf_counter()
                logger.info(
                    "event=mcp_tool_call_received tool=%s arguments=%s",
                    name,
                    json.dumps(arguments, ensure_ascii=False, default=str),
                )
                try:
                    if name == COMPRESS_TOOL_NAME:
                        result = await self._handle_compress(arguments)
                    elif name == CCR_TOOL_NAME:
                        result = await self._handle_retrieve(arguments)
                    elif name == STATS_TOOL_NAME:
                        result = await self._handle_stats()
                    elif name == READ_TOOL_NAME and _READ_ENABLED:
                        result = await self._handle_read(arguments)
                    else:
                        result = [
                            TextContent(
                                type="text",
                                text=json.dumps({"error": f"Unknown tool: {name}"}),
                            )
                        ]

                    logger.info(
                        "event=mcp_tool_call_completed tool=%s duration_ms=%.2f output=%s",
                        name,
                        (time.perf_counter() - started) * 1000.0,
                        json.dumps(
                            [getattr(item, "text", str(item)) for item in result],
                            ensure_ascii=False,
                            default=str,
                        ),
                    )
                    return result
                except Exception as e:
                    logger.error("Tool %s failed: %s", name, e, exc_info=True)
                    return [
                        TextContent(type="text", text=json.dumps({"error": str(e)}))
                    ]

        ms.HeadroomMCPServer._setup_handlers = patched_setup_handlers

except Exception as e:
    print(
        f"[headroom-wrapper] Warning: Failed to apply monkeypatches: {e}",
        file=sys.stderr,
    )

# 2. Delegate to the original headroom entry point
from headroom.cli import main  # type: ignore


@main.group(name="patterns", short_help="Manage compression patterns")
def patterns_group():
    """Manage patterns used by TOIN."""


@patterns_group.command(name="list")
def patterns_list():
    """List active compression patterns."""
    import json

    import httpx

    try:
        # Try to query proxy if running. The proxy exposes TOIN patterns at /v1/toin/patterns
        # Wait, the proxy might be on a different port, but default is usually 42131
        response = httpx.get("http://127.0.0.1:42131/v1/toin/patterns", timeout=2.0)
        if response.status_code == 200:
            print("Proxy active. Retrieved active TOIN patterns:")
            print(json.dumps(response.json(), indent=2))
            return
    except httpx.HTTPError:
        pass

    print(
        "Proxy not reachable or TOIN stats unavailable. "
        "Displaying default built-in static patterns:\n"
    )
    try:
        from headroom.transforms.code_compressor import _LANGUAGE_PREFILTER
        from headroom.transforms.content_router import (
            _CODE_FENCE_PATTERN,
            _PROSE_PATTERN,
            _SEARCH_RESULT_PATTERN,
        )

        print("Content Router Patterns:")
        print(f"  - Code Fences: {_CODE_FENCE_PATTERN.pattern}")
        print(f"  - Search Results: {_SEARCH_RESULT_PATTERN.pattern}")
        print(f"  - Prose: {_PROSE_PATTERN.pattern}")
        print("\nLanguage Prefilters:")
        for lang, patterns in _LANGUAGE_PREFILTER.items():
            print(f"  - {lang.value}: {len(patterns)} patterns")
    except ImportError:
        print("Could not import built-in patterns.")


if __name__ == "__main__":
    # If sys.argv[0] is this script, Click might show the script name in help, which is fine
    if sys.argv[0].endswith("-script.pyw"):
        sys.argv[0] = sys.argv[0][:-11]
    elif sys.argv[0].endswith(".exe"):
        sys.argv[0] = sys.argv[0][:-4]
    sys.exit(main())  # type: ignore # pylint: disable=no-value-for-parameter
