import asyncio
import json
import os

import yaml


async def test_server(name, config):
    cmd = config.get("command")
    if not cmd:
        return
    args = config.get("args", [])

    # Run the command
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **config.get("env", {})},
        )

        # Send initialize
        req = '{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}}}\n'
        proc.stdin.write(req.encode())
        await proc.stdin.drain()

        # Read response
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
            if not line:
                print(f"❌ {name}: Connection closed immediately")
            else:
                try:
                    resp = json.loads(line)
                    if "result" in resp or "error" in resp:
                        print(f"✅ {name}: OK")
                    else:
                        print(f"⚠️ {name}: Invalid JSON-RPC response")
                except json.JSONDecodeError:
                    print(f"❌ {name}: Invalid JSON: {line.decode().strip()[:50]}")
        except asyncio.TimeoutError:
            print(f"⏳ {name}: TIMEOUT (hanging!)")

        # Clean up
        proc.kill()
    except Exception as e:
        print(f"❌ {name}: Failed to start - {e}")


async def main():
    with open(os.path.expanduser("~/.hermes/config.yaml")) as f:
        config = yaml.safe_load(f)
    servers = config.get("mcp_servers", {})
    for name, srv in servers.items():
        await test_server(name, srv)


asyncio.run(main())
