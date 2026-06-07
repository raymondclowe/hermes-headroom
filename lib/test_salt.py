import asyncio

from headroom_wrapper import ms


async def test():
    server = ms.HeadroomMCPServer()

    res1 = await server._handle_compress({"content": "hello world"})
    res2 = await server._handle_compress({"content": "hello world", "salt": "A"})
    res3 = await server._handle_compress({"content": "hello world", "salt": "B"})

    print("res1:", res1[0].text)
    print("res2:", res2[0].text)
    print("res3:", res3[0].text)


if __name__ == "__main__":
    asyncio.run(test())
