import asyncio
import curses
import json
import re
from typing import List, Optional

import aiohttp

import websockets


WS_URL: str = "ws://10.161.116.188:8770"
USERNAME: str = "madhav"
IS_RASPBERRY: bool = False  # set True on the Raspberry Pi to enable vibration on receive

# AI backends (adjust as needed)
OLLAMA_URL: str = "http://localhost:11434"  # On laptop for TinyLlama
SMOLLM_URL: str = "http://localhost:11434"   # On Pi/laptop smollm served via Ollama-compatible API
SMOLLM_MODEL: str = "smollm2:135m-instruct-q4_K_S"


class ChatUI:
    def __init__(self, stdscr: "curses._CursesWindow") -> None:
        self.stdscr = stdscr
        self.messages: List[str] = []
        self.input_buffer: str = ""
        self.scroll_offset: int = 0  # 0 = follow tail; >0 = scrolled up by N lines

    def append_message(self, text: str) -> None:
        self.messages.append(text)
        if len(self.messages) > 1000:
            self.messages = self.messages[-1000:]

    def draw(self) -> None:
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        msg_area_h = max(1, height - 2)
        total = len(self.messages)
        max_offset = max(0, total - msg_area_h)
        self.scroll_offset = max(0, min(self.scroll_offset, max_offset))
        start = max(0, total - msg_area_h - self.scroll_offset)
        end = min(total, start + msg_area_h)
        visible = self.messages[start:end]
        for i, line in enumerate(visible[-msg_area_h:]):
            attr = curses.color_pair(1)
            if line.startswith("[system]"):
                attr = curses.color_pair(3) | curses.A_BOLD
            elif line.startswith(f"{USERNAME}:"):
                attr = curses.color_pair(2)
            else:
                attr = curses.color_pair(1)
            try:
                self.stdscr.addnstr(i, 0, line, max(1, width - 1), attr)
            except Exception:
                pass
        # separator line
        try:
            self.stdscr.hline(height - 2, 0, ord("-"), max(1, width - 1))
        except Exception:
            pass
        # prompt
        prompt = f"{USERNAME}> {self.input_buffer}"
        try:
            self.stdscr.addnstr(height - 1, 0, prompt, max(1, width - 1), curses.color_pair(4) | curses.A_BOLD)
        except Exception:
            pass
        self.stdscr.refresh()


async def _trigger_vibration() -> None:
    try:
        proc = await asyncio.create_subprocess_shell(
            "python3 -c \"from gpiozero import LED; from time import sleep; led=LED(20); led.on(); sleep(1); led.off()\"",
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
                        # Do not duplicate sender's own messages locally
                        if user == USERNAME:
                            continue
                        ui.append_message(f"{user}: {text}")
                        # Auto-follow only if not scrolled up
                        if ui.scroll_offset == 0:
                            ui.draw()
                        if IS_RASPBERRY and user != USERNAME:
                            asyncio.create_task(_trigger_vibration())
                    except Exception:
                        continue
        except Exception as exc:
            ui.append_message(f"[system] ws error: {repr(exc)}; reconnecting in 2s")
            await asyncio.sleep(2)


async def keyboard_loop(ui: ChatUI, outgoing_raw: "asyncio.Queue[str]") -> None:
    while True:
        ch = ui.stdscr.getch()
        if ch == -1:
            await asyncio.sleep(0.01)
            continue
        # Scrolling
        if ch in (curses.KEY_PPAGE,):  # Page Up
            ui.scroll_offset += 1
            ui.draw()
            continue
        if ch in (curses.KEY_NPAGE,):  # Page Down
            ui.scroll_offset = max(0, ui.scroll_offset - 1)
            ui.draw()
            continue
        if ch == curses.KEY_UP:
            ui.scroll_offset += 1
            ui.draw()
            continue
        if ch == curses.KEY_DOWN:
            ui.scroll_offset = max(0, ui.scroll_offset - 1)
            ui.draw()
            continue
        if ch in (curses.KEY_ENTER, 10, 13):
            text = ui.input_buffer.strip()
            if text:
                await outgoing_raw.put(text)
                # Only echo plain messages locally; commands will be echoed after generation
                if not text.startswith("/"):
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


async def sender_loop(ui: ChatUI, url: str, outgoing_send: "asyncio.Queue[str]") -> None:
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
                    text = await asyncio.wait_for(outgoing_send.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                payload = json.dumps({"user": USERNAME, "text": text}, separators=(",", ":"))
                try:
                    await ws.send(payload)
                except Exception as exc:
                    # put back to queue and break to reconnect
                    await outgoing_send.put(text)
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


async def command_loop(ui: ChatUI, outgoing_raw: "asyncio.Queue[str]", outgoing_send: "asyncio.Queue[str]") -> None:
    while True:
        try:
            text = await asyncio.wait_for(outgoing_raw.get(), timeout=0.05)
        except asyncio.TimeoutError:
            await asyncio.sleep(0.01)
            continue

        # Handle commands
        if text.startswith("/quiz"):
            if USERNAME != "madhav":
                ui.append_message("[system] /quiz is restricted to 'madhav'.")
                ui.draw()
                continue
            topic = text[len("/quiz"):].strip() or "general knowledge"
            ui.append_message(f"[system] generating quiz on '{topic}' ...")
            ui.draw()
            try:
                quiz = await generate_quiz(topic)
                ui.append_message(f"{USERNAME}: {quiz}")
                ui.draw()
                await outgoing_send.put(quiz)
            except Exception as exc:
                ui.append_message(f"[system] quiz failed: {exc}")
                ui.draw()
            continue

        if text.startswith("/summary"):
            if USERNAME == "madhav":
                ui.append_message("[system] /summary is not allowed for 'madhav'.")
                ui.draw()
                continue
            content = text[len("/summary"):].strip()
            if not content:
                ui.append_message("[system] usage: /summary <text>")
                ui.draw()
                continue
            ui.append_message("[system] summarizing ...")
            ui.draw()
            try:
                summ = await summarize_text(content)
                ui.append_message(f"{USERNAME}: {summ}")
                ui.draw()
                await outgoing_send.put(summ)
            except Exception as exc:
                ui.append_message(f"[system] summary failed: {exc}")
                ui.draw()
            continue

        # Not a command: forward as normal
        await outgoing_send.put(text)


async def run(stdscr: "curses._CursesWindow") -> None:
    curses.curs_set(1)
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
        except Exception:
            pass
        # color pairs: 1=others, 2=self, 3=system, 4=prompt
        curses.init_pair(1, curses.COLOR_WHITE, -1)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_GREEN, -1)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    ui = ChatUI(stdscr)
    ui.append_message("[system] Press Enter to send, Backspace to edit")
    ui.draw()
    outgoing_raw: "asyncio.Queue[str]" = asyncio.Queue()
    outgoing_send: "asyncio.Queue[str]" = asyncio.Queue()
    await asyncio.gather(
        ws_receiver(ui, WS_URL),
        keyboard_loop(ui, outgoing_raw),
        command_loop(ui, outgoing_raw, outgoing_send),
        sender_loop(ui, WS_URL, outgoing_send),
    )


async def generate_quiz(topic: str) -> str:
    base_prompt = (
        "Create exactly ONE multiple-choice question about '" + topic + "'. "
        "Output ONLY in this format (no extra text):"\
        "\nQ: <on-topic question ending with ?>"\
        "\nA) <concise option>"\
        "\nB) <concise option>"\
        "\nC) <concise option>"\
        "\nD) <concise option>"\
        "\nConstraints:"\
        "\n- Exactly one option is correct; other three are plausible distractors."\
        "\n- Keep each option under 12 words and similar length/style."\
        "\n- Options must be specific to the topic; no jokes, no meta, no 'True/False' unless directly relevant."\
        "\n- Do NOT include the answer or any explanation."
    )
    strict_prompt = base_prompt + "\nReturn only lines that start with 'Q:' or 'A)'/'B)'/'C)'/'D)'. No other text."

    async def _call_model(p: str) -> str:
        if IS_RASPBERRY:
            return await smollm_generate(p)
        return await ollama_generate(p, model="tinyllama")

    # Try up to 2 times
    raw = await _call_model(base_prompt)
    normalized = _normalize_quiz_output(raw, topic)
    if not _quiz_is_valid(normalized):
        raw = await _call_model(strict_prompt)
        normalized = _normalize_quiz_output(raw, topic)
    # Final fallback: ensure valid structure even if weak content
    if not _quiz_is_valid(normalized):
        normalized = _fallback_quiz(topic)
    return normalized


def _normalize_quiz_output(raw: str, topic: str) -> str:
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    q = None
    opts: List[str] = []
    for ln in lines:
        m = re.match(r"^([A-Da-d])[)\.]\s*(.*)$", ln)
        if m:
            label = m.group(1).upper()
            text = m.group(2).strip()
            if text and text not in ("...", "..."):
                opts.append(f"{label}) {text}")
            continue
        if q is None and '?' in ln and not re.match(r"^[A-Da-d][)\.]", ln):
            if ln.lower().startswith('q:'):
                ln = ln[2:].strip()
            ln = re.sub(r"(?i)multiple[- ]choice.*?format.*?:?\s*", "", ln)
            ln = re.sub(r"(?i)output.*?:?\s*", "", ln)
            q = ln.strip()
    opts = opts[:4]
    # Deduplicate options and filter empty/meta
    seen = set()
    clean_opts: List[str] = []
    for o in opts:
        key = o.split(") ", 1)[-1].lower()
        if key and key not in seen and not key.startswith("option ") and key != "...":
            seen.add(key)
            clean_opts.append(o)
    opts = clean_opts
    # Pad missing options
    labels = ["A", "B", "C", "D"]
    next_idx = 0
    while len(opts) < 4 and next_idx < 4:
        label = labels[next_idx]
        if all(not o.startswith(f"{label})") for o in opts):
            opts.append(f"{label}) {topic} concept")
        next_idx += 1
    if q is None or not q.endswith('?'):
        q = f"What is a key concept of {topic}?"
    return "\n".join([f"Q: {q}"] + opts[:4])


def _quiz_is_valid(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 5:
        return False
    if not lines[0].startswith("Q:") or not lines[0].strip().endswith("?"):
        return False
    labels = ["A)", "B)", "C)", "D)"]
    got = [any(ln.startswith(l) and len(ln) > len(l) + 1 for ln in lines[1:]) for l in labels]
    return all(got)


def _fallback_quiz(topic: str) -> str:
    q = f"Which of the following is a core idea in {topic}?"
    opts = [
        "A) Variables and expressions",
        "B) Photosynthesis",
        "C) Plate tectonics",
        "D) Musical tempo",
    ]
    return "\n".join([f"Q: {q}"] + opts)


async def summarize_text(text: str) -> str:
    prompt = "Summarize concisely in 3-5 bullet points:\n\n" + text
    # Prefer smollm; fallback to ollama if smollm fails
    try:
        return await smollm_generate(prompt)
    except Exception:
        return await ollama_generate(prompt, model="tinyllama")


async def ollama_generate(prompt: str, model: str = "tinyllama") -> str:
    url = OLLAMA_URL.rstrip("/") + "/api/generate"
    payload = {"model": model, "prompt": prompt, "stream": False}
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("response", "") or "(no response)"


async def smollm_generate(prompt: str) -> str:
    # Use Ollama-compatible /api/generate with explicit model
    url = SMOLLM_URL.rstrip("/") + "/api/generate"
    payload = {"model": SMOLLM_MODEL, "prompt": prompt, "stream": False}
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("response", "") or "(no response)"


def main() -> None:
    asyncio.run(curses.wrapper(run))


if __name__ == "__main__":
    main()


