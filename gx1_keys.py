#!/usr/bin/env python3
"""
Realforce GX1 per-key analog monitor.

Request  (host -> kb): aa aa c3 00 01 <key_index> 00 ...  (64 bytes)
Reply    (kb -> host): 55 55 c3 00 00 18 <flags> <live> <base> <range> <table...>

Setup:    pip install hidapi      (close the Realforce software first)

Learn a key (press it when asked, it saves the index to gx1_keymap.json):
    python gx1_keys.py --learn a
    python gx1_keys.py --learn s

Monitor keys (names from the map, or raw indices like 0x1f):
    python gx1_keys.py --key s
    python gx1_keys.py --key a,s,d,f
    python gx1_keys.py --key 0x1f --apc 2.0

Only sends the same read request the official software sends.
"""
import argparse
import json
import os
import sys
import time

import hid

TOPRE_VID = 0x0853
MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gx1_keymap.json")
DEFAULT_MAP = {"s": 0x1F}  # seen in your capture; learn the others


def load_map():
    m = dict(DEFAULT_MAP)
    try:
        with open(MAP_FILE) as f:
            m.update(json.load(f))
    except (OSError, ValueError):
        pass
    return m


def save_map(m):
    with open(MAP_FILE, "w") as f:
        json.dump(m, f, indent=2)


def parse(data):
    buf = bytes(data)
    i = buf.find(b"\x55\x55\xc3")
    if i < 0 or len(buf) - i < 14:
        return None
    p = buf[i:]
    return {"live": int.from_bytes(p[8:10], "big"),
            "base": int.from_bytes(p[10:12], "big"),
            "range": int.from_bytes(p[12:14], "big")}


def query(h, idx, timeout_ms=40):
    """Ask the keyboard for one key. Returns parsed dict or None."""
    while h.read(64, 1):  # drop stale reports
        pass
    pkt = bytearray(64)
    pkt[:6] = bytes([0xAA, 0xAA, 0xC3, 0x00, 0x01, idx])
    try:
        h.write(b"\x00" + bytes(pkt))  # leading 0 = "no report ID" for hidapi
    except OSError:
        return None
    end = time.time() + timeout_ms / 1000
    while time.time() < end:
        d = h.read(64, 10)
        if d:
            r = parse(d)
            if r:
                return r
    return None


def query_many(h, idxs, timeout_ms=30, pipeline=False):
    """Pipelined: send every request, then read the replies in order.
    Replies carry no key index, so order is the only link. On any timeout we
    drain and return None for the rest so a lost reply cannot shift keys."""
    if not pipeline:
        return [query(h, i, timeout_ms) for i in idxs]
    while h.read(64, 1):  # drop stale reports
        pass
    for idx in idxs:
        pkt = bytearray(64)
        pkt[:6] = bytes([0xAA, 0xAA, 0xC3, 0x00, 0x01, idx])
        try:
            h.write(b"\x00" + bytes(pkt))
        except OSError:
            return [None] * len(idxs)
    out = []
    for _ in idxs:
        r = None
        d = h.read(64, timeout_ms)
        if d:
            r = parse(d)
        if r is None:
            out.extend([None] * (len(idxs) - len(out)))
            while h.read(64, 1):
                pass
            break
        out.append(r)
    return out


def open_keyboard(vid, pid):
    for info in hid.enumerate(vid, pid or 0):
        if info["usage_page"] < 0xFF00:
            continue  # skip normal typing interfaces
        h = hid.device()
        try:
            h.open_path(info["path"])
        except OSError:
            continue
        if query(h, 0x1F, 150):
            print(f"using iface {info['interface_number']} "
                  f"PID {info['product_id']:04x}")
            return h
        h.close()
    return None


def travel_mm(r, total):
    if not r["range"]:
        return 0.0
    return max(0.0, min(1.0, (r["live"] - r["base"]) / r["range"])) * total


def learn(h, name, kmap):
    print("Hands off the keyboard, measuring baseline...")
    base = {}
    for i in range(256):
        r = query(h, i, 30)
        if r:
            base[i] = r["live"]
    print(f"{len(base)} indices respond.")
    print(f"Now press and HOLD the '{name}' key (Ctrl+C to cancel).")
    try:
        while True:
            best, best_d = None, 0
            for i, b0 in base.items():
                r = query(h, i, 30)
                if not r or not r["range"]:
                    continue
                d = r["live"] - b0
                if d > best_d and d > 0.3 * r["range"]:
                    best, best_d = i, d
            if best is not None:
                # confirm it is still held
                time.sleep(0.05)
                r = query(h, best, 30)
                if r and r["live"] - base[best] > 0.3 * r["range"]:
                    kmap[name] = best
                    save_map({k: v for k, v in kmap.items()})
                    print(f"'{name}' = index {best} (0x{best:02x}), saved to {MAP_FILE}")
                    return
    except KeyboardInterrupt:
        print("\ncancelled")


DEFAULT_NAMES = (
    "esc f1 f2 f3 f4 f5 f6 f7 f8 f9 f10 f11 f12 "
    "` 1 2 3 4 5 6 7 8 9 0 - = backspace "
    "tab q w e r t y u i o p [ ] \\ "
    "caps a s d f g h j k l ; ' enter "
    "lshift z x c v b n m , . / rshift "
    "lctrl lwin lalt space ralt rwin menu rctrl "
    "up down left right"
).split()


def learn_all(h, names, kmap, redo):
    print("Hands off the keyboard, measuring baseline...")
    base = {}
    for i in range(256):
        r = query(h, i, 30)
        if r:
            base[i] = r["live"]
    print(f"{len(base)} indices respond.\n")
    todo = [n for n in names if redo or n not in kmap]
    print(f"{len(todo)} keys to learn. Press each key when asked, then release it.")
    print("Ctrl+C stops (progress is saved after every key).\n")
    try:
        for n, name in enumerate(todo, 1):
            print(f"[{n}/{len(todo)}] press '{name}' ...", end=" ", flush=True)
            idx = None
            while idx is None:
                best, best_d = None, 0
                for i, b0 in base.items():
                    r = query(h, i, 30)
                    if not r or not r["range"]:
                        continue
                    d = r["live"] - b0
                    if d > best_d and d > 0.3 * r["range"]:
                        best, best_d = i, d
                idx = best
            owner = [k for k, v in kmap.items() if v == idx and k != name]
            kmap[name] = idx
            save_map(kmap)
            print(f"index {idx} (0x{idx:02x})" +
                  (f"  WARNING: already assigned to {owner}" if owner else ""))
            # wait for release before the next key
            while True:
                r = query(h, idx, 30)
                if not r or r["live"] - base[idx] < 0.1 * (r["range"] or 1):
                    break
    except KeyboardInterrupt:
        print("\nstopped, progress saved")
        return
    print(f"\nDone. Map saved to {MAP_FILE}")


def resolve(tokens, kmap):
    keys = []
    for t in tokens.split(","):
        t = t.strip()
        if not t:
            continue
        if t.lower() in kmap:
            keys.append((t.lower(), kmap[t.lower()]))
        else:
            try:
                keys.append((t, int(t, 0)))
            except ValueError:
                sys.exit(f"unknown key '{t}'. Learn it with --learn {t}")
    return keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default="s", help="comma list of names or indices")
    ap.add_argument("--learn", metavar="NAME", help="find the index of a key by pressing it")
    ap.add_argument("--learn-all", action="store_true",
                    help="walk through every key and record its index")
    ap.add_argument("--names", help="comma list of key names for --learn-all "
                                    "(default: full ANSI layout)")
    ap.add_argument("--redo", action="store_true", help="relearn keys already in the map")
    ap.add_argument("--show-map", action="store_true")
    ap.add_argument("--pipeline", action="store_true",
                    help="send all requests first (faster, experimental)")
    ap.add_argument("--vid", type=lambda x: int(x, 16), default=TOPRE_VID)
    ap.add_argument("--pid", type=lambda x: int(x, 16))
    ap.add_argument("--travel", type=float, default=4.0, help="total travel in mm")
    ap.add_argument("--apc", type=float, help="show PRESSED past this many mm")
    a = ap.parse_args()

    if a.show_map:
        for k, v in sorted(load_map().items(), key=lambda kv: kv[1]):
            print(f"{v:3d}  0x{v:02x}  {k}")
        return

    h = open_keyboard(a.vid, a.pid)
    if not h:
        sys.exit("No Topre vendor interface answered. Is the Realforce software closed?")

    kmap = load_map()
    if a.learn_all:
        names = [n.strip() for n in a.names.split(",")] if a.names else DEFAULT_NAMES
        learn_all(h, names, kmap, a.redo)
        return
    if a.learn:
        learn(h, a.learn.lower(), kmap)
        return

    keys = resolve(a.key, kmap)
    peaks = {n: 0.0 for n, _ in keys}
    print("Press the keys. Ctrl+C to quit.\n")
    t0, loops, hz = time.time(), 0, 0.0
    last_draw = 0.0
    try:
        while True:
            loops += 1
            now = time.time()
            if now - t0 >= 1.0:
                hz, t0, loops = loops / (now - t0), now, 0
            results = query_many(h, [i for _, i in keys], pipeline=a.pipeline)
            if now - last_draw < 0.033:
                for (name, _), r in zip(keys, results):
                    if r:
                        peaks[name] = max(peaks[name], travel_mm(r, a.travel))
                continue
            last_draw = now
            parts = [f"{hz:4.0f}Hz"]
            for (name, idx), r in zip(keys, results):
                if not r:
                    parts.append(f"{name}: --")
                    continue
                mm = travel_mm(r, a.travel)
                peaks[name] = max(peaks[name], mm)
                bar = "#" * int(mm / a.travel * 20)
                flag = ""
                if a.apc is not None:
                    flag = " ON " if mm >= a.apc else " off"
                parts.append(f"{name}: {mm:4.2f}mm pk{peaks[name]:4.2f}{flag}[{bar:<20}]")
            sys.stdout.write("\r" + "  ".join(parts) + "   ")
            sys.stdout.flush()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
