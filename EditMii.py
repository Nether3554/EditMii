#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mii3ds.py - Nintendo 3DS system Mii database viewer and cleaner
================================================================

Works with CFL_DB.dat and CFL_OldDB.dat (shared extdata 0xF000000B in NAND),
i.e. files such as "00000005" / "00000006" extracted from the NAND.
Does not require any additional libraries (Python 3.8+).

USAGE:  python mii3ds.py            -> browser interface (tables + delete buttons)
        python mii3ds.py info|wipe|delete ...   -> command-line mode (python mii3ds.py -h)
        python mii3ds.py menu       -> simple text menu

FILE STRUCTURE (read from the provided files and confirmed with 3dbrew.org/wiki/Mii)
------------------------------------------------------------------------------------
Layer 1 - DIFF container (file = 0x4FD20 bytes)
  0x000  AES-CMAC (16 B)       depends on console keys - cannot be calculated on a PC
  0x100  DIFF header           identifies the active one of two tables (0x200 / 0x330)
  0x134  SHA-256 of active table
  table: DIFI + IVFC + DPFS   hash tree: data -> L3 -> L2 -> L1 -> "master hash"
  0x4000 ACTUAL DATA (0x4BD20 B)  <- Miis are stored here; hashes are calculated per 0x1000 B

Layer 2 - data (CFL_DB.dat), offsets relative to the beginning of the data
  0x00000  "CFOG" + 4 B          MII MAKER section
  0x00008  100 x 0x5C B          Miis saved in Mii Maker (full records including creator)
  0x023F8  "CFHE"                HISTORY of encountered Miis (StreetPass, multiplayer...)
  0x023FC  u16 tail, u16 head    0xFFFF when the list is empty
  0x02400  3000 x 14 B           doubly linked list nodes: ID(4) MAC(6) prev(2) next(2)
                                 empty node: ID=0 MAC=0 prev=next=0x7FFF; bit 15 = flag
  0x0C810  14 B                  padding
  0x0C81E  u16 (big-endian)      CRC-16/XMODEM of bytes [0x0, 0xC81E)   <-- also includes Mii Maker!
  0x0C820  "CFRA"                Mii Plaza INVITATIONS (Miis received from games)
  0x0C824  u32                   number of Miis in this section (max 100)
  0x0C828  100 B                 ordering (permutation of indices)
  0x0C88C  100 x 0x48 B          Mii records (like Mii Maker, but without the "creator" field)
  0x0E4AC  0x12 B                constant: 01 00 .. 00
  0x0E4BE  u16 (big-endian)      CRC-16/XMODEM of bytes [0xC820, 0xE4BE)
  0x0E4C0  3000 x 0x54 B         Mii data for CFHE nodes (record i <-> node i):
                                 0x48 B Mii + u32 encounter time (seconds since 2000-01-01) + 8 zero bytes

The program NEVER modifies the Mii Maker section (bytes 0x0..0x23F7) - this is
verified by comparing every byte before saving.
"""
import argparse
import datetime
import hashlib
import http.server
import io
import json
import os
import re
import secrets
import struct
import sys
import threading
import urllib.parse
import webbrowser
import zipfile

# ----------------------------------------------------------------------------
# CFL_DB.dat layout constants
# ----------------------------------------------------------------------------
DB_SIZE = 0x4BD20
MM_OFF, MM_N, MM_SZ = 0x08, 100, 0x5C            # Mii Maker
CFHE_OFF, TAIL_OFF, HEAD_OFF = 0x23F8, 0x23FC, 0x23FE
NODE_OFF, NODE_N, NODE_SZ = 0x2400, 3000, 14     # history index
CRC1_OFF = 0xC81E
CFRA_OFF, INV_COUNT_OFF, INV_ORDER_OFF = 0xC820, 0xC824, 0xC828
INV_OFF, INV_N, INV_SZ = 0xC88C, 100, 0x48       # invitations
CRC2_OFF = 0xE4BE
HIST_OFF, HIST_SZ = 0xE4C0, 0x54                 # history data (3000 entries)
NIL, EMPTY = 0x7FFF, 0xFFFF
MAKER_END = 0x23F8                               # end of Mii Maker section


class FormatError(Exception):
    pass


# ----------------------------------------------------------------------------
# CRC-16/XMODEM (the same algorithm used by Wii/3DS for Mii), stored big-endian
# ----------------------------------------------------------------------------
def _crc_table():
    t = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
        t.append(c)
    return t


_CRC = _crc_table()


def crc16(data):
    c = 0
    for b in data:
        c = ((c << 8) & 0xFFFF) ^ _CRC[(c >> 8) ^ b]
    return c


def sha(b):
    return hashlib.sha256(bytes(b)).digest()


# ----------------------------------------------------------------------------
# DIFF container: reading, verification and recalculation of the hash tree
# ----------------------------------------------------------------------------
class DiffFile:
    def __init__(self, raw):
        r = self.raw = bytearray(raw)
        if len(r) < 0x1000 or r[0x100:0x104] != b"DIFF":
            raise FormatError("this is not a DIFF container (missing 'DIFF' signature at 0x100)")
        sec, pri, tsz, poff, psz, active = struct.unpack_from("<QQQQQI", r, 0x108)
        self.tab = sec if active == 1 else pri
        self.tsz, self.poff, self.psz = tsz, poff, psz
        T = r[self.tab:self.tab + tsz]
        (magic, _v, iv_off, _ivs, dp_off, _dps, ph_off, _phs,
         ext, sel, _pad, extoff) = struct.unpack_from("<4sIQQQQQQBBHQ", T, 0)
        if magic != b"DIFI":
            raise FormatError("corrupted DIFI table")
        if not ext:
            raise FormatError("unsupported DIFF variant (data is not 'external IVFC level 4')")
        self.sel, self.extoff, self.ph_off = sel, extoff, ph_off
        iv = T[iv_off:iv_off + 0x78]
        if iv[:4] != b"IVFC":
            raise FormatError("corrupted IVFC descriptor")
        self.lv = [struct.unpack_from("<QQII", iv, 0x10 + i * 0x18) for i in range(4)]
        dp = T[dp_off:dp_off + 0x50]
        if dp[:4] != b"DPFS":
            raise FormatError("corrupted DPFS descriptor")
        self.dp = [struct.unpack_from("<QQII", dp, 8 + i * 24) for i in range(3)]
        self.data_off = poff + extoff
        self.data_len = self.lv[3][1]
        if self.data_off + self.data_len > len(r):
            raise FormatError("file is shorter than specified in the header")

    # --- DPFS: determine which copy (A/B) of each block is active ----------
    @staticmethod
    def _bit(buf, i):
        w = struct.unpack_from("<I", buf, (i // 32) * 4)[0]
        return (w >> (31 - i % 32)) & 1

    def _l3_map(self):
        r, base = self.raw, self.poff
        (o1, s1, _b1, _), (o2, s2, b2, _), (o3, s3, b3, _) = self.dp
        l1 = r[base + o1 + self.sel * s1: base + o1 + self.sel * s1 + s1]
        l2 = bytearray(s2)
        chunk = 1 << b2
        for k in range(0, s2, chunk):
            n = min(chunk, s2 - k)
            c = self._bit(l1, k // chunk)
            src = base + o2 + c * s2 + k
            l2[k:k + n] = r[src:src + n]

        def to_file(x):
            c = self._bit(l2, x >> b3)
            return base + o3 + c * s3 + x
        return to_file, 1 << b3

    def _l3_read(self, off, n):
        to_file, bs = self._l3_map()
        out = bytearray()
        while n:
            k = min(n, bs - off % bs)
            f = to_file(off)
            out += self.raw[f:f + k]
            off += k
            n -= k
        return bytes(out)

    def _l3_write(self, off, data):
        to_file, bs = self._l3_map()
        i = 0
        while i < len(data):
            k = min(len(data) - i, bs - off % bs)
            f = to_file(off)
            self.raw[f:f + k] = data[i:i + k]
            off += k
            i += k

    # --- data and hash tree -----------------------------------------------
    def data(self):
        return bytes(self.raw[self.data_off:self.data_off + self.data_len])

    def set_data(self, payload):
        if len(payload) != self.data_len:
            raise FormatError("data size changed - this is not allowed")
        self.raw[self.data_off:self.data_off + self.data_len] = payload

    @staticmethod
    def _level_hash(buf, log2):
        bs = 1 << log2
        return b"".join(sha(bytes(buf[i:i + bs]).ljust(bs, b"\0"))
                        for i in range(0, len(buf), bs))

    def _tree(self):
        l3 = self._level_hash(self.data(), self.lv[3][2])
        l2 = self._level_hash(l3, self.lv[2][2])
        l1 = self._level_hash(l2, self.lv[1][2])
        master = self._level_hash(l1, self.lv[0][2])
        return l1, l2, l3, master

    def verify(self):
        """Returns a list of problems (empty = everything is OK)."""
        bad = []
        l1, l2, l3, master = self._tree()
        stored = [self._l3_read(self.lv[i][0], self.lv[i][1]) for i in range(3)]
        if stored[2] != l3:
            n = sum(1 for i in range(0, len(l3), 32) if stored[2][i:i + 32] != l3[i:i + 32])
            bad.append("data hashes (IVFC L3) do not match in %d blocks" % n)
        if stored[1] != l2:
            bad.append("IVFC L2 hash mismatch")
        if stored[0] != l1:
            bad.append("IVFC L1 hash mismatch")
        t = self.tab + self.ph_off
        if bytes(self.raw[t:t + 0x20]) != master:
            bad.append("master hash in table does not match")
        if sha(self.raw[self.tab:self.tab + self.tsz]) != bytes(self.raw[0x134:0x154]):
            bad.append("table hash in DIFF header does not match")
        return bad

    def rehash(self):
        """Recalculates the entire hash tree for the current data (without CMAC)."""
        l1, l2, l3, master = self._tree()
        for i, h in enumerate((l1, l2, l3)):
            if len(h) != self.lv[i][1]:
                raise FormatError("unexpected IVFC level %d size" % (i + 1))
            self._l3_write(self.lv[i][0], h)
        t = self.tab + self.ph_off
        self.raw[t:t + 0x20] = master
        self.raw[0x134:0x154] = sha(self.raw[self.tab:self.tab + self.tsz])

    def to_bytes(self):
        return bytes(self.raw)


# ----------------------------------------------------------------------------
# Mii database (CFL_DB.dat contents)
# ----------------------------------------------------------------------------
def utf16z(b):
    return bytes(b).decode("utf-16le", "replace").split("\x00")[0]


def fmt_mac(b):
    return ":".join("%02x" % x for x in b)


def ts2000(sec):
    try:
        return (datetime.datetime(2000, 1, 1) + datetime.timedelta(seconds=sec)).strftime("%Y-%m-%d %H:%M")
    except OverflowError:
        return "?"


def mii_created(rec):
    mid = struct.unpack_from(">I", rec, 0x0C)[0]
    if not mid:
        return "-"
    try:
        d = datetime.datetime(2010, 1, 1) + datetime.timedelta(seconds=(mid & 0x0FFFFFFF) * 2)
        return d.strftime("%Y-%m-%d")
    except OverflowError:
        return "?"


class CflDb:
    def __init__(self, payload):
        if len(payload) != DB_SIZE:
            raise FormatError("data size %d != %d - this does not look like CFL_DB.dat" % (len(payload), DB_SIZE))
        self.p = bytearray(payload)
        if (self.p[0:4], self.p[CFHE_OFF:CFHE_OFF + 4], self.p[CFRA_OFF:CFRA_OFF + 4]) != (b"CFOG", b"CFHE", b"CFRA"):
            raise FormatError("missing CFOG/CFHE/CFRA signatures - this is not a Mii database")

    # --- Mii Maker --------------------------------------------------------
    def maker(self):
        out = []
        for k in range(MM_N):
            rec = bytes(self.p[MM_OFF + k * MM_SZ: MM_OFF + (k + 1) * MM_SZ])
            if rec[0] == 3 and any(rec):
                out.append((k, rec))
        return out

    # --- history (CFHE + 3000 x 0x54) ------------------------------------
    def node(self, i):
        o = NODE_OFF + i * NODE_SZ
        return bytes(self.p[o:o + 10]), struct.unpack_from("<HH", self.p, o + 10)  # (id+mac, (prev,next))

    def hist_rec(self, i):
        o = HIST_OFF + i * HIST_SZ
        return bytes(self.p[o:o + HIST_SZ])

    def hist_used(self):
        return [i for i in range(NODE_N) if any(self.p[NODE_OFF + i * NODE_SZ: NODE_OFF + i * NODE_SZ + 10])]

    def hist_order(self):
        """Returns indices from 'head' to 'tail' according to the list. None if the list is corrupted."""
        tail, head = struct.unpack_from("<HH", self.p, TAIL_OFF)
        if head == EMPTY and tail == EMPTY:
            return []
        order, seen, cur = [], set(), head
        while cur != NIL and cur != EMPTY:
            if cur >= NODE_N or cur in seen:
                return None
            seen.add(cur)
            order.append(cur)
            cur = self.node(cur)[1][1] & 0x7FFF
        return order

    # --- invitations (CFRA) ----------------------------------------------
    def inv_count(self):
        return struct.unpack_from("<I", self.p, INV_COUNT_OFF)[0]

    def inv_rec(self, i):
        o = INV_OFF + i * INV_SZ
        return bytes(self.p[o:o + INV_SZ])

    # --- consistency checks -----------------------------------------------
    def check(self):
        bad = []
        c1 = struct.unpack_from(">H", self.p, CRC1_OFF)[0]
        if c1 != crc16(self.p[:CRC1_OFF]):
            bad.append("CRC1 (Mii Maker + history) mismatch")
        c2 = struct.unpack_from(">H", self.p, CRC2_OFF)[0]
        if c2 != crc16(self.p[CFRA_OFF:CRC2_OFF]):
            bad.append("CRC2 (invitations) mismatch")
        used, order = self.hist_used(), self.hist_order()
        if order is None or sorted(order) != used:
            bad.append("history list (CFHE) is inconsistent")
        else:
            for a, b in zip(order, order[1:]):
                if self.node(b)[1][0] & 0x7FFF != a:
                    bad.append("history list: prev/next pointers do not match")
                    break
            tail = struct.unpack_from("<H", self.p, TAIL_OFF)[0]
            if order and tail != order[-1]:
                bad.append("history list: incorrect tail index")
        for i in used:
            r = self.hist_rec(i)
            if bytes(r[0x0C:0x10]) != self.node(i)[0][:4] or bytes(r[0x10:0x16]) != self.node(i)[0][4:]:
                bad.append("history record %d does not match its node" % i)
                break
        for i in range(NODE_N):
            if i not in set(used) and any(self.hist_rec(i)):
                bad.append("non-empty history record in unused node %d" % i)
                break
        n = self.inv_count()
        if n > INV_N:
            bad.append("invitation count %d > 100" % n)
        elif sorted(self.p[INV_ORDER_OFF:INV_ORDER_OFF + n]) != list(range(n)):
            bad.append("invitation order is not a permutation")
        return bad

    def fix_crc(self):
        struct.pack_into(">H", self.p, CRC1_OFF, crc16(self.p[:CRC1_OFF]))
        struct.pack_into(">H", self.p, CRC2_OFF, crc16(self.p[CFRA_OFF:CRC2_OFF]))

    # --- deletion ---------------------------------------------------------
    def delete_history(self, drop):
        used = self.hist_used()
        order = self.hist_order()
        if order is None or sorted(order) != used:
            raise FormatError("history list is corrupted - individual entries cannot be safely deleted")
        keep = [i for i in used if i not in drop]
        newidx = {old: n for n, old in enumerate(keep)}
        chain = [i for i in order if i in newidx]          # head -> tail, excluding deleted entries
        old_nodes = {i: (self.node(i), self.hist_rec(i)) for i in keep}
        blank = bytes(10) + struct.pack("<HH", NIL, NIL)
        for i in range(NODE_N):
            self.p[NODE_OFF + i * NODE_SZ: NODE_OFF + (i + 1) * NODE_SZ] = blank
        self.p[HIST_OFF:HIST_OFF + NODE_N * HIST_SZ] = bytes(NODE_N * HIST_SZ)
        pos = {old: k for k, old in enumerate(chain)}
        for old in keep:
            (idmac, (fprev, fnext)), rec = old_nodes[old]
            k, n = pos[old], newidx[old]
            prv = newidx[chain[k - 1]] if k > 0 else NIL
            nxt = newidx[chain[k + 1]] if k + 1 < len(chain) else NIL
            o = NODE_OFF + n * NODE_SZ
            self.p[o:o + 10] = idmac
            struct.pack_into("<HH", self.p, o + 10, prv | (fprev & 0x8000), nxt | (fnext & 0x8000))
            self.p[HIST_OFF + n * HIST_SZ: HIST_OFF + (n + 1) * HIST_SZ] = rec
        if chain:
            struct.pack_into("<HH", self.p, TAIL_OFF, newidx[chain[-1]], newidx[chain[0]])
        else:
            struct.pack_into("<HH", self.p, TAIL_OFF, EMPTY, EMPTY)
        return len(used) - len(keep)

    def delete_invitations(self, drop):
        n = self.inv_count()
        order = list(self.p[INV_ORDER_OFF:INV_ORDER_OFF + n])
        recs = [self.inv_rec(i) for i in range(n)]
        keep = [i for i in range(n) if i not in drop]
        newidx = {old: k for k, old in enumerate(keep)}
        self.p[INV_OFF:INV_OFF + INV_N * INV_SZ] = bytes(INV_N * INV_SZ)
        for old, k in newidx.items():
            self.p[INV_OFF + k * INV_SZ: INV_OFF + (k + 1) * INV_SZ] = recs[old]
        new_order = [newidx[o] for o in order if o in newidx]
        self.p[INV_ORDER_OFF:INV_ORDER_OFF + INV_N] = bytes(INV_N)
        self.p[INV_ORDER_OFF:INV_ORDER_OFF + len(new_order)] = bytes(new_order)
        struct.pack_into("<I", self.p, INV_COUNT_OFF, len(keep))
        return n - len(keep)


# ----------------------------------------------------------------------------
# File operations
# ----------------------------------------------------------------------------
def load(path):
    with open(path, "rb") as f:
        raw = f.read()
    diff = DiffFile(raw)
    return diff, CflDb(diff.data())


def parse_ranges(text, maxn, what):
    out = set()
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    bad = sorted(x for x in out if not 0 <= x < maxn)
    if bad:
        raise ValueError("%s: numbers outside the range 0..%d: %s" % (what, maxn - 1, bad))
    return out


def select_by_name(db, text):
    """Returns (history indices, invitation indices) containing the specified name."""
    t = text.lower()
    h = {i for i in db.hist_used() if t in utf16z(db.hist_rec(i)[0x1A:0x2E]).lower()}
    n = db.inv_count()
    v = {i for i in range(n) if t in utf16z(db.inv_rec(i)[0x1A:0x2E]).lower()}
    return h, v


def finalize(diff, db, maker_before):
    """Saves changes from 'db' to the 'diff' container (CRC + hash tree) and returns the file bytes.

    Performs a self-check: the result must be consistent, and the Mii Maker section
    must be identical to 'maker_before'."""
    db.fix_crc()
    diff.set_data(bytes(db.p))
    diff.rehash()
    out = diff.to_bytes()
    d2 = DiffFile(out)
    db2 = CflDb(d2.data())
    err = d2.verify() + db2.check()
    if err:
        raise FormatError("output self-check failed: " + "; ".join(err))
    if bytes(db2.p[:MAKER_END]) != maker_before:
        raise FormatError("CRITICAL ERROR: Mii Maker section was modified - aborting without saving anything")
    return out


def process(path, outdir, drop_hist, drop_inv):
    diff, db = load(path)
    before_maker = bytes(db.p[:MAKER_END])
    problems = diff.verify() + db.check()
    if problems:
        print("  WARNING - input file contains inconsistencies (%s)." % "; ".join(problems))
        print("  This may be a normal state saved while the console was running; processing will continue.")
    removed_h = db.delete_history(drop_hist) if drop_hist else 0
    removed_i = db.delete_invitations(drop_inv) if drop_inv else 0
    out = finalize(diff, db, before_maker)
    dest = os.path.join(outdir, os.path.basename(path))
    if os.path.abspath(dest) == os.path.abspath(path):
        raise FormatError("output directory is the same as the source directory - choose another one (original files are never overwritten)")
    os.makedirs(outdir, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(out)
    return dest, removed_h, removed_i, len(db.maker())


def show(path, full=True):
    diff, db = load(path)
    print("=" * 72)
    print("File:", path)
    print("-" * 72)
    probs = diff.verify() + db.check()
    print("Consistency check:", "OK (DIFF hashes, CRC, history list)" if not probs else "; ".join(probs))
    print("AES-CMAC: not checked (requires console keys) - fix it in GodMode9")
    mm, hu, ni = db.maker(), db.hist_used(), db.inv_count()
    print("Mii Maker : %3d / %d   (NOT MODIFIED BY THIS PROGRAM)" % (len(mm), MM_N))
    print("History   : %3d / %d   (encountered Miis: StreetPass, multiplayer)" % (len(hu), NODE_N))
    print("Invitations: %2d / %d   (Mii Plaza - 'invitations')" % (ni, INV_N))
    if not full:
        return
    print("\n--- Mii Maker (file position | name | creator | creation date) ---")
    for k, r in mm:
        print("  %3d  %-12s %-12s %s" % (k, utf16z(r[0x1A:0x2E]), utf16z(r[0x48:0x5C]), mii_created(r)))
    print("\n--- History (number | name | creator console MAC | encounter time according to console clock) ---")
    for i in hu:
        r = db.hist_rec(i)
        print("  %4d  %-12s %s  %s" % (i, utf16z(r[0x1A:0x2E]), fmt_mac(r[0x10:0x16]),
                                      ts2000(struct.unpack_from("<I", r, 0x48)[0])))
    print("\n--- Invitations (number | name | MAC | creation date) ---")
    for i in range(ni):
        r = db.inv_rec(i)
        print("  %4d  %-12s %s  %s" % (i, utf16z(r[0x1A:0x2E]), fmt_mac(r[0x10:0x16]), mii_created(r)))


def run_clean(files, outdir, drop_hist_spec, drop_inv_spec, name_filter, everything_h, everything_i):
    print()
    for path in files:
        _, db = load(path)
        dh = set(db.hist_used()) if everything_h else parse_ranges(drop_hist_spec, NODE_N, "history")
        di = set(range(db.inv_count())) if everything_i else parse_ranges(drop_inv_spec, INV_N, "invitations")
        if name_filter:
            nh, ni = select_by_name(db, name_filter)
            dh |= nh
            di |= ni
        dh &= set(db.hist_used())
        di = {i for i in di if i < db.inv_count()}
        print("%s:" % path)
        dest, rh, ri, mm = process(path, outdir, dh, di)
        print("  removed from history: %d, from invitations: %d" % (rh, ri))
        print("  Mii Maker unchanged: %d Miis (confirmed by byte-by-byte comparison)" % mm)
        print("  saved: %s" % dest)
    print("\nDONE. Remember: files in '%s' still have the old AES-CMAC - fix it in GodMode9 before use" % outdir)
    print("(select 'Fix CMAC'), otherwise the console will reject the file.")


# ----------------------------------------------------------------------------
# Editing session (logic without the interface - used by the browser UI)
# ----------------------------------------------------------------------------
class Loaded:
    """One loaded file together with its editing state."""

    def __init__(self, name, raw):
        self.name = name
        self.orig = bytes(raw)
        self.id = 0
        self._init()

    def _init(self):
        self.diff = DiffFile(self.orig)
        self.db = CflDb(self.diff.data())
        self.maker_before = bytes(self.db.p[:MAKER_END])
        self.problems = self.diff.verify() + self.db.check()
        self.changed = False
        self.exported = False

    def reset(self):
        self._init()

    # Mii key = ID(4) + MAC(6); identical in history and invitations
    def all_indices(self, kind):
        return self.db.hist_used() if kind == "hist" else list(range(self.db.inv_count()))

    def key(self, kind, i):
        if kind == "hist":
            return self.db.node(i)[0]
        return self.db.inv_rec(i)[0x0C:0x16]

    def drop(self, kind, idx):
        valid = set(self.all_indices(kind))
        idx = {i for i in idx if i in valid}
        if not idx:
            return 0
        n = self.db.delete_history(idx) if kind == "hist" else self.db.delete_invitations(idx)
        if n:
            self.changed, self.exported = True, False
        return n

    def counts(self):
        return {"maker": len(self.db.maker()), "hist": len(self.db.hist_used()), "inv": self.db.inv_count()}

    def info(self):
        return {"id": self.id, "name": self.name, "problems": self.problems, "counts": self.counts(),
                "changed": self.changed, "exported": self.exported}

    def rows(self):
        db = self.db
        maker = []
        for k, r in db.maker():
            b2 = r[2]
            maker.append({"i": k, "ord": (b2 & 15) * 16 + (b2 >> 4),
                          "pos": "Page %d, slot %d" % ((b2 & 15) + 1, (b2 >> 4) + 1),
                          "name": utf16z(r[0x1A:0x2E]), "author": utf16z(r[0x48:0x5C]),
                          "created": mii_created(r)})
        hist = []
        for i in db.hist_used():
            r = db.hist_rec(i)
            hist.append({"i": i, "name": utf16z(r[0x1A:0x2E]), "mac": fmt_mac(r[0x10:0x16]),
                         "met": ts2000(struct.unpack_from("<I", r, 0x48)[0])})
        inv = []
        for i in range(db.inv_count()):
            r = db.inv_rec(i)
            inv.append({"i": i, "name": utf16z(r[0x1A:0x2E]), "mac": fmt_mac(r[0x10:0x16]),
                        "created": mii_created(r)})
        return {"maker": maker, "hist": hist, "inv": inv}

    def export(self):
        """Returns the file bytes with the applied changes (always calculated from the untouched original)."""
        return finalize(DiffFile(self.orig), CflDb(self.db.p), self.maker_before)


class Session:
    def __init__(self):
        self.files = []
        self._next = 1

    def add(self, name, raw):
        f = Loaded(name, raw)                       # may raise FormatError
        self.files = [x for x in self.files if x.name != name]
        f.id, self._next = self._next, self._next + 1
        self.files.append(f)
        return f

    def get(self, fid):
        for f in self.files:
            if f.id == fid:
                return f
        raise FormatError("file not found in session")

    def delete(self, fid, kind, indices, apply_all):
        """indices=None means 'all'. With apply_all, other files are matched by ID+MAC."""
        if kind not in ("hist", "inv"):
            raise FormatError("unknown section")
        cur = self.get(fid)
        others = [f for f in self.files if f is not cur] if apply_all else []
        plan = {}
        if indices is None:
            for f in [cur] + others:
                plan[f.id] = set(f.all_indices(kind))
        else:
            valid = set(cur.all_indices(kind))
            idx = {int(i) for i in indices}
            if not idx <= valid:
                raise FormatError("invalid entry indices")
            keys = {cur.key(kind, i) for i in idx}
            plan[cur.id] = idx
            for f in others:
                plan[f.id] = {i for i in f.all_indices(kind) if f.key(kind, i) in keys}
        return {str(f.id): f.drop(kind, plan[f.id]) for f in [cur] + others}

    def wipe(self):
        out = {}
        for f in self.files:
            out[str(f.id)] = f.drop("hist", set(f.all_indices("hist"))) + f.drop("inv", set(f.all_indices("inv")))
        return out

    def reset(self):
        for f in self.files:
            f.reset()


# ----------------------------------------------------------------------------
# Browser interface (local server on 127.0.0.1)
# ----------------------------------------------------------------------------
HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EditMii v1.0 – 3DS Mii Database Editor</title>
<style>
:root{--bg:#f4f5f8;--card:#fff;--text:#1d2530;--muted:#667085;--line:#e2e6ee;--row:#f3f5f9;--accent:#1c7ed6;--ok:#2b8a3e;--warn:#d9480f;--danger:#e03131;--lock:#5f3dc4}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2229;--text:#e6e9ee;--muted:#9aa4b5;--line:#2c333d;--row:#232a33;--accent:#4dabf7;--ok:#69db7c;--warn:#ffa94d;--danger:#ff8787;--lock:#b197fc}}
*{box-sizing:border-box}
[hidden]{display:none !important}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1040px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:24px;margin:6px 0 2px}h1 small{font-weight:400;color:var(--muted);font-size:16px}
.sub{color:var(--muted);margin:0 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:0 0 14px}
.card h2{font-size:16px;margin:0 0 10px}
.drop{display:block;border:2px dashed var(--line);border-radius:10px;padding:22px;text-align:center;cursor:pointer;color:var(--muted)}
.drop.over,.drop:hover{border-color:var(--accent);color:var(--text)}
.drop input{display:none}
.frow{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:8px 0;border-top:1px solid var(--line)}
.frow:first-child{border-top:0}
.frow b{min-width:100px}
.badge{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid currentColor}
.ok{color:var(--ok)}.warn{color:var(--warn)}.lock{color:var(--lock)}.danger{color:var(--danger)}
.muted{color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:12px}
.stat{border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:var(--card);text-align:left;color:inherit;font:inherit;cursor:pointer}
.stat.active{outline:2px solid var(--accent)}
.stat .n{font-size:24px;font-weight:650;display:block}
.stat .l{color:var(--muted);font-size:13px}
.seg{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px}
.seg button{padding:6px 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:inherit;cursor:pointer;font:inherit}
.seg button.on{background:var(--accent);border-color:var(--accent);color:#fff}
button.btn,a.btn{display:inline-block;padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:inherit;font:inherit;cursor:pointer;text-decoration:none}
button.btn:hover,a.btn:hover{border-color:var(--accent)}
button.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.danger{background:var(--danger);border-color:var(--danger);color:#fff}
.btn.big{padding:11px 18px;font-weight:600}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 10px}
.bar input[type=search]{flex:1;min-width:160px;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:inherit;font:inherit}
.tablewrap{max-height:430px;overflow:auto;border:1px solid var(--line);border-radius:8px}
table{width:100%;border-collapse:collapse}
th,td{padding:6px 10px;text-align:left;white-space:nowrap}
th{position:sticky;top:0;background:var(--card);border-bottom:1px solid var(--line);cursor:pointer;user-select:none;font-size:13px;color:var(--muted)}
tr.r:nth-child(even){background:var(--row)}
tr.r.sel{background:color-mix(in srgb,var(--accent) 22%,transparent)}
tr.r{cursor:pointer}
td.mac{font-family:ui-monospace,Consolas,monospace;font-size:13px}
.note{border-left:4px solid var(--warn);padding:8px 12px;background:var(--row);border-radius:0 8px 8px 0;margin:10px 0 0;font-size:14px}
.note.lockn{border-color:var(--lock)}
#toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:#222;color:#fff;padding:10px 16px;border-radius:10px;opacity:0;transition:opacity .25s;pointer-events:none;max-width:90vw}
#toast.show{opacity:.96}#toast.err{background:#c92a2a}
label.chk{display:inline-flex;gap:6px;align-items:center;cursor:pointer}
@media (max-width:640px){.cards{grid-template-columns:1fr}}
</style>
</head>
<body>
<main>
<h1>EditMii v1.0 <small>3DS Mii Database Editor</small></h1>
<p class="sub">Runs locally – loaded files are never uploaded anywhere. Original files are never modified. Edited files are downloaded as new files.</p>

<section class="card" id="load">
  <label class="drop" id="drop">
    <input type="file" id="fileinp" multiple>
    <b>Select files</b> <span>(CFL_DB.dat and CFL_OldDB.dat, e.g. <code>00000005</code> and <code>00000006</code>) or drag and drop them here</span>
  </label>
  <div id="files"></div>
</section>

<div id="main" hidden>
  <section class="card">
    <div class="bar" id="filebar"></div>
    <div class="cards" id="cards"></div>
    <div class="bar">
      <button class="btn danger big" id="wipe">Delete everything except Mii Maker</button>
      <span class="muted">clears encounter history and invitations in all loaded files</span>
    </div>
  </section>

  <section class="card">
    <div class="bar" id="tools">
      <input type="search" id="flt" placeholder="Search by name…">
      <button class="btn" id="selvis">Select visible</button>
      <button class="btn danger" id="delsel">Delete selected</button>
      <button class="btn" id="delall">Delete all in this section</button>
    </div>
    <div class="bar" id="applybar"><label class="chk"><input type="checkbox" id="applyall" checked> Also delete from other loaded files (match by ID + MAC)</label></div>
    <div class="tablewrap"><table><thead id="thead"></thead><tbody id="tbody"></tbody></table></div>
    <div class="note lockn" id="locknote" hidden>The Mii Maker section is read-only – it is not modified by the program and has no deletion buttons.</div>
  </section>

  <section class="card">
    <h2>Download edited files</h2>
    <div class="bar" id="dl"></div>
    <div class="note"><b>Important:</b> downloaded files still have the old AES-CMAC. After transferring them to your 3DS, run <b>Fix CMAC</b> in GodMode9 on each file, otherwise the console will reject it. Back up the original files first.</div>
  </section>

  <div class="bar">
    <button class="btn" id="reset">Undo all changes</button>
    <button class="btn" id="clear">Close files</button>
    <button class="btn" id="quit">Exit program</button>
  </div>
</div>
</main>
<div id="toast"></div>
<script>
(() => {
const T = "__TOKEN__";
const $ = s => document.querySelector(s);
const el = (tag, props = {}, ...kids) => { const e = document.createElement(tag); Object.assign(e, props); kids.forEach(k => e.append(k)); return e; };
const TABS = {hist: "Encounter History", inv: "Invitations", maker: "Mii Maker"};
const COLS = {
  hist: [["i","No."],["name","Name"],["mac","Creator console MAC"],["met","Encountered (console clock)"]],
  inv: [["i","No."],["name","Name"],["mac","Creator console MAC"],["created","Created"]],
  maker: [["ord","Slot"],["name","Name"],["author","Creator"],["created","Created"]]
};
let files = [], cur = null, tab = "hist", rows = {hist: [], inv: [], maker: []}, sel = new Set(), sortKey = null, sortDir = 1, toastT = null;

async function api(path, opts = {}) {
  const r = await fetch(path + (path.includes("?") ? "&" : "?") + "t=" + T,
    Object.assign({headers: {"Content-Type": "application/json"}}, opts));
  let j = {};
  try { j = await r.json(); } catch (e) { j = {error: "Invalid server response"}; }
  if (!r.ok || j.error) throw new Error(j.error || r.statusText);
  return j;
}
function toast(msg, err) {
  const t = $("#toast"); t.textContent = msg; t.className = "show" + (err ? " err" : "");
  clearTimeout(toastT); toastT = setTimeout(() => t.className = "", 4200);
}
const curFile = () => files.find(f => f.id === cur);

async function refresh() {
  files = (await api("/api/state")).files;
  if (!files.some(f => f.id === cur)) cur = files.length ? files[0].id : null;
  rows = cur ? await api("/api/list?file=" + cur) : {hist: [], inv: [], maker: []};
  render();
}

function render() {
  const has = files.length > 0;
  $("#main").hidden = !has;
  const fl = $("#files"); fl.replaceChildren();
  files.forEach(f => {
    const ok = f.problems.length === 0;
    fl.append(el("div", {className: "frow"},
      el("b", {textContent: f.name}),
      el("span", {className: "badge " + (ok ? "ok" : "warn"), textContent: ok ? "✓ File is consistent" : "⚠ Inconsistencies"}),
      el("span", {className: "muted", textContent: `Mii Maker ${f.counts.maker} · history ${f.counts.hist} · invitations ${f.counts.inv}`}),
      f.changed ? el("span", {className: "badge warn", textContent: f.exported ? "modified · downloaded" : "modified · not downloaded"}) : "",
      ok ? "" : el("span", {className: "muted", textContent: f.problems.join("; ")})));
  });
  if (!has) return;
  const fb = $("#filebar"); fb.replaceChildren();
  if (files.length > 1) {
    fb.append(el("span", {className: "muted", textContent: "Viewing file:"}));
    const seg = el("div", {className: "seg"});
    files.forEach(f => seg.append(el("button", {className: f.id === cur ? "on" : "", textContent: f.name,
      onclick: async () => { cur = f.id; sel.clear(); await refresh(); }})));
    fb.append(seg);
  }
  const c = curFile().counts, cards = $("#cards"); cards.replaceChildren();
  [["hist","Encounter History",c.hist,"/ 3000 · StreetPass, multiplayer"],
   ["inv","Mii Plaza Invitations",c.inv,"/ 100"],
   ["maker","Mii Maker",c.maker,"/ 100 · read-only"]].forEach(([k,l,n,s]) =>
    cards.append(el("button", {className: "stat" + (tab === k ? " active" : ""),
      onclick: () => { tab = k; sel.clear(); sortKey = null; render(); }},
      el("span", {className: "n", textContent: n}), el("span", {className: "l", textContent: l + " " + s}))));
  $("#applybar").hidden = files.length < 2 || tab === "maker";
  $("#locknote").hidden = tab !== "maker";
  ["selvis","delsel","delall"].forEach(id => $("#" + id).hidden = tab === "maker");
  renderTable();
  const dl = $("#dl"); dl.replaceChildren();
  files.forEach(f => dl.append(el("a", {className: "btn primary", href: `/api/download?file=${f.id}&t=${T}`,
    download: f.name, textContent: "Download " + f.name, onclick: () => setTimeout(refresh, 700)})));
  if (files.length > 1) dl.append(el("a", {className: "btn", href: `/api/zip?t=${T}`, download: "mii_edited.zip",
    textContent: "Download all (ZIP)", onclick: () => setTimeout(refresh, 700)}));
}

function visibleRows() {
  const q = $("#flt").value.trim().toLowerCase();
  let r = rows[tab].filter(x => !q || (x.name + " " + (x.author || "")).toLowerCase().includes(q));
  const key = sortKey || COLS[tab][0][0];
  return r.slice().sort((a, b) => (a[key] > b[key] ? 1 : a[key] < b[key] ? -1 : 0) * sortDir);
}
function renderTable() {
  const head = $("#thead"), body = $("#tbody"), editable = tab !== "maker", vis = visibleRows();
  head.replaceChildren(); body.replaceChildren();
  const tr = el("tr");
  if (editable) tr.append(el("th", {textContent: "✓"}));
  COLS[tab].forEach(([k, l]) => tr.append(el("th", {textContent: l + (sortKey === k ? (sortDir > 0 ? " ▲" : " ▼") : ""),
    onclick: () => { sortDir = sortKey === k ? -sortDir : 1; sortKey = k; renderTable(); }})));
  head.append(tr);
  vis.forEach(x => {
    const r = el("tr", {className: "r" + (sel.has(x.i) ? " sel" : "")});
    if (editable) {
      const cb = el("input", {type: "checkbox", checked: sel.has(x.i)});
      cb.onclick = e => e.stopPropagation();
      cb.onchange = () => toggle(x.i, r, cb);
      r.append(el("td", {}, cb));
      r.onclick = () => { cb.checked = !cb.checked; toggle(x.i, r, cb); };
    }
    COLS[tab].forEach(([k]) => r.append(el("td", {className: k === "mac" ? "mac" : "", textContent: k === "ord" ? x.pos : x[k]})));
    body.append(r);
  });
  if (!vis.length) body.append(el("tr", {}, el("td", {colSpan: 6, className: "muted", textContent: "No entries."})));
  updateSel();
}
function toggle(i, r, cb) { cb.checked ? sel.add(i) : sel.delete(i); r.classList.toggle("sel", cb.checked); updateSel(); }
function updateSel() { const b = $("#delsel"); b.textContent = `Delete selected (${sel.size})`; b.disabled = sel.size === 0; $("#delall").disabled = rows[tab].length === 0; }

async function del(all) {
  const applyAll = $("#applyall").checked && files.length > 1;
  const n = all ? rows[tab].length : sel.size;
  const where = applyAll ? "in ALL loaded files" : "in file " + curFile().name;
  const what = all ? `ALL entries (${n}) from the "${TABS[tab]}" section` : `${n} selected entries from the "${TABS[tab]}" section`;
  if (!confirm(`Delete ${what} ${where}?\n\nMii Maker will remain unchanged. Original files will not be modified.`)) return;
  try {
    const res = await api("/api/delete", {method: "POST", body: JSON.stringify({file: cur, kind: tab, indices: all ? null : [...sel], apply_all: applyAll})});
    const tot = Object.values(res.removed).reduce((a, b) => a + b, 0);
    sel.clear(); await refresh(); toast(`Entries deleted: ${tot}`);
  } catch (e) { toast(e.message, true); }
}

async function upload(list) {
  try {
    for (const f of list) {
      const r = await fetch("/api/upload?name=" + encodeURIComponent(f.name) + "&t=" + T, {method: "POST", body: f});
      const j = await r.json();
      if (!r.ok || j.error) throw new Error(`${f.name}: ${j.error || r.statusText}`);
    }
    sel.clear(); await refresh(); toast("Files loaded");
  } catch (e) { toast(e.message, true); }
}

$("#fileinp").onchange = e => { upload([...e.target.files]); e.target.value = ""; };
const drop = $("#drop");
["dragenter","dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave","drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", e => upload([...e.dataTransfer.files]));
$("#flt").oninput = renderTable;
$("#selvis").onclick = () => { visibleRows().forEach(x => sel.add(x.i)); renderTable(); };
$("#delsel").onclick = () => del(false);
$("#delall").onclick = () => del(true);
$("#wipe").onclick = async () => {
  if (!confirm("Delete ALL Miis outside Mii Maker (encounter history and invitations) from all loaded files?\n\nMii Maker will remain unchanged.")) return;
  try { const res = await api("/api/wipe", {method: "POST", body: "{}"}); sel.clear(); await refresh();
    toast("Entries deleted: " + Object.values(res.removed).reduce((a, b) => a + b, 0)); } catch (e) { toast(e.message, true); }
};
$("#reset").onclick = async () => { if (!confirm("Undo all changes and restore the loaded original files?")) return;
  await api("/api/reset", {method: "POST", body: "{}"}); sel.clear(); await refresh(); toast("Changes undone"); };
$("#clear").onclick = async () => { if (files.some(f => f.changed && !f.exported) && !confirm("There are changes that have not been downloaded. Close the files anyway?")) return;
  await api("/api/clear", {method: "POST", body: "{}"}); sel.clear(); await refresh(); };
$("#quit").onclick = async () => { if (files.some(f => f.changed && !f.exported) && !confirm("There are changes that have not been downloaded. Exit anyway?")) return;
  try { await api("/api/quit", {method: "POST", body: "{}"}); } catch (e) {}
  document.body.innerHTML = "<main><h1>Program exited</h1><p class='sub'>You can close this tab.</p></main>"; };
window.addEventListener("beforeunload", e => { if (files.some(f => f.changed && !f.exported)) { e.preventDefault(); e.returnValue = ""; } });
refresh().catch(e => toast(e.message, true));
})();
</script>
</body>
</html>
"""


def _json_bytes(obj):
    return json.dumps(obj).encode("utf-8")


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "mii3ds"
    app = None  # set in run_ui: {"session", "token", "lock", "httpd"}

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; connect-src 'self'; "
                         "style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._send(code, _json_bytes({"error": msg}))

    def _guard(self, q, need_token=True):
        host = self.headers.get("Host", "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost"):
            self._err(403, "disallowed host")
            return False
        if need_token and q.get("t", [""])[0] != self.app["token"]:
            self._err(403, "invalid session token")
            return False
        return True

    def _body(self, limit=4 * 1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise FormatError("file is too large")
        return self.rfile.read(n)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            if not self._guard(q, need_token=False):
                return
            page = HTML_PAGE.replace("__TOKEN__", self.app["token"]).encode("utf-8")
            return self._send(200, page, "text/html; charset=utf-8")
        if not self._guard(q):
            return
        ses, lock = self.app["session"], self.app["lock"]
        try:
            with lock:
                if u.path == "/api/state":
                    return self._send(200, _json_bytes({"files": [f.info() for f in ses.files]}))
                if u.path == "/api/list":
                    return self._send(200, _json_bytes(ses.get(int(q["file"][0])).rows()))
                if u.path == "/api/download":
                    f = ses.get(int(q["file"][0]))
                    data = f.export()
                    f.exported = True
                    return self._send(200, data, "application/octet-stream",
                                      {"Content-Disposition": 'attachment; filename="%s"' % f.name})
                if u.path == "/api/zip":
                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
                        for f in ses.files:
                            z.writestr(f.name, f.export())
                            f.exported = True
                    return self._send(200, buf.getvalue(), "application/zip",
                                      {"Content-Disposition": 'attachment; filename="mii_edited.zip"'})
            self._err(404, "page not found")
        except (FormatError, KeyError, ValueError, IndexError) as e:
            self._err(400, str(e) or "invalid request")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if not self._guard(q):
            return
        ses, lock = self.app["session"], self.app["lock"]
        try:
            raw = self._body()
            with lock:
                if u.path == "/api/upload":
                    name = re.sub(r"[^\w.\-]", "_", os.path.basename(q.get("name", ["file"])[0])) or "file"
                    f = ses.add(name, raw)
                    return self._send(200, _json_bytes(f.info()))
                req = json.loads(raw or b"{}")
                if u.path == "/api/delete":
                    removed = ses.delete(int(req["file"]), req["kind"], req.get("indices"), bool(req.get("apply_all")))
                    return self._send(200, _json_bytes({"removed": removed}))
                if u.path == "/api/wipe":
                    return self._send(200, _json_bytes({"removed": ses.wipe()}))
                if u.path == "/api/reset":
                    ses.reset()
                    return self._send(200, _json_bytes({"ok": True}))
                if u.path == "/api/clear":
                    ses.files = []
                    return self._send(200, _json_bytes({"ok": True}))
                if u.path == "/api/quit":
                    self._send(200, _json_bytes({"ok": True}))
                    threading.Thread(target=self.app["httpd"].shutdown, daemon=True).start()
                    return
            self._err(404, "page not found")
        except (FormatError, KeyError, ValueError, TypeError) as e:
            self._err(400, str(e) or "invalid request")


def run_ui(paths, port=0, open_browser=True):
    session = Session()
    for p in paths:
        with open(p, "rb") as fh:
            session.add(os.path.basename(p), fh.read())
    app = {"session": session, "token": secrets.token_urlsafe(18), "lock": threading.Lock()}
    _Handler.app = app
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    app["httpd"] = httpd
    url = "http://127.0.0.1:%d/" % httpd.server_address[1]
    print("Interface available at:", url)
    print("(runs only on this computer; exit using the 'Exit program' button or Ctrl+C)")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


# ----------------------------------------------------------------------------
# Interactive mode and CLI
# ----------------------------------------------------------------------------
def interactive():
    print("mii3ds - Nintendo 3DS Mii database cleaner (CFL_DB.dat / CFL_OldDB.dat)\n")
    guess = [p for p in ("00000005", "00000006") if os.path.exists(p)]
    if guess:
        print("Found in the current directory:", ", ".join(guess))
        ans = input("Use these files? [Y/n]: ").strip().lower()
        files = guess if ans in ("", "t", "tak", "y", "yes") else []
    else:
        files = []
    if not files:
        raw = input("Enter file paths (separated by spaces/semicolons): ")
        files = [x.strip().strip('"') for x in raw.replace(";", " ").split() if x.strip()]
    if not files:
        return
    show(files[0], full=False)
    print("\nWhat would you like to do?")
    print("  1) Delete EVERYTHING except Mii Maker (history + invitations)  [recommended]")
    print("  2) Delete encounter history only")
    print("  3) Delete invitations only")
    print("  4) Show the complete list of Miis")
    print("  5) Delete selected entries (by numbers / part of a name)")
    ch = input("Selection [1-5]: ").strip()
    out = input("Output directory [output]: ").strip() or "output"
    if ch == "1":
        run_clean(files, out, "", "", "", True, True)
    elif ch == "2":
        run_clean(files, out, "", "", "", True, False)
    elif ch == "3":
        run_clean(files, out, "", "", "", False, True)
    elif ch == "4":
        for p in files:
            show(p)
    elif ch == "5":
        h = input("History indices (e.g. 0,5,10-20; empty = none): ")
        v = input("Invitation indices (e.g. 0-9; empty = none): ")
        n = input("Part of a name (empty = none): ")
        run_clean(files, out, h, v, n, False, False)


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Nintendo 3DS Mii database viewer and cleaner (CFL_DB.dat).")
    sub = ap.add_subparsers(dest="cmd")
    u = sub.add_parser("ui", help="browser interface (default when launched without arguments)")
    u.add_argument("files", nargs="*", help="files to load at startup (optional)")
    u.add_argument("--port", type=int, default=0, help="port (a free random port by default)")
    u.add_argument("--bez-przegladarki", action="store_true", help="do not open the browser automatically")
    sub.add_parser("menu", help="simple text menu")
    a = sub.add_parser("info", help="show contents and check consistency")
    a.add_argument("files", nargs="+")
    a.add_argument("--krotko", action="store_true", help="summary only")
    w = sub.add_parser("wipe", help="delete everything except Mii Maker")
    w.add_argument("files", nargs="+")
    w.add_argument("-o", "--out", default="output")
    w.add_argument("--tylko-historie", action="store_true")
    w.add_argument("--tylko-zaproszenia", action="store_true")
    d = sub.add_parser("delete", help="delete selected Miis")
    d.add_argument("files", nargs="+")
    d.add_argument("-o", "--out", default="output")
    d.add_argument("--historia", default="", help="indices, e.g. 0,5,10-20")
    d.add_argument("--zaproszenia", default="", help="indices, e.g. 0-9")
    d.add_argument("--imie", default="", help="delete Miis whose names contain this text")
    args = ap.parse_args()
    try:
        if args.cmd in (None, "ui"):
            files = list(getattr(args, "files", []) or [])
            if not files:
                files = [p for p in ("00000005", "00000006") if os.path.exists(p)]
                if files:
                    print("EditMii v1.0")
                    print("Loading from the current directory:", ", ".join(files))
            run_ui(files, getattr(args, "port", 0), not getattr(args, "bez-przegladarki", False))
        elif args.cmd == "menu":
            interactive()
        elif args.cmd == "info":
            for p in args.files:
                show(p, full=not args.krotko)
        elif args.cmd == "wipe":
            run_clean(args.files, args.out, "", "", "", not args.tylko_zaproszenia, not args.tylko_historie)
        elif args.cmd == "delete":
            if not (args.historia or args.zaproszenia or args.imie):
                ap.error("specify --historia, --zaproszenia and/or --imie")
            run_clean(args.files, args.out, args.historia, args.zaproszenia, args.imie, False, False)
    except (FormatError, ValueError, OSError) as e:
        print("ERROR:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()