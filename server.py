import asyncio
import json
import signal
from typing import Set

import websockets


clients: Set[websockets.WebSocketServerProtocol] = set()


async def handle_client(websocket: websockets.WebSocketServerProtocol) -> None:
    clients.add(websocket)
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                # Expecting {"user": str, "text": str}
                if not isinstance(data, dict):
                    continue
                user = data.get("user")
                text = data.get("text")
                if not isinstance(user, str) or not isinstance(text, str):
                    continue
            except json.JSONDecodeError:
                continue

            payload = json.dumps({"user": user, "text": text}, separators=(",", ":"))
            # Broadcast to all connected clients
            if clients:
                await asyncio.gather(*(c.send(payload) for c in list(clients) if c.open), return_exceptions=True)
    finally:
        clients.discard(websocket)


async def main() -> None:
    host = "0.0.0.0"
    port = 8770
    async with websockets.serve(handle_client, host, port, max_size=None, ping_interval=20, ping_timeout=20):
        print(f"listening on {host}:{port}")
        # Run until cancelled
        stop = asyncio.Future()

        def _cancel() -> None:
            if not stop.done():
                stop.set_result(None)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _cancel)
            except NotImplementedError:
                pass

        await stop


if __name__ == "__main__":
    asyncio.run(main())


