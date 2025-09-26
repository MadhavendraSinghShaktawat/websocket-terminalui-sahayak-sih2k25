import asyncio
import curses
import json
import os
from typing import List, Optional

import websockets


WS_URL: str = os.getenv("CHAT_WS_URL", "ws://192.168.1.100:8770")
USERNAME: str = os.getenv("CHAT_USERNAME", "pi-zero")


class ChatUI:
    def __init__(self, stdscr: "curses._CursesWindow") -> None:
        self.stdscr = stdscr
        self.messages: List[str] = []
        self.input_buffer: str = ""

    def append_message(self, text: str) -> None:
        self.messages.append(text)
        if len(self.messages) > 1000:
            self.messages = self.messages[-1000:]

    def draw(self) -> None:
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        msg_area_h = max(1, height - 2)
        start = max(0, len(self.messages) - msg_area_h)
        visible = self.messages[start:]
        for i, line in enumerate(visible[-msg_area_h:]):
            self.stdscr.addnstr(i, 0, line, max(1, width - 1))
        self.stdscr.hline(height - 2, 0, ord("-"), max(1, width - 1))
        prompt = f"{USERNAME}> {self.input_buffer}"
        self.stdscr.addnstr(height - 1, 0, prompt, max(1, width - 1))
        self.stdscr.refresh()


async def ws_receiver(ui: ChatUI, url: str) -> None:
    while True:
        try:
            async with websockets.connect(url, max_size=None, ping_interval=20, ping_timeout=20) as ws:
                ui.append_message(f"[system] connected to {url}")
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        user = data.get("user", "anon")
                        text = data.get("text", "")
                        ui.append_message(f"{user}: {text}")
                    except Exception:
                        continue
        except Exception as exc:
            ui.append_message(f"[system] ws error: {exc}; reconnecting in 2s")
            await asyncio.sleep(2)


async def keyboard_and_sender(ui: ChatUI, url: str) -> None:
    ws: Optional[websockets.WebSocketClientProtocol] = None
    while True:
        try:
            ws = await websockets.connect(url, max_size=None, ping_interval=20, ping_timeout=20)
            ui.append_message(f"[system] ready to send to {url}")
            while True:
                ch = ui.stdscr.getch()
                if ch == -1:
                    await asyncio.sleep(0.01)
                    continue
                if ch in (curses.KEY_ENTER, 10, 13):
                    text = ui.input_buffer.strip()
                    if text:
                        payload = json.dumps({"user": USERNAME, "text": text}, separators=(",", ":"))
                        try:
                            await ws.send(payload)
                            ui.append_message(f"{USERNAME}: {text}")
                        except Exception as exc:
                            ui.append_message(f"[system] send failed: {exc}")
                    ui.input_buffer = ""
                elif ch in (curses.KEY_BACKSPACE, 127, 8):
                    if ui.input_buffer:
                        ui.input_buffer = ui.input_buffer[:-1]
                elif ch == curses.KEY_RESIZE:
                    pass
                elif 32 <= ch <= 126:
                    ui.input_buffer += chr(ch)
                ui.draw()
        except Exception as exc:
            ui.append_message(f"[system] sender error: {exc}; reconnecting in 2s")
            await asyncio.sleep(2)
        finally:
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
                ws = None


async def run(stdscr: "curses._CursesWindow") -> None:
    curses.curs_set(1)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    ui = ChatUI(stdscr)
    ui.append_message("[system] Press Enter to send, Backspace to edit")
    ui.draw()
    await asyncio.gather(ws_receiver(ui, WS_URL), keyboard_and_sender(ui, WS_URL))


def main() -> None:
    asyncio.run(curses.wrapper(run))


if __name__ == "__main__":
    main()


