import importlib.util
import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]


def load_server():
    pynq = types.ModuleType("pynq")
    pynq.Overlay = object
    pynq.allocate = mock.Mock()
    sys.modules["pynq"] = pynq
    path = ROOT / "harness/pynq/actnow_fpga_server.py"
    spec = importlib.util.spec_from_file_location("actnow_fpga_server", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_dashboard():
    path = ROOT / "harness/dashboard/backend/dashboard.py"
    spec = importlib.util.spec_from_file_location("actnow_dashboard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Bram:
    def __init__(self):
        self.words = {}

    def write(self, address, value):
        self.words[address] = value

    def read(self, address):
        return self.words.get(address, 0)


class Channel:
    def __init__(self):
        self.value = 0

    def write(self, value, _mask):
        self.value = value

    def read(self):
        return self.value


class Overlay:
    def __init__(self):
        self.bram_ctrl = Bram()
        self.gpio_ctrl = types.SimpleNamespace(channel1=Channel())
        for index in range(6):
            setattr(self, f"gpio_s{index}",
                    types.SimpleNamespace(channel1=Channel(), channel2=Channel()))
        recv = types.SimpleNamespace(stop=mock.Mock(), start=mock.Mock(), running=True)
        recv.stop.side_effect = lambda: setattr(recv, "running", False)
        recv.start.side_effect = lambda: setattr(recv, "running", True)
        self.dma_res = types.SimpleNamespace(recvchannel=recv)
        raw_recv = types.SimpleNamespace(stop=mock.Mock(), start=mock.Mock(), running=True)
        raw_recv.stop.side_effect = lambda: setattr(raw_recv, "running", False)
        raw_recv.start.side_effect = lambda: setattr(raw_recv, "running", True)
        self.dma_raw = types.SimpleNamespace(recvchannel=raw_recv)


class DashboardTests(unittest.TestCase):
    def test_compiler_diagnostics(self):
        dashboard = load_dashboard()
        items = dashboard.parse_diagnostics("main.c:17:9: error: expected expression")
        self.assertEqual(items, [{"line": 17, "column": 9, "severity": "error",
                                  "message": "expected expression"}])

    def test_firmware_write_and_readback(self):
        server = load_server()
        overlay = Overlay()
        with tempfile.NamedTemporaryFile("w", delete=False) as stream:
            stream.write("00000000000000000000000000000001\n")
            stream.write("00000000000000000000000000000010\n")
            name = stream.name
        self.assertEqual(server.load_firmware(overlay, name), 2)
        self.assertEqual(overlay.bram_ctrl.words, {0: 1, 4: 2})

    def test_runtime_reload_keeps_dma_running_and_resets(self):
        server = load_server()
        overlay = Overlay()
        runtime = server.Runtime(overlay, "old.mem")
        with tempfile.NamedTemporaryFile("w", delete=False) as stream:
            stream.write("00000000000000000000000000000111\n")
            name = stream.name
        reset_reads = iter([0, 1])
        overlay.gpio_s4.channel2.read = mock.Mock(
            side_effect=lambda: next(reset_reads, 1))
        with mock.patch.object(server.time, "sleep"):
            reply = runtime.command({"command": "reload", "path": name})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["words"], 1)
        overlay.dma_res.recvchannel.stop.assert_not_called()
        overlay.dma_res.recvchannel.start.assert_not_called()
        overlay.dma_raw.recvchannel.stop.assert_not_called()
        overlay.dma_raw.recvchannel.start.assert_not_called()
        self.assertEqual(overlay.gpio_ctrl.channel1.value, 0)

    def test_unknown_control_command_is_rejected(self):
        server = load_server()
        with self.assertRaisesRegex(ValueError, "unknown command"):
            server.Runtime(Overlay(), "old.mem").command({"command": "erase"})

    def test_shutdown_stops_both_dma_channels(self):
        server = load_server()
        overlay = Overlay()
        reply = server.Runtime(overlay, "old.mem").command({"command": "shutdown"})
        self.assertTrue(reply["ok"])
        overlay.dma_res.recvchannel.stop.assert_called_once()
        overlay.dma_raw.recvchannel.stop.assert_called_once()

    def test_raw_dma_uses_independent_udp_destination(self):
        server = load_server()
        overlay = Overlay()
        runtime = types.SimpleNamespace(overlay=overlay, running=True)
        channel = overlay.dma_raw.recvchannel
        channel.transferred = 8
        channel.transfer = mock.Mock()
        channel.wait = mock.Mock(side_effect=lambda: setattr(runtime, "running", False))

        class Buffer(list):
            def freebuffer(self):
                self.freed = True

        buf = Buffer([0x12345678, 0xABCDEF01] + [0] * 254)
        sock = mock.Mock()
        with mock.patch.object(server, "allocate", return_value=buf), \
             mock.patch.object(server.socket, "socket", return_value=sock):
            server.dma_to_udp(runtime, "dma_raw", "raw", "192.0.2.1", 3336)

        channel.transfer.assert_called_once_with(buf)
        packet, destination = sock.sendto.call_args.args
        self.assertEqual(destination, ("192.0.2.1", 3336))
        self.assertEqual(server.HDR.unpack_from(packet), (server.MAGIC, 0, 2))
        self.assertEqual(packet[server.HDR.size:],
                         server.struct.pack("<2I", 0x12345678, 0xABCDEF01))
        self.assertTrue(buf.freed)
        sock.close.assert_called_once()


class UdpTests(unittest.IsolatedAsyncioTestCase):
    async def test_packet_is_counted_and_forwarded_without_header(self):
        dashboard_module = load_dashboard()

        class FakeDashboard:
            def __init__(self):
                self.stats = {"words": 0, "packets": 0, "dropped": 0,
                              "sequence": None}
                self.payload = None

            def enqueue_binary(self, payload):
                self.payload = payload

        target = FakeDashboard()
        protocol = dashboard_module.UdpProtocol(target)
        body = bytes.fromhex("0100000002000000")
        protocol.datagram_received(dashboard_module.HDR.pack(
            dashboard_module.MAGIC, 7, 2) + body, None)
        self.assertEqual(target.stats["words"], 2)
        self.assertEqual(target.stats["packets"], 1)
        self.assertEqual(target.stats["sequence"], 8)
        self.assertEqual(target.payload, body)

    async def test_viewer_queue_is_bounded_under_packet_flood(self):
        dashboard_module = load_dashboard()
        args = types.SimpleNamespace()
        target = dashboard_module.Dashboard(args)
        for index in range(dashboard_module.EVENT_QUEUE_PACKETS * 4):
            target.enqueue_binary(index.to_bytes(4, "little"))
        self.assertEqual(target.event_queue.qsize(),
                         dashboard_module.EVENT_QUEUE_PACKETS)
        oldest = int.from_bytes(target.event_queue.get_nowait(), "little")
        self.assertEqual(oldest, dashboard_module.EVENT_QUEUE_PACKETS * 3)

    async def test_raw_tap_is_dropped_until_a_client_subscribes(self):
        dashboard_module = load_dashboard()
        target = dashboard_module.Dashboard(types.SimpleNamespace())
        protocol = dashboard_module.RawUdpProtocol(target)
        body = bytes.fromhex("0100000002000000")
        packet = dashboard_module.HDR.pack(dashboard_module.MAGIC, 0, 2) + body

        protocol.datagram_received(packet, None)
        self.assertEqual(target.raw_queue.qsize(), 0)  # nobody watching

        target.raw_websockets.add(object())
        protocol.datagram_received(packet, None)
        self.assertEqual(target.raw_queue.get_nowait(), body)  # header stripped

    async def test_stream_sender_prefixes_the_stream_tag(self):
        dashboard_module = load_dashboard()
        target = dashboard_module.Dashboard(types.SimpleNamespace())
        target.raw_queue.put_nowait(b"\x11\x11\x11\x11")
        target.raw_queue.put_nowait(b"\x22\x22\x22\x22")
        sent = []

        async def capture(payload, raw):
            sent.append((payload, raw))
        target.broadcast_binary = capture

        task = asyncio.create_task(target.stream_sender(
            target.raw_queue, target.enqueue_raw, dashboard_module.STREAM_RAW, True))
        await asyncio.sleep(0.05)
        task.cancel()

        payload, raw = sent[0]
        self.assertTrue(raw)
        self.assertEqual(payload, dashboard_module.struct.pack(
            "<3I", dashboard_module.STREAM_RAW, 0x11111111, 0x22222222))


if __name__ == "__main__":
    unittest.main()
