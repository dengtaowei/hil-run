"""In-process stand-in for a couart hub control socket.

Speaks the subset hil-run uses: TXLOCK, TXUNLOCK, WRITE, HISTORY, HISTORYT.
One command per connection, like couart. A board callable turns each written
line into UART output; FakeHub stamps every chunk with CLOCK_MONOTONIC.
"""

import os
import re
import socket
import threading
import time

PROMPT = "boot> "


class FakeHub(object):
    def __init__(self, runtime_dir, name, board, historyt=True, reply_delay_s=0.005):
        self.name = name
        self.board = board
        self.historyt = historyt
        self.reply_delay_s = reply_delay_s
        self.data = bytearray()
        self.stamps = []
        self.seq = 0
        self.lock = threading.Lock()
        self.lock_id = 0
        self.next_id = 1
        self.writes = []
        directory = os.path.join(runtime_dir, "couart", name)
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, "control.sock")
        if os.path.exists(self.path):
            os.unlink(self.path)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(16)
        self.stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        board.attach(self)

    def close(self):
        self.stop = True
        try:
            self.server.close()
        except OSError:
            pass
        if os.path.exists(self.path):
            os.unlink(self.path)

    def emit(self, text):
        raw = text.encode() if isinstance(text, str) else bytes(text)
        if not raw:
            return
        ns = time.monotonic_ns()
        with self.lock:
            self.data.extend(raw)
            self.stamps.extend([ns] * len(raw))
            self.seq += len(raw)

    def emit_later(self, delay_s, text):
        timer = threading.Timer(delay_s, self.emit, args=(text,))
        timer.daemon = True
        timer.start()

    def _serve(self):
        while not self.stop:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        try:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            line = buf.split(b"\n", 1)[0].decode()
            conn.sendall(self._handle(line).encode())
        finally:
            conn.close()

    def _window(self, want):
        with self.lock:
            n = min(want, len(self.data))
            return bytes(self.data[len(self.data) - n:]), list(self.stamps[len(self.stamps) - n:]), self.seq

    def _handle(self, line):
        if line == "TXLOCK":
            with self.lock:
                if self.lock_id:
                    return "ERR busy\n"
                self.lock_id = self.next_id
                self.next_id += 1
                return "OK id=%d\n" % self.lock_id
        match = re.fullmatch(r"TXUNLOCK (\d+)", line)
        if match:
            with self.lock:
                if int(match.group(1)) == self.lock_id:
                    self.lock_id = 0
            return "OK\n"
        match = re.fullmatch(r"WRITE id=(\d+) ([0-9a-f]+)", line)
        if match:
            if int(match.group(1)) != self.lock_id:
                return "ERR not owner\n"
            raw = bytes.fromhex(match.group(2))
            self.emit(raw)
            command = raw.decode().rstrip("\r\n")
            self.writes.append(command)
            threading.Thread(target=self._answer, args=(command,), daemon=True).start()
            return "OK\n"
        match = re.fullmatch(r"(HISTORYT?)(?: (\d+))?", line)
        if match:
            want = int(match.group(2) or 8192)
            raw, stamps, seq = self._window(want)
            if match.group(1) == "HISTORY":
                return "OK bytes=%d seq=%d\n%s\n" % (len(raw), seq, raw.hex())
            if not self.historyt:
                return "ERR unknown command\n"
            spans = []
            for ns in stamps:
                if spans and spans[-1][1] == ns:
                    spans[-1][0] += 1
                else:
                    spans.append([1, ns])
            body = "".join("%d %d\n" % (count, ns) for count, ns in spans)
            return "OK bytes=%d seq=%d spans=%d\n%s\n%s" % (len(raw), seq, len(spans), raw.hex(), body)
        return "ERR unknown command\n"

    def _answer(self, command):
        time.sleep(self.reply_delay_s)
        out = self.board.respond(command)
        if out is None:
            self.emit(command + "\r\n")
            return
        body = "".join(line + "\r\n" for line in out)
        self.emit(command + "\r\n" + body + PROMPT)


class RtcBoard(object):
    """32768 Hz counter with set/start/stop, a 1 s alarm, and a hang command."""

    def __init__(self, hz=32768):
        self.hz = hz
        self.base = 0
        self.t0 = time.monotonic()
        self.running = True
        self.hub = None

    def attach(self, hub):
        self.hub = hub

    def tick(self):
        if not self.running:
            return self.base
        return self.base + int((time.monotonic() - self.t0) * self.hz)

    def respond(self, command):
        if command == "get_tick":
            return ["rtc0 tick %d" % self.tick()]
        match = re.fullmatch(r"set_tick (\d+)", command)
        if match:
            self.base = int(match.group(1))
            self.t0 = time.monotonic()
            return ["rtc0 set_tick %d" % self.base]
        if command == "start_tick":
            if not self.running:
                self.t0 = time.monotonic()
                self.running = True
            return ["rtc0 start_tick"]
        if command == "stop_tick":
            self.base = self.tick()
            self.running = False
            return ["rtc0 stop_tick"]
        if command == "set_alarm 1":
            first = self.tick()
            self.hub.emit_later(
                1.0,
                "RTC interrupt triggered!\r\ntick1: %d, tick2: %d, Difference: %d\r\n"
                % (first, first + self.hz, self.hz),
            )
            return ["rtc0 alarm set for 1 seconds"]
        if command == "hang":
            return None
        if command == "version":
            return ["version 0x3230382a"]
        return ["unknown command"]
