"""tools/mcp-session.py —— 与 CasioEmuMsvc 的 MCP 插件（127.0.0.1:3001）交互的小工具库。

用法（在别的脚本里）：
    import sys; sys.path.insert(0, "tools")
    from importlib import import_module
    sess = import_module("mcp-session")          # 文件名带横线，用 import_module
    sess.reset_and_resume()
    sess.wmem(0xEC00, chain)

按键/复位顺序（**实测**）：鼠标点计算器窗口聚焦 → **F4（开机/复位）** → `Right`（=【→】）→ `Return`（=【=】）

按键约定（**实测**，主机键盘走 computer-use）：
    【→】 = 主机 Right     【=】 = 主机 Return
注意：MCP 的 keyboard_code 键码表不完整（0x30 打出来是 '+'），不要用它按 '='。
"""
from __future__ import annotations
import json, time, urllib.request

URL = "http://127.0.0.1:3001/mcp"
LAUNCHER_EC00 = bytes([0xFD, 0x24, 0xF0, 0xEB, 0x8F, 0x23, 0x42])   # 链基址 0xEC00
LAUNCHER_D400 = bytes([0xFD, 0x24, 0xF0, 0xD3, 0x8F, 0x23, 0x42])   # 链基址 0xD400


# ★必须绕开系统代理：本机环境里设了 http_proxy=http://192.168.3.59:8080，
#   它会把 127.0.0.1:3001 的请求也代理走，然后回 502（实测踩过这个坑）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def rpc(tool: str, args: dict):
    req = urllib.request.Request(
        URL,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": tool, "arguments": args}}).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"})
    with _OPENER.open(req, timeout=30) as fh:
        body = fh.read().decode()
    if body.lstrip().startswith(("event:", "data:")):
        for line in body.splitlines():
            if line.startswith("data:"):
                body = line[5:].strip()
    d = json.loads(body)
    res = d.get("result", d)
    if isinstance(res, dict) and "content" in res:
        return "\n".join(c.get("text", "") for c in res["content"])
    return json.dumps(res, ensure_ascii=False)[:2000]


def wmem(addr: int, data: bytes, chunk: int = 128):
    """分块写：长链一次写会被 MCP 插件截断（实测 644 字节链在 0xC6 处丢数据）。"""
    out = None
    for k in range(0, len(data), chunk):
        out = rpc("write_memory", {"address": addr + k, "bytes": list(data[k:k + chunk])})
    return out


def rmem(addr: int, n: int, chunk: int = 192) -> bytes:
    """分块读（长段一次读会被截断）。"""
    out = bytearray()
    while len(out) < n:
        k = min(chunk, n - len(out))
        t = rpc("read_memory", {"address": addr + len(out), "size": k})
        got = b""
        try:
            o = json.loads(t)
            if isinstance(o, dict) and isinstance(o.get("bytes"), list):
                got = bytes(int(x) & 0xFF for x in o["bytes"])
        except ValueError:
            got = b""
        if not got:
            break
        out += got[:k]
    return bytes(out)


def status():
    s = json.loads(rpc("get_status", {}))
    sp = json.loads(rpc("read_register", {"name": "SP"}))["value"]
    return s["program_counter"], sp, s.get("paused")


def reset_and_resume(wait: float = 2.2):
    rpc("reset", {})
    time.sleep(wait)
    rpc("resume", {})          # ★用完 MCP 一定让机器继续跑，不留在暂停态


def inject(chain: bytes, chain_base: int = 0xEC00, data_base: int = 0xEA40,
           data_len: int = 0x20, launcher: bytes | None = None):
    """清输入区 → 写 launcher/账本 → 写链 → 清数据区。返回一条人类可读的状态行。"""
    reset_and_resume()
    wmem(0xD180, b"\0" * 0x40)
    wmem(0xD248, launcher or (LAUNCHER_EC00 if chain_base == 0xEC00 else LAUNCHER_D400))
    wmem(0xD244, bytes([7] * 4))
    wmem(chain_base, chain)
    wmem(data_base, b"\0" * data_len)
    time.sleep(0.3)
    return ("链 @%04X: %d 字节 回读一致=%s  launcher=%s  账本=%s  PC=%05X"
            % (chain_base, len(chain), rmem(chain_base, len(chain)) == chain,
               rmem(0xD248, 7).hex(" ").upper(), rmem(0xD244, 4).hex(" ").upper(),
               status()[0]))


def to_png(b: bytes, path: str, stride: int = 24, w: int = 192, h: int = 64, scale: int = 4):
    """把屏幕缓冲区（0xDDD4）渲染成 PNG，便于**用多模态直接看**。"""
    import struct, zlib
    raw = b""
    for y in range(h):
        row = bytearray()
        for x in range(w):
            on = (b[y * stride + (x >> 3)] >> (x & 7)) & 1
            row += bytes([0 if on else 255]) * scale
        for _ in range(scale):
            raw += b"\x00" + bytes(row)

    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n"
                 + chunk(b"IHDR", struct.pack(">IIBBBBB", w * scale, h * scale, 8, 0, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path
