### Minimal LAN chat (WebSocket) for Raspberry Pi Zero (client) and laptop (server)

#### What you get
- Python WebSocket broadcast server (`server.py`) for your laptop.
- Ultra-light curses-based terminal chat client (`client.py`) for the Pi Zero.

#### Requirements
- Python 3.11 on both.
- Install deps:

```bash
pip install -r requirements.txt
```

#### Run the server on the laptop
```bash
python server.py
```
It listens on `0.0.0.0:8765`.

#### Run the client on the Pi Zero
Set the laptop IP and your username via env vars (or edit `client.py`):

```bash
export CHAT_WS_URL=ws://<LAPTOP_IP>:8765
export CHAT_USERNAME=pi-zero
python client.py
```

Controls: type text; Enter sends; Backspace edits. The UI is single-line input with scrollback capped to 1000 lines.

#### Notes
- Plain WS over LAN (no TLS). For security, keep on trusted LAN or tunnel via SSH.
- Single global room, no persistence.
- Designed for very small terminals and low CPU usage on Pi Zero.


