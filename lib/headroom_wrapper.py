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
    import headroom.config as hc  # type: ignore
    import headroom.ccr.mcp_server as ms  # type: ignore

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
        cs.CompressionEntry.__dataclass_fields__["ttl"].default = custom_ttl

    # Patch CCRConfig default
    if hasattr(hc, "CCRConfig"):
        hc.CCRConfig.store_ttl_seconds = custom_ttl

    # Patch MCP Retrieve handler to output raw strings on success and clean strings on error
    async def patched_handle_retrieve(self, arguments: dict) -> list:
        """Patched handler for the retrieval tool to prevent raw JSON responses."""
        from mcp.types import TextContent  # type: ignore
        import json

        hash_key = arguments.get("hash")
        if not hash_key:
            return [TextContent(type="text", text="Error: hash parameter is required")]

        query = arguments.get("query")
        # Call the original retrieve method
        result = await self._retrieve_content(hash_key, query)

        # Success case for direct retrieve (no query)
        if "error" not in result and not query and "original_content" in result:
            return [TextContent(type="text", text=result["original_content"])]

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

except Exception as e:
    print(
        f"[headroom-wrapper] Warning: Failed to apply monkeypatches: {e}",
        file=sys.stderr,
    )

# 2. Delegate to the original headroom entry point
from headroom.cli import main  # type: ignore

if __name__ == "__main__":
    # If sys.argv[0] is this script, Click might show the script name in help, which is fine
    if sys.argv[0].endswith("-script.pyw"):
        sys.argv[0] = sys.argv[0][:-11]
    elif sys.argv[0].endswith(".exe"):
        sys.argv[0] = sys.argv[0][:-4]
    sys.exit(main())
