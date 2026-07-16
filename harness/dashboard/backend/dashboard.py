#!/usr/bin/env python3
"""Local dashboard backend for the ActNow KR260 demonstration."""

import argparse
import asyncio
import json
import os
import struct
import webbrowser
from pathlib import Path

from aiohttp import WSMsgType, web

MAGIC = b"ACT1"
HDR = struct.Struct("<4sIH")
ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "software/application/main.c"
BLOCKS = ROOT / "software/application/transform.blocks.json"
FIRMWARE = ROOT / "software/build/rom.mem"
SERVER = ROOT / "harness/pynq/actnow_fpga_server.py"
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
               "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2"]
TOOLCHAIN_BIN = "/opt/riscv/bin"
EVENT_QUEUE_PACKETS = 256
WEBSOCKET_BATCH_BYTES = 64 * 1024


class Dashboard:
    def __init__(self, args):
        self.args = args
        self.websockets = set()
        self.ssh = None
        self.build_lock = asyncio.Lock()
        self.stats = {"words": 0, "packets": 0, "dropped": 0, "sequence": None}
        self.board = {"connected": False, "firmware": "unknown", "counters": {}}
        self.log_lines = []
        self.event_queue = asyncio.Queue(maxsize=EVENT_QUEUE_PACKETS)

    async def log(self, message, level="info"):
        print(f"[{level}] {message}", flush=True)
        line = {"type": "log", "level": level, "message": message}
        self.log_lines.append(line)
        self.log_lines = self.log_lines[-400:]
        asyncio.create_task(self.broadcast_json(line))

    async def broadcast_json(self, value):
        dead = []
        for ws in self.websockets:
            try:
                await asyncio.wait_for(ws.send_json(value), 1)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.websockets.discard(ws)

    async def broadcast_binary(self, payload):
        dead = []
        for ws in self.websockets:
            try:
                await asyncio.wait_for(ws.send_bytes(payload), 0.1)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.websockets.discard(ws)

    def enqueue_binary(self, payload):
        if self.event_queue.full():
            self.event_queue.get_nowait()
        self.event_queue.put_nowait(payload)

    async def event_sender(self):
        while True:
            first = await self.event_queue.get()
            batch = bytearray(first)
            while len(batch) < WEBSOCKET_BATCH_BYTES:
                try:
                    payload = self.event_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                room = WEBSOCKET_BATCH_BYTES - len(batch)
                if len(payload) <= room:
                    batch.extend(payload)
                else:
                    batch.extend(payload[:room])
                    self.enqueue_binary(payload[room:])
                    break
            await self.broadcast_binary(bytes(batch))

    async def run(self, command, cwd=ROOT, env=None):
        await self.log("+ " + " ".join(map(str, command)))
        proc = await asyncio.create_subprocess_exec(
            *map(str, command), cwd=cwd,
            env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        lines = []
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            lines.append(line)
            await self.log(line)
        code = await proc.wait()
        return code, "\n".join(lines)

    async def deploy(self):
        if self.ssh and self.ssh.returncode is None:
            self.ssh.terminate()
            await self.ssh.wait()
        remote = f"{self.args.user}@{self.args.kria}"
        remote_dir = self.args.remote_dir.rstrip("/")
        code, _ = await self.run(["ssh", *SSH_OPTIONS, remote,
                                  "mkdir", "-p", remote_dir])
        if code:
            raise RuntimeError("failed to create remote directory")
        code, _ = await self.run([
            "scp", *SSH_OPTIONS, SERVER, self.args.xsa, FIRMWARE,
            f"{remote}:{remote_dir}/"])
        if code:
            raise RuntimeError("failed to copy dashboard assets")
        await self.run([
            "ssh", *SSH_OPTIONS, remote, "sudo", "pkill", "-f",
            "[a]ctnow_fpga_server.py"])
        command = (
            f"cd {remote_dir} && sudo bash -lc "
            f"'source /etc/profile.d/pynq_venv.sh && python3 actnow_fpga_server.py "
            f"--host {self.args.listen_host} --port {self.args.udp_port} "
            f"--raw-port {self.args.raw_udp_port} "
            f"--control-port {self.args.remote_control_port} "
            f"--xsa {Path(self.args.xsa).name} --firmware {FIRMWARE.name}'")
        self.ssh = await asyncio.create_subprocess_exec(
            "ssh", *SSH_OPTIONS, "-o", "ExitOnForwardFailure=yes", "-L",
            f"{self.args.control_port}:127.0.0.1:{self.args.remote_control_port}",
            remote, command, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        asyncio.create_task(self.pipe_server_log())
        for _ in range(120):
            await asyncio.sleep(0.25)
            try:
                reply = await self.control({"command": "status"})
                self.board.update(connected=True, firmware=reply.get("firmware", "unknown"),
                                  counters=reply.get("counters", {}))
                await self.broadcast_state()
                return
            except OSError:
                pass
        raise RuntimeError("KR260 control tunnel did not become ready")

    async def deploy_reported(self):
        try:
            await self.deploy()
        except Exception as exc:
            self.board["connected"] = False
            await self.log(f"deployment failed: {exc}", "error")
            await self.broadcast_state()

    async def pipe_server_log(self):
        if not self.ssh or not self.ssh.stdout:
            return
        async for raw in self.ssh.stdout:
            await self.log("KR260: " + raw.decode(errors="replace").rstrip())
        self.board["connected"] = False
        await self.broadcast_state()

    async def control(self, request):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.args.control_port)
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), 15)
        writer.close()
        await writer.wait_closed()
        reply = json.loads(line)
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "board command failed"))
        return reply

    async def build(self, source=None):
        async with self.build_lock:
            if source is not None:
                SOURCE.write_text(source)
                await self.log(f"saved {SOURCE.relative_to(ROOT)}")
            env = os.environ.copy()
            env["PATH"] = TOOLCHAIN_BIN + os.pathsep + env.get("PATH", "")
            code, output = await self.run(
                ["make", "-C", "software", "PROG=application"], env=env)
            diagnostics = parse_diagnostics(output)
            result = {"ok": code == 0, "diagnostics": diagnostics}
            asyncio.create_task(self.broadcast_json({"type": "build", **result}))
            return result

    async def apply(self, source):
        result = await self.build(source)
        if not result["ok"]:
            return result
        remote = f"{self.args.user}@{self.args.kria}"
        remote_path = f"{self.args.remote_dir.rstrip('/')}/rom.next.mem"
        code, _ = await self.run(["scp", *SSH_OPTIONS, FIRMWARE,
                                  f"{remote}:{remote_path}"])
        if code:
            raise RuntimeError("firmware upload failed")
        reply = await self.control({"command": "reload", "path": remote_path})
        self.board.update(connected=True, firmware=remote_path,
                          counters=reply.get("counters", {}))
        await self.log(f"applied {reply['words']} firmware words")
        await self.broadcast_state()
        return {"ok": True, "diagnostics": [], "board": reply}

    async def broadcast_state(self):
        asyncio.create_task(self.broadcast_json(
            {"type": "state", "board": self.board, "stream": self.stats}))

    async def poll_state(self):
        tick = 0
        while True:
            await asyncio.sleep(1)
            tick += 1
            if self.board["connected"] and tick % 2 == 0:
                try:
                    reply = await self.control({"command": "status"})
                    self.board["counters"] = reply.get("counters", {})
                except Exception:
                    self.board["connected"] = False
            await self.broadcast_state()

    async def close(self):
        if self.ssh and self.ssh.returncode is None:
            try:
                await self.control({"command": "shutdown"})
            except Exception:
                pass
            self.ssh.terminate()
            try:
                await asyncio.wait_for(self.ssh.wait(), 3)
            except asyncio.TimeoutError:
                self.ssh.kill()
                await self.ssh.wait()


def parse_diagnostics(output):
    diagnostics = []
    for line in output.splitlines():
        parts = line.split(":", 4)
        if len(parts) >= 4 and parts[1].isdigit() and parts[2].isdigit():
            severity = "error" if "error:" in line else "warning"
            diagnostics.append({"line": int(parts[1]), "column": int(parts[2]),
                                "severity": severity, "message": parts[-1].strip()})
    return diagnostics


class UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, dashboard):
        self.dashboard = dashboard

    def datagram_received(self, data, _addr):
        if len(data) < HDR.size:
            return
        magic, seq, count = HDR.unpack_from(data)
        if magic != MAGIC:
            return
        count = min(count, (len(data) - HDR.size) // 4)
        expected = self.dashboard.stats["sequence"]
        if expected is not None and seq != expected:
            self.dashboard.stats["dropped"] += (seq - expected) & 0xFFFFFFFF
        self.dashboard.stats["sequence"] = (seq + 1) & 0xFFFFFFFF
        self.dashboard.stats["words"] += count
        self.dashboard.stats["packets"] += 1
        self.dashboard.enqueue_binary(data[HDR.size:HDR.size + count * 4])


async def make_app(args):
    dashboard = Dashboard(args)
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app["dashboard"] = dashboard

    async def index(_request):
        return web.FileResponse(Path(args.static) / "index.html")

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        dashboard.websockets.add(ws)
        await ws.send_json({"type": "init", "board": dashboard.board,
                            "stream": dashboard.stats, "logs": dashboard.log_lines})
        async for message in ws:
            if message.type == WSMsgType.ERROR:
                break
        dashboard.websockets.discard(ws)
        return ws

    async def source_handler(request):
        if request.method == "GET":
            return web.json_response({"source": SOURCE.read_text()})
        payload = await request.json()
        SOURCE.write_text(payload["source"])
        return web.json_response({"ok": True})

    async def blocks_handler(request):
        if request.method == "GET":
            value = json.loads(BLOCKS.read_text()) if BLOCKS.exists() else None
            return web.json_response({"blocks": value})
        payload = await request.json()
        BLOCKS.write_text(json.dumps(payload["blocks"], indent=2) + "\n")
        return web.json_response({"ok": True})

    async def action(request):
        name = request.match_info["name"]
        payload = await request.json() if request.can_read_body else {}
        try:
            if name == "build":
                result = await dashboard.build(payload.get("source"))
            elif name == "apply":
                result = await dashboard.apply(payload["source"])
            elif name == "reset":
                result = await dashboard.control({"command": "reset"})
            elif name == "reconnect":
                await dashboard.deploy()
                result = {"ok": True}
            else:
                raise web.HTTPNotFound()
            return web.json_response(result, status=200 if result.get("ok") else 400)
        except Exception as exc:
            await dashboard.log(str(exc), "error")
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_route("*", "/api/source", source_handler)
    app.router.add_route("*", "/api/blocks", blocks_handler)
    app.router.add_post("/api/{name}", action)
    app.router.add_static("/assets", Path(args.static) / "assets")

    async def start(_app):
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: UdpProtocol(dashboard),
                                            local_addr=("0.0.0.0", args.udp_port))
        asyncio.create_task(dashboard.event_sender())
        asyncio.create_task(dashboard.poll_state())
        if not args.no_deploy:
            asyncio.create_task(dashboard.deploy_reported())
        if not args.no_browser:
            loop.call_later(0.8, webbrowser.open, f"http://127.0.0.1:{args.http_port}")

    async def stop(_app):
        await dashboard.close()

    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kria", default="kria.local")
    parser.add_argument("--user", default="ubuntu")
    parser.add_argument("--listen-host", required=True)
    parser.add_argument("--udp-port", type=int, default=3334)
    parser.add_argument("--raw-udp-port", type=int, default=3336)
    parser.add_argument("--control-port", type=int, default=3335)
    parser.add_argument("--remote-control-port", type=int, default=3335)
    parser.add_argument("--http-port", type=int, default=8088)
    parser.add_argument("--remote-dir", default="/tmp/actnow_harness")
    parser.add_argument("--xsa", required=True, type=Path)
    parser.add_argument("--static", required=True, type=Path)
    parser.add_argument("--no-deploy", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    web.run_app(make_app(args), host="127.0.0.1", port=args.http_port)


if __name__ == "__main__":
    main()
