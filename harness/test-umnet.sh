#!/bin/sh

set -eu

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
temporary_directory=$(mktemp -d "${TMPDIR:-/tmp}/umnet-test.XXXXXX")
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM

${CC:-cc} ${CFLAGS:-} -std=c11 -Wall -Wextra -Werror -O2 \
    "$script_directory/umnet.c" -o "$temporary_directory/umnet"

python3 - "$temporary_directory/umnet" <<'PY'
import socket
import struct
import subprocess
import sys

binary = sys.argv[1]
maximum_frame = 65535


def read_exact(sock, length):
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise AssertionError("unexpected stream EOF")
        data.extend(chunk)
    return bytes(data)


def start_bridge():
    uml_peer, uml_bridge = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    passt_peer, passt_bridge = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    process = subprocess.Popen(
        [binary, f"--uml-fd={uml_bridge.fileno()}",
         f"--passt-fd={passt_bridge.fileno()}"],
        pass_fds=(uml_bridge.fileno(), passt_bridge.fileno()),
        stderr=subprocess.PIPE,
    )
    uml_bridge.close()
    passt_bridge.close()
    uml_peer.settimeout(2)
    passt_peer.settimeout(2)
    return process, uml_peer, passt_peer


def close_cleanly(process, uml_peer, passt_peer, expected=0):
    uml_peer.close()
    passt_peer.close()
    result = process.wait(timeout=2)
    if result != expected:
        error = process.stderr.read().decode() if process.stderr else ""
        raise AssertionError(
            f"bridge exited {result}, expected {expected}: {error}")


def test_uml_to_passt(frame):
    process, uml, passt = start_bridge()
    uml.send(frame)
    wire = read_exact(passt, 4 + len(frame))
    if wire != struct.pack("!I", len(frame)) + frame:
        raise AssertionError("UML-to-passt frame mismatch")
    close_cleanly(process, uml, passt)


def test_passt_to_uml(frame):
    process, uml, passt = start_bridge()
    wire = struct.pack("!I", len(frame)) + frame
    passt.send(wire[:2])
    passt.send(wire[2:5])
    passt.sendall(wire[5:])
    received = uml.recv(maximum_frame + 1)
    if received != frame:
        raise AssertionError("passt-to-UML frame mismatch")
    close_cleanly(process, uml, passt)


def test_multiple_frames():
    process, uml, passt = start_bridge()
    frames = [b"first-frame", bytes(range(256)) * 8]
    passt.sendall(b"".join(struct.pack("!I", len(frame)) + frame
                             for frame in frames))
    for frame in frames:
        if uml.recv(maximum_frame + 1) != frame:
            raise AssertionError("multiple-frame ordering mismatch")
    close_cleanly(process, uml, passt)


def test_maximum_frame():
    frame = bytes((index % 251 for index in range(maximum_frame)))
    test_uml_to_passt(frame)
    test_passt_to_uml(frame)


def test_invalid_length():
    process, uml, passt = start_bridge()
    passt.send(struct.pack("!I", maximum_frame + 1))
    uml.close()
    passt.close()
    result = process.wait(timeout=2)
    if result == 0:
        raise AssertionError("invalid frame length was accepted")


def test_invalid_length_after_frame():
    process, uml, passt = start_bridge()
    valid = b"valid-frame"
    passt.sendall(struct.pack("!I", len(valid)) + valid +
                  struct.pack("!I", maximum_frame + 1))
    if uml.recv(maximum_frame + 1) != valid:
        raise AssertionError("valid frame before invalid length was lost")
    uml.close()
    passt.close()
    result = process.wait(timeout=2)
    if result == 0:
        raise AssertionError("invalid trailing frame length was accepted")


def test_truncated_frame():
    process, uml, passt = start_bridge()
    passt.sendall(struct.pack("!I", 100) + b"short")
    passt.shutdown(socket.SHUT_WR)
    uml.close()
    passt.close()
    result = process.wait(timeout=2)
    if result == 0:
        raise AssertionError("truncated frame was accepted")


test_uml_to_passt(b"\x00\x01\x02ethernet-frame")
test_passt_to_uml(b"\xff\xee\xddethernet-frame")
test_multiple_frames()
test_maximum_frame()
test_invalid_length()
test_invalid_length_after_frame()
test_truncated_frame()
print("umnet framing tests passed")
PY
