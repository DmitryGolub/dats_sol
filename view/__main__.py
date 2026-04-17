import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("view")

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"

clients: set[web.WebSocketResponse] = set()
latest_raw: str | None = None


async def index_handler(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    clients.add(ws)
    log.info("WS client connected (%d total)", len(clients))

    if latest_raw is not None:
        try:
            await ws.send_str(latest_raw)
        except Exception:
            clients.discard(ws)
            return ws

    try:
        async for _msg in ws:
            pass
    finally:
        clients.discard(ws)
        log.info("WS client disconnected (%d total)", len(clients))
    return ws


async def broadcast(message: str) -> None:
    dead: list[web.WebSocketResponse] = []
    for ws in clients:
        try:
            await ws.send_str(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


async def file_watcher(data_file: Path, poll_interval: float) -> None:
    global latest_raw
    last_mtime = 0.0

    while True:
        try:
            stat = os.stat(data_file)
            if stat.st_mtime > last_mtime and stat.st_size > 0:
                raw = data_file.read_text(encoding="utf-8")
                data = json.loads(raw)
                last_mtime = stat.st_mtime
                latest_raw = json.dumps({"type": "state", "data": data})
                log.info(
                    "Arena updated (turn %s, %d bytes)",
                    data.get("turnNo", "?"),
                    len(raw),
                )
                await broadcast(latest_raw)
        except FileNotFoundError:
            pass
        except json.JSONDecodeError:
            pass
        except Exception:
            log.exception("File watcher error")

        await asyncio.sleep(poll_interval)


async def on_startup(app: web.Application) -> None:
    app["watcher"] = asyncio.create_task(
        file_watcher(app["data_file"], app["poll_interval"])
    )


async def on_cleanup(app: web.Application) -> None:
    app["watcher"].cancel()
    try:
        await app["watcher"]
    except asyncio.CancelledError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="DatsSol live visualizer")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--file", type=str, default="data/arena.json")
    parser.add_argument("--poll", type=float, default=0.5)
    args = parser.parse_args()

    data_file = ROOT / args.file

    app = web.Application()
    app["data_file"] = data_file
    app["poll_interval"] = args.poll
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/", index_handler)
    app.router.add_get("/ws", websocket_handler)

    log.info("Watching %s (poll %.1fs)", data_file, args.poll)
    log.info("Starting server on http://localhost:%d", args.port)
    web.run_app(app, host="0.0.0.0", port=args.port, print=None)


if __name__ == "__main__":
    main()
