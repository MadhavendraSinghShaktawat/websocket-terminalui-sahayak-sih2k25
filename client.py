import asyncio
import curses
import json
from typing import List, Optional

import websockets


WS_URL: str = "ws://10.161.116.188:8770"
USERNAME: str = "madhav"
IS_RASPBERRY: bool = False  # set True on the Raspberry Pi to enable vibration on receive


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


async def _trigger_vibration() -> None:
    try:
        proc = await asyncio.create_subprocess_shell(
            "python3 -c \"from gpiozero import LED; from time import sleep; led=LED(20); led.on(); sleep(5); led.off()\"",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Let it run independently; do not await
    except Exception:
        pass


async def ws_receiver(ui: ChatUI, url: str) -> None:
    while True:
        try:
            async with websockets.connect(url, max_size=None, ping_interval=20, ping_timeout=20) as ws:
                ui.append_message(f"[system] connected to {url}")
                ui.draw()
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        user = data.get("user", "anon")
                        text = data.get("text", "")
                        ui.append_message(f"{user}: {text}")
                        ui.draw()
                        if IS_RASPBERRY and user != USERNAME:
                            asyncio.create_task(_trigger_vibration())
                    except Exception:
                        continue
        except Exception as exc:
            ui.append_message(f"[system] ws error: {repr(exc)}; reconnecting in 2s")
            await asyncio.sleep(2)


async def keyboard_loop(ui: ChatUI, outgoing: "asyncio.Queue[str]") -> None:
    while True:
        ch = ui.stdscr.getch()
        if ch == -1:
            await asyncio.sleep(0.01)
            continue
        if ch in (curses.KEY_ENTER, 10, 13):
            text = ui.input_buffer.strip()
            if text:
                await outgoing.put(text)
                ui.append_message(f"{USERNAME}: {text}")
            ui.input_buffer = ""
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            if ui.input_buffer:
                ui.input_buffer = ui.input_buffer[:-1]
        elif ch == curses.KEY_RESIZE:
            pass
        elif 32 <= ch <= 126:
            ui.input_buffer += chr(ch)
        ui.draw()


async def sender_loop(ui: ChatUI, url: str, outgoing: "asyncio.Queue[str]") -> None:
    ws: Optional[websockets.WebSocketClientProtocol] = None
    while True:
        try:
            ws = await websockets.connect(url, max_size=None, ping_interval=20, ping_timeout=20)
            # Announce join once per successful connection
            try:
                await ws.send(json.dumps({"user": USERNAME, "text": "[joined]"}, separators=(",", ":")))
            except Exception:
                pass
            while True:
                try:
                    text = await asyncio.wait_for(outgoing.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                payload = json.dumps({"user": USERNAME, "text": text}, separators=(",", ":"))
                try:
                    await ws.send(payload)
                except Exception as exc:
                    # put back to queue and break to reconnect
                    await outgoing.put(text)
                    raise exc
        except Exception as exc:
            ui.append_message(f"[system] send loop: {repr(exc)}; reconnecting in 2s")
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
    outgoing: "asyncio.Queue[str]" = asyncio.Queue()
    await asyncio.gather(
        ws_receiver(ui, WS_URL),
        keyboard_loop(ui, outgoing),
        sender_loop(ui, WS_URL, outgoing),
    )


def main() -> None:
    asyncio.run(curses.wrapper(run))


if __name__ == "__main__":
    main()


