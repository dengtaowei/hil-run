"""couart hub control socket client and UART window helpers."""

import os
import re
import socket
import time

from .errors import PlanError

POLL_S = 0.05
WINDOW_BYTES = 16384


def control_path(hub):
    root = os.environ.get("XDG_RUNTIME_DIR")
    if root:
        return os.path.join(root, "couart", hub, "control.sock")
    return "/tmp/couart-%d/%s/control.sock" % (os.getuid(), hub)


def ctl(hub, cmd, timeout_s):
    path = control_path(hub)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        sock.connect(path)
        sock.sendall((cmd + "\n").encode())
        chunks = []
        while True:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
    except OSError as exc:
        raise PlanError("%s: %s" % (hub, exc)) from exc
    finally:
        sock.close()
    return b"".join(chunks).decode("utf-8", "replace")


def tx_lock(hub, timeout_s):
    deadline = time.monotonic() + timeout_s
    while True:
        reply = ctl(hub, "TXLOCK", min(2.0, timeout_s))
        if reply.startswith("OK"):
            match = re.search(r"id=(\d+)", reply)
            if not match:
                raise PlanError("%s: TXLOCK reply has no id" % hub)
            return int(match.group(1))
        if time.monotonic() >= deadline:
            raise PlanError("%s: TX lock busy" % hub)
        time.sleep(0.03)


def tx_unlock(hub, lock_id):
    try:
        ctl(hub, "TXUNLOCK %d" % lock_id, 2.0)
    except PlanError:
        pass


def write_line(hub, lock_id, command):
    payload = command.rstrip("\r\n").encode() + b"\r"
    reply = ctl(hub, "WRITE id=%d %s" % (lock_id, payload.hex()), 2.0)
    if not reply.startswith("OK"):
        raise PlanError("%s: write failed: %s" % (hub, reply.strip()))


def history(hub, want):
    reply = ctl(hub, "HISTORY %d" % want, 2.0)
    if not reply.startswith("OK"):
        raise PlanError("%s: HISTORY failed: %s" % (hub, reply.split("\n", 1)[0]))
    seq_match = re.search(r"seq=(\d+)", reply)
    seq = int(seq_match.group(1)) if seq_match else 0
    hex_part = reply.split("\n", 1)[1].strip() if "\n" in reply else ""
    if len(hex_part) % 2:
        raise PlanError("%s: HISTORY hex truncated" % hub)
    raw = bytes.fromhex(hex_part).decode("utf-8", "replace") if hex_part else ""
    return seq, raw


def parse_historyt(reply):
    """Return seq, text, per-character host milliseconds.

    HISTORYT bytes are one CLOCK_MONOTONIC stamp per read or write chunk.
    Milliseconds are that stamp divided by 1e6.
    """
    lines = reply.splitlines()
    if not lines or not lines[0].startswith("OK "):
        raise PlanError("HISTORYT failed: %s" % (lines[0] if lines else reply[:80]))
    header = re.match(r"OK bytes=(\d+) seq=(\d+) spans=(\d+)$", lines[0])
    if not header:
        raise PlanError("HISTORYT header: %s" % lines[0])
    nbytes, seq, spans = (int(header.group(i)) for i in (1, 2, 3))
    hex_part = lines[1].strip() if len(lines) > 1 else ""
    if len(hex_part) % 2:
        raise PlanError("HISTORYT hex truncated")
    raw = bytes.fromhex(hex_part) if hex_part else b""
    if len(raw) != nbytes:
        raise PlanError("HISTORYT bytes %d hex %d" % (nbytes, len(raw)))
    stamps = []
    for line in lines[2:2 + spans]:
        parts = line.split()
        if len(parts) != 2:
            raise PlanError("HISTORYT span: %s" % line)
        count, ns = int(parts[0]), int(parts[1])
        stamps.extend([ns // 1000000] * count)
    if len(stamps) != nbytes:
        raise PlanError("HISTORYT spans cover %d bytes, want %d" % (len(stamps), nbytes))
    text = raw.decode("utf-8", "replace")
    if len(text) != len(stamps):
        raise PlanError("HISTORYT text and timestamps differ in length")
    return seq, text, stamps


def history_timed(hub, want):
    """Return seq, text, and per-character milliseconds.

    times is None when the hub does not implement HISTORYT. Callers that need
    a timestamp must fail the step instead of inventing one.
    """
    reply = ctl(hub, "HISTORYT %d" % want, 2.0)
    first = reply.split("\n", 1)[0]
    if first.startswith("ERR unknown"):
        seq, text = history(hub, want)
        return seq, text, None
    return parse_historyt(reply)


def slice_new(seq0, seq1, text, times):
    new_n = max(0, seq1 - (seq0 or 0))
    if new_n > len(text):
        new_n = len(text)
    text = text[-new_n:] if new_n else ""
    if times is None:
        return text, None
    if len(times) < new_n:
        return text, None
    return text, (times[-new_n:] if new_n else [])


def normalize(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize_timed(text, times):
    """Map CR LF and lone CR to LF. A CR LF keeps the LF byte's stamp."""
    text = text or ""
    if times is None or len(times) != len(text):
        return normalize(text), None
    out = []
    stamped = []
    i = 0
    while i < len(text):
        if text[i] == "\r" and i + 1 < len(text) and text[i + 1] == "\n":
            out.append("\n")
            stamped.append(times[i + 1])
            i += 2
        elif text[i] == "\r":
            out.append("\n")
            stamped.append(times[i])
            i += 1
        else:
            out.append(text[i])
            stamped.append(times[i])
            i += 1
    return "".join(out), stamped


def new_text(hub, seq0):
    """UART text after seq0, normalized, with stamps. Returns text, times, seq."""
    seq1, raw, times = history_timed(hub, WINDOW_BYTES)
    text, tms = slice_new(seq0, seq1, raw, times)
    text, tms = normalize_timed(text, tms)
    return text, tms, seq1
