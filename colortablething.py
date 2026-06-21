#!/usr/bin/env python3
"""
colortablething.py - patch DaVinci Resolve ColorChecker reference tables
                     to user-supplied XYZ values. Multi-OS, stdlib-only.

The chart references inside Resolve are zlib-compressed XML files embedded in
the Resolve binary (Qt RCC data). This tool finds them without hard-coded
offsets, rewrites the <color xyz="..."/> values you specify, recompresses, and
writes the result back into the same slot.

Run this file with no arguments in an interactive terminal to open the TUI.

SAFETY MODEL:
  * It never grows anything. The new compressed payload is always <= the
    original slot length (zlib levels 1..9 are tried, then precision is
    trimmed if needed). Resolve/Qt reads the stored compressed length and
    decompresses with inflate, which stops at the stream's Adler32 and ignores
    any trailing slack - so a shorter in-place stream is transparent to Qt.
  * It edits ONLY the xyz="..." attribute of the <color> tags you specify.
    Everything else (layout, UI coords, rgb, other charts) is byte-preserved.
  * It backs up the binary, refuses to run while Resolve is running, verifies
    the patch by re-reading and re-decompressing, and supports --dry-run.

INPUT FORMATS (pick one):
  --json  FILE     Canonical JSON, simple mapping, or 24-item list (see below)
  --csv   FILE     CSV: index,x,y,z[,r,g,b]   (one row per patch)
  --xml   FILE     Resolve-style colorchart XML; imports every <grid index><color xyz>
                   Also accepts RGB-table XML with <color no="001"><R/G/B>.
  --table NAME/PATH
                   user-added/built-in table name, or JSON/XML/CSV table file
  --preset NAME    built-in preset or alias, e.g. type8 or type9
  --set   N=x,y,z  repeatable, e.g. --set 0=0.124,0.110,0.071 --set 19=0.95,1.0,0.83

CANONICAL JSON FORMAT:
  {
    "format": "davinci-colorchecker-values/v1",
    "chart": "X-Rite ColorChecker",
    "encoding": "CIEXYZ",
    "patches": [
      {"index": 0, "name": "Dark Skin", "xyz": [0.124176, 0.110316, 0.071575]},
      ...
    ]
  }

Also accepted: {"0":[x,y,z]}, {"0":{"xyz":[x,y,z]}}, [[x,y,z], ...],
or {"values": ...}. Exports use the canonical format by default.

OTHER:
  --list           print every chart found in the binary (read-only)
  --print-current  print the current 24 xyz values of the target chart
  --tui            interactive color-preview editor for staging/applying edits
  --export-json P  write current/staged chart values as canonical JSON and exit
  --export-xml P   write current/staged chart values as Resolve-style XML and exit
  --format-help    print accepted input/output formats and exit
  --list-presets   print built-in presets and exit
  --list-tables    print built-in + user-added tables and exit
  --add-table P    add JSON/XML/CSV table to the app-side table library
  --base NAME      alias for --chart: base Resolve chart/slot to target
  --chart NAME     substring of the <colorchart name="..."> (default: the
                   current 'X-Rite ColorChecker', skipping the 'Pre November 2014')
                   aliases: legacy, classic, current
  --binary PATH    override the Resolve binary location
  --dry-run        show the planned edits, write nothing
  --restore        restore the most recent backup
  --list-slots     print Resolve's fixed Color Match dropdown slots
  --install-table-slot SLOT
                   convenience wrapper: choose SLOT as --chart target
  --restore-slot SLOT
                   restore one chart slot from the most recent backup
  --restore-all-slots
                    restore all chart streams/labels from the most recent backup
  --install-type8   install a true extra Color Match dropdown entry backed by an
                    external type=8 XML chart (Linux Resolve 21 layout)
  --install-type9   install a second extra entry: type 8 remains the default
                    AliExpress chart, type 9 uses the supplied chart data
  --restore-type8   remove the type=8 binary hooks; use --restore for a full
                    byte-for-byte backup restore
  --yes            skip confirmation prompt
  --force          patch even if a Resolve process appears to be running
  --wait-resolve   wait until Resolve exits, then continue
  --kill-resolve   warn, terminate Resolve, then continue
  --rename-label N rename the visible Resolve dropdown label for the selected chart slot
  --rename-table A --to B  rename an app-side user table
  --remove-table A delete an app-side user table

NOTE: resolves values to 6 decimals (Resolve's native precision). Values are
CIEXYZ (Y ~ normalized to ~1.0 for white), matching the stock <color xyz=...>.
The Color Match solver reads xyz (not rgb), so rgb is left untouched.
"""

import argparse
import csv
import json
import os
import re
import signal
import sys
import time
import zlib
from pathlib import Path

_TUI_RESIZED = False


def _mark_tui_resized(_signum, _frame):
    global _TUI_RESIZED
    _TUI_RESIZED = True


def _sysname():
    if sys.platform.startswith("win"):
        return "Windows"
    if sys.platform == "darwin":
        return "Darwin"
    if sys.platform.startswith("linux"):
        return "Linux"
    return sys.platform


def _terminal_size(fallback=(100, 32)):
    try:
        env_columns = int(os.environ.get("COLUMNS") or 0)
    except ValueError:
        env_columns = 0
    try:
        env_lines = int(os.environ.get("LINES") or 0)
    except ValueError:
        env_lines = 0
    try:
        size = os.get_terminal_size()
    except OSError:
        size = os.terminal_size((0, 0))
    columns = env_columns or size.columns or fallback[0]
    lines = env_lines or size.lines or fallback[1]
    return os.terminal_size((columns, lines))


def _which(exe):
    if os.path.dirname(exe):
        return exe if os.path.isfile(exe) and os.access(exe, os.X_OK) else None
    exts = [""]
    if _sysname() == "Windows":
        exts = os.environ.get("PATHEXT", ".EXE;.BAT;.CMD").split(os.pathsep)
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for ext in exts:
            candidate = os.path.join(directory, exe + ext)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def _copy_file(src, dst):
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            chunk = fsrc.read(1024 * 1024)
            if not chunk:
                break
            fdst.write(chunk)
    try:
        st = os.stat(src)
        os.chmod(dst, st.st_mode & 0o7777)
        os.utime(dst, (st.st_atime, st.st_mtime))
    except OSError:
        pass


def _timestamp_filename():
    ns = time.time_ns()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ns // 1_000_000_000))
    return f"{stamp}-{(ns // 1000) % 1_000_000:06d}"


def _timestamp_seconds():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _windows_quote_arg(arg):
    arg = str(arg)
    if arg == "":
        return '""'
    if not any(c in arg for c in ' \t"'):
        return arg
    out = ['"']
    slashes = 0
    for c in arg:
        if c == "\\":
            slashes += 1
        elif c == '"':
            out.append("\\" * (slashes * 2 + 1))
            out.append(c)
            slashes = 0
        else:
            out.append("\\" * slashes)
            out.append(c)
            slashes = 0
    out.append("\\" * (slashes * 2))
    out.append('"')
    return "".join(out)


def _windows_cmdline(args):
    return " ".join(_windows_quote_arg(arg) for arg in args)

# ----------------------------------------------------------------------------
# Binary discovery (Linux / macOS / Windows)
# ----------------------------------------------------------------------------

def candidate_binaries():
    sysname = _sysname()
    home = str(Path.home())
    cands = []
    if sysname == "Linux":
        cands += [
            os.environ.get("RESOLVE_DIR", "") and os.path.join(os.environ["RESOLVE_DIR"], "bin", "resolve"),
            "/opt/resolve/bin/resolve",
            "/opt/resolve/bin/resolve-studio",
            os.path.join(home, ".local", "share", "resolve", "bin", "resolve"),
        ]
    elif sysname == "Darwin":
        for apps in ["/Applications", os.path.join(home, "Applications")]:
            for name in ["DaVinci Resolve", "DaVinci Resolve Studio"]:
                cands.append(f"{apps}/{name}.app/Contents/MacOS/Resolve")
    elif sysname == "Windows":
        for drv in ("C:", "D:"):
            for pf in ("Program Files", "Program Files\\Blackmagic Design"):
                for name in ("DaVinci Resolve", "DaVinci Resolve Studio"):
                    cands.append(f"{drv}\\{pf}\\{name}\\resolve.exe")
    # Accept a real Resolve binary found on PATH.
    which = _which("resolve")
    if which:
        cands.append(which)
    cands.append(os.environ.get("DAVINCI_RESOLVE_BINARY", ""))
    # Drop empty values and preserve the first occurrence of each path.
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def is_likely_resolve_binary(path):
    try:
        if not os.path.isfile(path):
            return False
        if os.path.getsize(path) < 80 * 1024 * 1024:  # Resolve binary is hundreds of MB
            return False
        with open(path, "rb") as f:
            head = f.read(8)
        # ELF, Mach-O, and PE executable signatures.
        return head[:4] in (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe") or head[:2] == b"MZ"
    except OSError:
        return False


def find_binary(override):
    if override:
        if not is_likely_resolve_binary(override):
            die(f"--binary {override!r} is not a recognizable Resolve binary.")
        return override
    for c in candidate_binaries():
        if is_likely_resolve_binary(c):
            return c
    die("Could not locate the DaVinci Resolve binary. Pass it with --binary PATH.\n"
        "Tried:\n  " + "\n  ".join(c for c in candidate_binaries() if c))


# ----------------------------------------------------------------------------
# Locating the compressed chart XML streams (no hard-coded offsets)
# ----------------------------------------------------------------------------

ZLIB_HEAD = re.compile(rb"\x78[\x01\x9c\xda\x5e]")
CHART_TAG_RE = re.compile(rb'<colorchart\s+name="([^"]*)"[^>]*>', re.IGNORECASE)
TYPE_RE = re.compile(rb'\btype="(\d+)"', re.IGNORECASE)
GRID_RE = re.compile(
    rb'<grid\b[^>]*\bindex="(\d+)"[^>]*>(.*?)</grid>', re.IGNORECASE | re.DOTALL)
GRID_BLOCK_RE = re.compile(rb'<grid\b([^>]*)>(.*?)</grid>', re.IGNORECASE | re.DOTALL)
ATTR_RE = re.compile(rb'([A-Za-z0-9_:-]+)="([^"]*)"')
XYZ_RE = re.compile(rb'xyz="([^"]*)"')
RGB_RE = re.compile(rb'rgb="([^"]*)"')
MOUSE_RE = re.compile(rb'\[<(\d+);(\d+);(\d+)([mM])')
ATTR_TEXT_RE = re.compile(r"([A-Za-z0-9_:-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
COLORCHART_TEXT_RE = re.compile(r"<colorchart\b([^>]*)>", re.IGNORECASE)
RGB_COLOR_BLOCK_RE = re.compile(r"<color\b([^>]*)>(.*?)</color>", re.IGNORECASE | re.DOTALL)
FORMAT_ID = "davinci-colorchecker-values/v1"
ALIEXPRESS_TABLE_NAME = "Unnamed Aliexpress 8,5 x 5.8 inches Table"
DEFAULT_TABLE_DIR = Path.home() / ".local/share/colorchecker_patch/tables"
TYPE8_DEFAULT_LABEL = "AliExpress 8.5x5.8"
TYPE8_DEFAULT_XML_NAME = "aliexpress-type8.xml"
TYPE9_DEFAULT_LABEL = "AliExpress Chart 2026"
TYPE9_DEFAULT_XML_NAME = "aliexpress-chart-2026-type9.xml"


def _xml_text(data):
    if isinstance(data, bytes):
        return data.decode("utf-8-sig", "replace")
    return str(data)


def _xml_unescape(text):
    return (text.replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"')
                .replace("&apos;", "'")
                .replace("&amp;", "&"))


def _xml_attrs(attr_text):
    attrs = {}
    for m in ATTR_TEXT_RE.finditer(attr_text or ""):
        value = m.group(2) if m.group(2) is not None else m.group(3)
        attrs[m.group(1).lower()] = _xml_unescape(value or "")
    return attrs


def _colorchart_attrs(data):
    m = COLORCHART_TEXT_RE.search(_xml_text(data))
    return _xml_attrs(m.group(1)) if m else {}


def _xml_child_text(block, tag):
    pattern = r"<%s\b[^>]*>(.*?)</%s>" % (re.escape(tag), re.escape(tag))
    m = re.search(pattern, block, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return _xml_unescape(re.sub(r"<[^>]*>", "", m.group(1)).strip())


def _rgb_table_entries(data):
    entries = {}
    for m in RGB_COLOR_BLOCK_RE.finditer(_xml_text(data)):
        no = _xml_attrs(m.group(1)).get("no")
        if not no:
            continue
        try:
            idx = int(no) - 1
            rgb = tuple(int(_xml_child_text(m.group(2), tag)) for tag in ("R", "G", "B"))
        except Exception as e:
            raise ValueError(f"bad RGB entry no={no!r}: {e}")
        if 0 <= idx <= 23:
            entries[idx] = rgb
    return entries


STANDARD_PATCH_NAMES = [
    "Dark Skin", "Light Skin", "Blue Sky", "Foliage", "Blue Flower", "Bluish Green",
    "Orange", "Purplish Blue", "Moderate Red", "Purple", "Yellow Green", "Orange Yellow",
    "Blue", "Green", "Red", "Yellow", "Magenta", "Cyan",
    "White", "Neutral(8)", "Neutral(6.5)", "Neutral(5)", "Neutral(3.5)", "Black",
]

ALIEXPRESS_TYPE8_RGB = [
    (115, 82, 69), (204, 161, 141), (101, 134, 179), (89, 109, 61),
    (141, 137, 194), (132, 228, 208), (249, 118, 35), (80, 91, 182),
    (222, 91, 125), (91, 63, 123), (173, 232, 91), (255, 164, 26),
    (44, 56, 142), (74, 148, 81), (179, 42, 50), (250, 226, 21),
    (191, 81, 160), (6, 142, 172), (252, 252, 252), (230, 230, 230),
    (200, 200, 200), (143, 143, 142), (100, 100, 100), (50, 50, 50),
]

BUILTIN_RGB_TABLES = {
    TYPE8_DEFAULT_LABEL: ALIEXPRESS_TYPE8_RGB,
    ALIEXPRESS_TABLE_NAME: ALIEXPRESS_TYPE8_RGB,
}

BUILTIN_XYZ_TABLES = {
    TYPE8_DEFAULT_LABEL: [
        (0.111621, 0.101098, 0.069925),
        (0.424553, 0.402522, 0.307273),
        (0.220260, 0.230703, 0.459315),
        (0.104307, 0.133979, 0.064505),
        (0.296653, 0.274485, 0.547636),
        (0.486398, 0.649428, 0.696344),
        (0.458535, 0.332240, 0.055881),
        (0.154901, 0.125640, 0.458557),
        (0.375695, 0.244967, 0.221479),
        (0.096663, 0.072093, 0.196173),
        (0.479783, 0.673517, 0.203679),
        (0.547066, 0.478910, 0.073399),
        (0.073337, 0.053161, 0.262256),
        (0.148983, 0.232286, 0.114815),
        (0.199964, 0.114730, 0.041786),
        (0.667596, 0.747742, 0.116257),
        (0.307740, 0.195018, 0.353943),
        (0.171913, 0.223610, 0.424317),
        (0.925231, 0.973445, 1.059916),
        (0.752105, 0.791298, 0.861589),
        (0.548973, 0.577580, 0.628887),
        (0.260318, 0.274376, 0.295105),
        (0.121126, 0.127438, 0.138758),
        (0.030316, 0.031896, 0.034729),
    ],
    TYPE9_DEFAULT_LABEL: [
        (0.110519, 0.095920, 0.048186),
        (0.370222, 0.327771, 0.178810),
        (0.164482, 0.177733, 0.251924),
        (0.110334, 0.133595, 0.052500),
        (0.240166, 0.227366, 0.323262),
        (0.307821, 0.418393, 0.339410),
        (0.400607, 0.305783, 0.051345),
        (0.117357, 0.105835, 0.282947),
        (0.284820, 0.184187, 0.095205),
        (0.085369, 0.065333, 0.106978),
        (0.339685, 0.427134, 0.082919),
        (0.485240, 0.436441, 0.064284),
        (0.067738, 0.055849, 0.208948),
        (0.139057, 0.219181, 0.373123),
        (0.211068, 0.125972, 0.038901),
        (0.586280, 0.597461, 0.074643),
        (0.296974, 0.188830, 0.220926),
        (0.128524, 0.186035, 0.292940),
        (0.840575, 0.878079, 0.698584),
        (0.572866, 0.596544, 0.491836),
        (0.352583, 0.367911, 0.305195),
        (0.183382, 0.190366, 0.156233),
        (0.086078, 0.090030, 0.075242),
        (0.031513, 0.032768, 0.026649),
    ],
}

BUILTIN_TABLE_BASE_TYPES = {
    ALIEXPRESS_TABLE_NAME: "7",
    TYPE8_DEFAULT_LABEL: "7",
    TYPE9_DEFAULT_LABEL: "7",
}

PUBLIC_BUILTIN_TABLES = [TYPE8_DEFAULT_LABEL, TYPE9_DEFAULT_LABEL]

BUILTIN_ALIASES = {
    "aliexpress": TYPE8_DEFAULT_LABEL,
    "ali": TYPE8_DEFAULT_LABEL,
    "8.5x5.8": TYPE8_DEFAULT_LABEL,
    "8,5x5.8": TYPE8_DEFAULT_LABEL,
    ALIEXPRESS_TABLE_NAME.lower(): ALIEXPRESS_TABLE_NAME,
    "ali8": TYPE8_DEFAULT_LABEL,
    "aliexpress8": TYPE8_DEFAULT_LABEL,
    "type8": TYPE8_DEFAULT_LABEL,
    "type-8": TYPE8_DEFAULT_LABEL,
    "ali2026": TYPE9_DEFAULT_LABEL,
    "aliexpress2026": TYPE9_DEFAULT_LABEL,
    "type9": TYPE9_DEFAULT_LABEL,
    "type-9": TYPE9_DEFAULT_LABEL,
    "2026": TYPE9_DEFAULT_LABEL,
}

CHART_ALIASES = {
    # Resolve UI labels differ from the embedded XML chart names.
    "legacy": {"type": "1", "label": "Calibrite ColorChecker Classic - Legacy"},
    "classic-legacy": {"type": "1", "label": "Calibrite ColorChecker Classic - Legacy"},
    "calibrite colorchecker classic - legacy": {"type": "1", "label": "Calibrite ColorChecker Classic - Legacy"},
    "x-rite colorchecker pre november 2014": {"type": "1", "label": "Calibrite ColorChecker Classic - Legacy"},
    "pre november 2014": {"type": "1", "label": "Calibrite ColorChecker Classic - Legacy"},

    # Current classic ColorChecker slot.
    "classic": {"type": "7", "label": "Calibrite ColorChecker Classic"},
    "current": {"type": "7", "label": "Calibrite ColorChecker Classic"},
    "calibrite colorchecker classic": {"type": "7", "label": "Calibrite ColorChecker Classic"},
    "x-rite colorchecker": {"type": "7", "label": "Calibrite ColorChecker Classic"},

    "spyder": {"type": "2", "label": "Datacolor SpyderCheckr 24"},
    "spydercheckr": {"type": "2", "label": "Datacolor SpyderCheckr 24"},
    "datacolor": {"type": "2", "label": "Datacolor SpyderCheckr 24"},
    "smpte": {"type": "3", "label": "DSC Labs SMPTE OneShot"},
    "oneshot": {"type": "3", "label": "DSC Labs SMPTE OneShot"},
    "chroma": {"type": "4", "label": "DSC Labs ChromaDuMonde 24+4"},
    "chromadumonde": {"type": "4", "label": "DSC Labs ChromaDuMonde 24+4"},
    "video": {"type": "5", "label": "Calibrite ColorChecker Video"},
    "passport": {"type": "6", "label": "Calibrite ColorChecker Passport Video"},
    "video-passport": {"type": "6", "label": "Calibrite ColorChecker Passport Video"},
}

CHART_UI_LABELS = {
    "1": "Calibrite ColorChecker Classic - Legacy",
    "2": "Datacolor SpyderCheckr 24",
    "3": "DSC Labs SMPTE OneShot",
    "4": "DSC Labs ChromaDuMonde 24+4",
    "5": "Calibrite ColorChecker Video",
    "6": "Calibrite ColorChecker Passport Video",
    "7": "Calibrite ColorChecker Classic",
}

CHART_UI_LABEL_ORDER = [
    ("2", "Datacolor SpyderCheckr 24"),
    ("4", "DSC Labs ChromaDuMonde 24+4"),
    ("3", "DSC Labs SMPTE OneShot"),
    ("7", "Calibrite ColorChecker Classic"),
    ("1", "Calibrite ColorChecker Classic - Legacy"),
    ("5", "Calibrite ColorChecker Video"),
    ("6", "Calibrite ColorChecker Passport Video"),
]

# Extra dropdown support targets the Resolve Color Match binary layout. Every
# write validates the expected bytes before touching the executable.
TYPE8_IMAGE_BASE = 0x400000
TYPE8_DROPDOWN_HOOK = 0x16DD6DF
TYPE8_UI_ALIAS_HOOK = 0x16D8857
TYPE8_MATCH_GETTER = 0x16D8942
TYPE8_DROPDOWN_CAVE = 0x9A9C2A0
TYPE8_UI_ALIAS_CAVE = 0x9A9C340
TYPE8_LABEL_CAVE = 0x9A9C420
TYPE8_PATH_TABLE_CAVE = 0x9A9C480
TYPE8_XML_PATH_CAVE = 0x9A9C500
TYPE8_PROP_CAVE_A = 0x9A9C580
TYPE8_PROP_CAVE_B = 0x9A9C5C0
TYPE9_DROPDOWN_CAVE = 0x9A9C600
TYPE9_LABEL_CAVE = 0x9A9C700
TYPE9_XML_PATH_CAVE = 0x9A9C760
TYPE8_MATCHER_TYPE_STORE_HOOK = 0x54744A9
TYPE8_MATCHER_TYPE_ALIAS_CAVE = 0x9A9C800
TYPE8_LABEL_CAPACITY = 0x60
TYPE8_XML_PATH_CAPACITY = 0x80
TYPE9_LABEL_CAPACITY = 0x60
TYPE9_XML_PATH_CAPACITY = 0xA0

TYPE8_STOCK_PATH_PTRS = [
    0x09D5338A, 0x0A370F8A, 0x0A370FA4, 0x0A370FBE, 0x0A370FD8,
    0x0A370FF7, 0x0A371016, 0x0A371037,
]


class Chart:
    def __init__(self, off, comp_len, unc_len, name, ctype, xml, comp_stored):
        self.off = off              # file offset of the zlib stream start
        self.comp_len = comp_len    # actual zlib stream byte length
        self.unc_len = unc_len      # decompressed byte length
        self.comp_stored = comp_stored  # stored slot length (>= comp_len), what Qt reads
        self.name = name
        self.ctype = ctype
        self.xml = xml


def find_charts(data):
    """Yield Chart objects for every zlib stream that decompresses to a <colorchart>."""
    found = []
    for m in ZLIB_HEAD.finditer(data):
        off = m.start()
        d = zlib.decompressobj()
        try:
            chunk = data[off:off + 65536]
            out = d.decompress(chunk, 65536)
            out += d.flush()
        except Exception:
            continue
        if b"<colorchart" not in out[:4096]:
            continue
        # full decompress (stream is small)
        d2 = zlib.decompressobj()
        try:
            xml = d2.decompress(data[off:off + (1 << 20)]) + d2.flush()
        except Exception:
            continue
        comp_len = (len(data[off:off + (1 << 20)]) - len(d2.unused_data))
        nm = CHART_TAG_RE.search(xml)
        tp = TYPE_RE.search(xml)
        name = nm.group(1).decode("utf-8", "replace") if nm else "?"
        ctype = tp.group(1).decode() if tp else "?"
        # Qt resource entries usually store compressed and uncompressed sizes
        # immediately before the zlib stream.
        comp_stored = comp_len
        if off >= 8:
            be = data[off - 8:off]
            try:
                import struct
                c1, c2 = struct.unpack(">II", be)
                if c2 == len(xml) and comp_len <= c1 <= comp_len + 64:
                    comp_stored = c1
            except Exception:
                pass
        found.append(Chart(off, comp_len, len(xml), name, ctype, xml, comp_stored))
    # Multiple zlib header patterns can point at the same stream.
    uniq, seen = [], set()
    for c in found:
        if c.off not in seen:
            seen.add(c.off)
            uniq.append(c)
    return uniq


def select_chart(charts, chart_filter):
    if chart_filter is None:
        # Default to current classic, not the legacy pre-November-2014 chart.
        matches = [c for c in charts
                   if "colorchecker" in c.name.lower()
                   and "pre november 2014" not in c.name.lower()
                   and "video" not in c.name.lower()
                   and "passport" not in c.name.lower()]
        if not matches:
            matches = [c for c in charts if chart_filter_default_substr(c)]
        label = "default (current X-Rite ColorChecker)"
    else:
        key = chart_filter.lower().strip()
        alias = CHART_ALIASES.get(key)
        if alias:
            matches = [c for c in charts if c.ctype == alias["type"]]
            label = f"chart alias {chart_filter!r} ({alias['label']})"
        else:
            matches = [c for c in charts if key in c.name.lower()]
            label = f"chart name containing {chart_filter!r}"
    if not matches:
        die(f"No chart matched {label}.\nCharts present:\n"
            + "\n".join(f"  - {c.name!r} (type={c.ctype}) @ {c.off}" for c in charts))
    if len(matches) > 1:
        die(f"Ambiguous match for {label}; refine with --base/--chart:\n"
            + "\n".join(f"  - {c.name!r} (type={c.ctype}) @ {c.off}" for c in matches))
    return matches[0]


def resolve_slot_type(slot):
    key = str(slot or "").strip().lower()
    if not key:
        die("missing slot name")
    if key in CHART_UI_LABELS:
        return key
    alias = CHART_ALIASES.get(key)
    if alias:
        return alias["type"]
    matches = []
    for ctype, label in CHART_UI_LABELS.items():
        low = label.lower()
        if key == low or key in low:
            matches.append((ctype, label))
    if len(matches) == 1:
        return matches[0][0]
    if len(matches) > 1:
        die("Ambiguous slot %r; matches: %s" % (slot, ", ".join(label for _ctype, label in matches)))
    die("Unknown slot %r. Use --list-slots." % slot)


def select_chart_by_type(charts, ctype):
    matches = [c for c in charts if c.ctype == str(ctype)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        die(f"No embedded chart stream found for slot type {ctype}.")
    die(f"Multiple embedded chart streams found for slot type {ctype}; refusing ambiguous restore.")


def chart_filter_default_substr(c):
    return "colorchecker" in c.name.lower()


# ----------------------------------------------------------------------------
# Editing
# ----------------------------------------------------------------------------

def parse_edits(args):
    """Return dict {int index: (x, y, z)} from --json/--csv/--xml/--set."""
    edits = {}
    if args.table:
        try:
            edits.update(_edits_from_table(args.table, args.table_dir))
        except Exception as e:
            die(str(e))
    if args.preset:
        try:
            edits.update(_edits_from_builtin(args.preset))
        except Exception as e:
            die(str(e))
    if args.json:
        edits.update(_edits_from_json_path(args.json))
    if args.csv:
        edits.update(_edits_from_csv_path(args.csv))
    if args.xml:
        edits.update(_edits_from_xml_path(args.xml))
    for s in (args.set or []):
        if "=" not in s:
            die(f"--set expects N=x,y,z ; got {s!r}")
        k, v = s.split("=", 1)
        edits[int(k)] = _xyz([float(t) for t in v.split(",")])
    # validate
    for idx, (x, y, z) in edits.items():
        if not 0 <= idx <= 23:
            die(f"patch index {idx} out of range (0-23).")
        for val in (x, y, z):
            if not 0.0 <= val <= 2.0:
                warn(f"xyz value {val} at index {idx} is outside the usual [0,1] range "
                     f"(CIEXYZ) - proceeding anyway.")
    return edits


def _xyz(lst):
    if len(lst) < 3:
        die(f"need 3 components for xyz, got {lst}")
    return (float(lst[0]), float(lst[1]), float(lst[2]))


def _xyz_or_error(lst):
    if len(lst) != 3:
        raise ValueError(f"need exactly 3 XYZ components, got {lst}")
    return (float(lst[0]), float(lst[1]), float(lst[2]))


def _srgb8_to_xyz(rgb):
    """Convert gamma-encoded sRGB 8-bit values to normalized CIEXYZ (D65)."""
    def dec(v):
        c = max(0.0, min(1.0, float(v) / 255.0))
        if c <= 0.04045:
            return c / 12.92
        return ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (dec(v) for v in rgb)
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    return (x, y, z)


def list_builtin_presets():
    return list(PUBLIC_BUILTIN_TABLES)


def _resolve_builtin_name(name):
    key = (name or "").strip()
    if not key:
        raise ValueError("missing preset name")
    lower = key.lower()
    if lower in BUILTIN_ALIASES:
        return BUILTIN_ALIASES[lower]
    names = list_builtin_presets()
    exact = [n for n in names if n.lower() == lower]
    if len(exact) == 1:
        return exact[0]
    partial = [n for n in names if lower in n.lower()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise ValueError("unknown preset %r; available: %s" % (name, ", ".join(list_builtin_presets())))
    raise ValueError("ambiguous preset %r; matches: %s" % (name, ", ".join(partial)))


def _edits_from_builtin(name):
    resolved = _resolve_builtin_name(name)
    if resolved in BUILTIN_XYZ_TABLES:
        xyz_values = BUILTIN_XYZ_TABLES[resolved]
        if len(xyz_values) != 24:
            raise ValueError(f"built-in table {resolved!r} has {len(xyz_values)} entries, expected 24")
        return {idx: tuple(float(v) for v in xyz) for idx, xyz in enumerate(xyz_values)}
    rgb_values = BUILTIN_RGB_TABLES[resolved]
    if len(rgb_values) != 24:
        raise ValueError(f"built-in table {resolved!r} has {len(rgb_values)} entries, expected 24")
    return {idx: _srgb8_to_xyz(rgb) for idx, rgb in enumerate(rgb_values)}


def _preferred_base_type_for_table_name(name):
    try:
        resolved = _resolve_builtin_name(name)
    except Exception:
        return None
    return BUILTIN_TABLE_BASE_TYPES.get(resolved)


def _preferred_base_type_for_table_row(row):
    if not row:
        return None
    name, kind, _path = row
    if kind != "built-in":
        return None
    return _preferred_base_type_for_table_name(name)


def _preferred_base_type_from_args(args):
    if args.chart or args.install_table_slot:
        return None
    if args.table and not _looks_like_table_path(args.table):
        preferred = _preferred_base_type_for_table_name(args.table)
        if preferred:
            return preferred
    if args.preset:
        return _preferred_base_type_for_table_name(args.preset)
    return None


def _table_dir(path=None):
    return Path(path or os.environ.get("COLORCHECKER_TABLE_DIR") or DEFAULT_TABLE_DIR)


def _slugify_table_name(name):
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip("-._")
    return slug or "table"


def _edits_from_csv_path(path):
    edits = {}
    try:
        with open(path, newline="") as f:
            for row in csv.reader(f):
                if not row or row[0].strip().lower().startswith(("index", "#")):
                    continue
                idx = int(row[0])
                edits[idx] = _xyz([float(x) for x in row[1:4]])
    except OSError as e:
        die(f"Could not read CSV {path!r}: {e}")
    if not edits:
        die(f"CSV {path!r} did not contain any patch rows")
    return edits


def _edits_from_any_file(path):
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        return _edits_from_json_path(path)
    if suffix in (".xml", ".chart"):
        return _edits_from_xml_path(path)
    if suffix == ".csv":
        return _edits_from_csv_path(path)

    errors = []
    for label, loader in (("JSON", _edits_from_json_path), ("XML", _edits_from_xml_path), ("CSV", _edits_from_csv_path)):
        try:
            return loader(path)
        except SystemExit:
            raise
        except Exception as e:
            errors.append(f"{label}: {e}")
    raise ValueError("could not parse table file %r (%s)" % (path, "; ".join(errors)))


def _table_payload_from_edits(name, edits, source=None):
    if len(edits) != 24:
        raise ValueError(f"table {name!r} has {len(edits)} patch values; expected 24")
    patches = []
    for idx in range(24):
        if idx not in edits:
            raise ValueError(f"table {name!r} missing patch index {idx}")
        xyz = edits[idx]
        patches.append({
            "index": idx,
            "name": STANDARD_PATCH_NAMES[idx],
            "xyz": [round(float(v), 6) for v in xyz],
            "preview_srgb": list(_xyz_to_srgb8(xyz)),
        })
    payload = {
        "format": FORMAT_ID,
        "kind": "table",
        "name": name,
        "encoding": "CIEXYZ",
        "patch_count": 24,
        "patches": patches,
    }
    if source:
        payload["source"] = source
    return payload


def add_user_table(source_path, table_name=None, table_dir=None):
    edits = _edits_from_any_file(source_path)
    name = table_name or Path(source_path).stem
    payload = _table_payload_from_edits(name, edits, source=str(source_path))
    out_dir = _table_dir(table_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (_slugify_table_name(name) + ".json")
    n = 1
    while out.exists():
        out = out_dir / (_slugify_table_name(name) + f"-{n}.json")
        n += 1
    out.write_text(json.dumps(payload, indent=2) + "\n")
    return out, name


def list_user_tables(table_dir=None):
    out = []
    base = _table_dir(table_dir)
    if not base.exists():
        return out
    for p in sorted(base.glob("*.json")):
        try:
            obj = json.loads(p.read_text())
            name = obj.get("name") or obj.get("chart") or p.stem
        except Exception:
            name = p.stem
        out.append((name, p))
    return out


def list_extra_tables():
    rows = []
    for default_name, path in (
        (TYPE8_DEFAULT_LABEL, _default_type8_xml_path()),
        (TYPE9_DEFAULT_LABEL, _default_type9_xml_path()),
    ):
        if not path.exists():
            continue
        name = default_name
        try:
            name = _colorchart_attrs(path.read_bytes()).get("name") or default_name
        except Exception:
            pass
        rows.append((name, path))
    return rows


def list_all_tables(table_dir=None):
    rows = [(name, "built-in", None) for name in list_builtin_presets()]
    builtin_names = {name.lower() for name in list_builtin_presets()}
    rows += [(name, "extra", path) for name, path in list_extra_tables() if name.lower() not in builtin_names]
    rows += [(name, "user", path) for name, path in list_user_tables(table_dir)]
    return rows


def _edits_from_table(name, table_dir=None):
    source = Path(os.path.expandvars(os.path.expanduser(str(name))))
    if source.is_file():
        return _edits_from_any_file(source)
    if _looks_like_table_path(name):
        raise ValueError(f"table file not found: {source}")

    try:
        return _edits_from_builtin(name)
    except Exception as builtin_error:
        builtin_msg = str(builtin_error)

    lower = name.lower()
    extra_matches = []
    for table_name, path in list_extra_tables():
        if table_name.lower() == lower or path.stem.lower() == lower:
            extra_matches = [(table_name, path)]
            break
        if lower in table_name.lower() or lower in path.stem.lower():
            extra_matches.append((table_name, path))
    if len(extra_matches) == 1:
        return _edits_from_xml_bytes(extra_matches[0][1].read_bytes())
    if len(extra_matches) > 1:
        raise ValueError("ambiguous table %r; matches: %s" % (name, ", ".join(n for n, _p in extra_matches)))

    candidates = []
    for table_name, path in list_user_tables(table_dir):
        if table_name.lower() == lower or path.stem.lower() == lower:
            candidates = [(table_name, path)]
            break
        if lower in table_name.lower() or lower in path.stem.lower():
            candidates.append((table_name, path))
    if len(candidates) == 1:
        return _edits_from_json_obj(json.loads(candidates[0][1].read_text()))
    if len(candidates) > 1:
        raise ValueError("ambiguous table %r; matches: %s" % (name, ", ".join(n for n, _p in candidates)))
    raise ValueError("unknown table %r; built-ins/extra/user tables not found (%s)" % (name, builtin_msg))


def _looks_like_table_path(name):
    text = str(name or "")
    if any(sep in text for sep in ("/", "\\")):
        return True
    return Path(text).suffix.lower() in (".json", ".xml", ".chart", ".csv")


def _find_user_table(name, table_dir=None):
    lower = name.lower()
    matches = []
    for table_name, path in list_user_tables(table_dir):
        if table_name.lower() == lower or path.stem.lower() == lower:
            return table_name, path
        if lower in table_name.lower() or lower in path.stem.lower():
            matches.append((table_name, path))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError("ambiguous table %r; matches: %s" % (name, ", ".join(n for n, _p in matches)))
    raise ValueError("user table %r not found" % name)


def rename_user_table(old_name, new_name, table_dir=None):
    table_name, path = _find_user_table(old_name, table_dir)
    obj = json.loads(path.read_text())
    obj["name"] = new_name
    out_dir = _table_dir(table_dir)
    out = out_dir / (_slugify_table_name(new_name) + ".json")
    n = 1
    while out.exists() and out.resolve() != path.resolve():
        out = out_dir / (_slugify_table_name(new_name) + f"-{n}.json")
        n += 1
    out.write_text(json.dumps(obj, indent=2) + "\n")
    if out.resolve() != path.resolve():
        path.unlink()
    return table_name, out


def remove_user_table(name, table_dir=None):
    table_name, path = _find_user_table(name, table_dir)
    path.unlink()
    return table_name, path


def _edits_from_xml_path(path):
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        die(f"Could not read XML {path!r}: {e}")
    try:
        return _edits_from_xml_bytes(data)
    except Exception as e:
        die(f"Could not parse XML {path!r}: {e}")


def _xml_payload_bytes(data):
    # Accept raw XML or a zlib-compressed XML blob copied from Resolve.
    if data[:1] != b"<":
        try:
            return zlib.decompress(data)
        except zlib.error:
            pass
    return data


def _edits_from_xml_bytes(data):
    data = _xml_payload_bytes(data)
    vals = _extract_values(data)
    if not vals:
        return _edits_from_rgb_table_xml_bytes(data)
    edits = {}
    for idx, xyz in vals.items():
        if not 0 <= idx <= 23:
            continue
        edits[idx] = _parse_xyz_string(xyz)
    if not edits:
        raise ValueError("no usable ColorChecker patch indices (0-23) found")
    return edits


def _rgb_metadata_from_args(args):
    rgbs = {}
    if args.table:
        rgbs.update(_rgb_metadata_from_named_table(args.table))
    if args.preset:
        rgbs.update(_rgb_metadata_from_named_table(args.preset))
    if args.xml:
        try:
            rgbs.update(_rgb_metadata_from_xml_path(args.xml))
        except Exception as e:
            warn(f"Could not import RGB preview metadata from {args.xml!r}: {e}")
    return rgbs


def _rgb_metadata_from_named_table(name):
    try:
        resolved = _resolve_builtin_name(name)
    except Exception:
        return {}
    if resolved not in BUILTIN_RGB_TABLES:
        return {}
    return {
        idx: _fmt_rgb(rgb)
        for idx, rgb in enumerate(BUILTIN_RGB_TABLES[resolved])
    }


def _rgb_metadata_from_xml_path(path):
    data = _xml_payload_bytes(Path(path).read_bytes())
    rgbs = _extract_rgb_values(data)
    if rgbs:
        return rgbs
    return _rgb_metadata_from_rgb_table_xml_bytes(data)


def _rgb_metadata_from_rgb_table_xml_bytes(data):
    return {idx: _fmt_rgb(rgb) for idx, rgb in _rgb_table_entries(data).items()}


def _edits_from_rgb_table_xml_bytes(data):
    """Read XML shaped like <RGB...><color no="001"><R>...</R>...</color>."""
    try:
        rgbs = _rgb_table_entries(data)
    except Exception as e:
        raise ValueError(f"no Resolve grid entries and RGB table parse failed: {e}")

    edits = {}
    for idx, rgb in rgbs.items():
        edits[idx] = _srgb8_to_xyz(rgb)

    if not edits:
        raise ValueError("no RGB table <color no=...><R/G/B> entries found")
    if len(edits) != 24:
        raise ValueError(f"RGB table has {len(edits)} usable entries; expected 24")
    return edits


def _edits_from_json_path(path):
    try:
        obj = json.loads(Path(path).read_text())
    except OSError as e:
        die(f"Could not read JSON {path!r}: {e}")
    except json.JSONDecodeError as e:
        die(f"Could not parse JSON {path!r}: {e}")
    try:
        return _edits_from_json_obj(obj)
    except Exception as e:
        die(f"Invalid JSON patch format in {path!r}: {e}")


def _edits_from_json_obj(obj):
    """Accept canonical JSON plus simple mapping/list forms."""
    if isinstance(obj, dict) and "patches" in obj:
        return _edits_from_json_values(obj["patches"])
    if isinstance(obj, dict) and "values" in obj:
        return _edits_from_json_values(obj["values"])
    return _edits_from_json_values(obj)


def _edits_from_json_values(values):
    edits = {}
    if isinstance(values, list):
        for pos, entry in enumerate(values):
            if entry is None:
                continue
            idx, xyz = _parse_json_patch_entry(pos, entry)
            edits[idx] = xyz
    elif isinstance(values, dict):
        for key, entry in values.items():
            idx_hint = int(key) if str(key).isdigit() else None
            idx, xyz = _parse_json_patch_entry(idx_hint, entry)
            edits[idx] = xyz
    else:
        raise ValueError("expected a dict or list of patch values")
    if not edits:
        raise ValueError("no patch values found")
    for idx in edits:
        if not 0 <= idx <= 23:
            raise ValueError(f"patch index {idx} out of range (0-23)")
    return edits


def _parse_json_patch_entry(idx_hint, entry):
    if isinstance(entry, dict):
        if "index" in entry:
            idx = int(entry["index"])
        elif idx_hint is not None:
            idx = idx_hint
        else:
            raise ValueError(f"patch entry missing index: {entry!r}")
        if "xyz" in entry:
            xyz = entry["xyz"]
        elif all(k in entry for k in ("x", "y", "z")):
            xyz = [entry["x"], entry["y"], entry["z"]]
        elif "value" in entry:
            xyz = entry["value"]
        else:
            raise ValueError(f"patch {idx} missing xyz")
        return idx, _xyz_or_error(xyz)
    if idx_hint is None:
        raise ValueError(f"simple patch value needs an index: {entry!r}")
    return idx_hint, _xyz_or_error(entry)


def apply_edits(xml_bytes, edits):
    """Return (new_xml_bytes, changed_count). Only xyz attributes are rewritten,
    formatted to 6 decimals. Everything else is byte-preserved."""
    new_xml, changed = _replace_xyz_values(xml_bytes, edits, 6)
    if changed == 0:
        die("No selected grid values differ from the current chart. Nothing to change.")
    return new_xml, changed


def _replace_xyz_values(xml_bytes, edits, decimals):
    """Rewrite selected grid xyz values while preserving the grid wrapper."""
    changed = 0

    def repl_grid(m):
        nonlocal changed
        idx = int(m.group(1))
        if idx not in edits:
            return m.group(0)
        x, y, z = edits[idx]
        fmt = "%%.%df, %%.%df, %%.%df" % (decimals, decimals, decimals)
        new_xyz = fmt % (x, y, z)

        def repl_xyz(mm):
            nonlocal changed
            if mm.group(1).decode("ascii", "replace") == new_xyz:
                return mm.group(0)
            changed += 1
            return b'xyz="' + new_xyz.encode("ascii") + b'"'

        body = XYZ_RE.sub(repl_xyz, m.group(2))
        return m.group(0)[:m.start(2) - m.start(0)] + body + m.group(0)[m.end(2) - m.start(0):]

    new_xml = GRID_RE.sub(repl_grid, xml_bytes)
    return new_xml, changed


def _replace_rgb_values(xml_bytes, rgbs):
    """Rewrite selected grid rgb metadata while preserving everything else."""
    rgbs = rgbs or {}
    changed = 0

    def repl_grid(m):
        nonlocal changed
        idx = int(m.group(1))
        if idx not in rgbs:
            return m.group(0)
        new_rgb = _fmt_rgb(rgbs[idx])

        def repl_rgb(mm):
            nonlocal changed
            if mm.group(1).decode("ascii", "replace") == new_rgb:
                return mm.group(0)
            changed += 1
            return b'rgb="' + new_rgb.encode("ascii") + b'"'

        body = RGB_RE.sub(repl_rgb, m.group(2))
        return m.group(0)[:m.start(2) - m.start(0)] + body + m.group(0)[m.end(2) - m.start(0):]

    new_xml = GRID_RE.sub(repl_grid, xml_bytes)
    return new_xml, changed


def best_compress(xml_bytes, slot_len):
    """Return (level, compressed) with the smallest size; None if none fit slot_len."""
    best = None
    for lvl in range(1, 10):
        c = zlib.compress(xml_bytes, lvl)
        if best is None or len(c) < len(best[1]):
            best = (lvl, c)
    if len(best[1]) <= slot_len:
        return best
    return None


# ----------------------------------------------------------------------------
# Safety
# ----------------------------------------------------------------------------

def resolve_running():
    return bool(resolve_processes())


def _linux_process_rows():
    rows = []
    proc = Path("/proc")
    if not proc.is_dir():
        return rows
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            raw = (entry / "cmdline").read_bytes()
            if raw:
                parts = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
                cmd = " ".join(parts)
            else:
                cmd = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if cmd:
            rows.append((pid, cmd))
    return rows


def _pgrep_process_rows():
    import subprocess

    rows = []
    out = subprocess.check_output(["pgrep", "-fl", "resolve"], text=True, stderr=subprocess.DEVNULL)
    for line in out.splitlines():
        try:
            pid_s, cmd = line.split(maxsplit=1)
            rows.append((int(pid_s), cmd))
        except ValueError:
            continue
    return rows


def resolve_processes():
    """Best-effort Resolve process list: [(pid, command)]."""
    sysname = _sysname()
    procs = []
    try:
        if sysname == "Windows":
            import subprocess

            out = subprocess.check_output(
                ["tasklist", "/FO", "CSV", "/NH"], text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                cols = [c.strip('"') for c in line.split('","')]
                if len(cols) >= 2 and cols[0].lower() == "resolve.exe":
                    procs.append((int(cols[1]), cols[0]))
            return procs

        rows = _linux_process_rows() if sysname == "Linux" else _pgrep_process_rows()
        self_pid = os.getpid()
        for pid, cmd in rows:
            if pid == self_pid or "colorchecker_patch" in cmd or "/bin/sh" in cmd:
                continue
            if _is_resolve_process_cmd(cmd):
                procs.append((pid, cmd))
    except Exception:
        pass
    return procs


def _is_resolve_process_cmd(cmd):
    lower = cmd.lower()
    tokens = lower.split()
    first = tokens[0].strip('"') if tokens else lower.strip('"')
    base = os.path.basename(first.replace("\\", "/"))
    if base in ("resolve", "resolve.exe"):
        return True
    if "/opt/resolve/bin/resolve" in lower:
        return True
    if "davinci resolve" in lower and ("/contents/macos/resolve" in lower or "resolve.exe" in lower):
        return True
    if "blackmagic design" in lower and "davinci resolve" in lower and "resolve.exe" in lower:
        return True
    return False


def terminate_resolve_processes(timeout=8):
    procs = resolve_processes()
    if not procs:
        return []
    sysname = _sysname()
    killed = []
    if sysname == "Windows":
        import subprocess

        for pid, cmd in procs:
            subprocess.run(["taskkill", "/PID", str(pid), "/T"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            killed.append((pid, cmd))
        deadline = time.time() + timeout
        while time.time() < deadline and resolve_processes():
            time.sleep(0.5)
        for pid, cmd in resolve_processes():
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            killed.append((pid, cmd))
        return killed

    for pid, cmd in procs:
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append((pid, cmd))
        except OSError:
            pass
    deadline = time.time() + timeout
    while time.time() < deadline and resolve_processes():
        time.sleep(0.5)
    for pid, cmd in resolve_processes():
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append((pid, cmd))
        except OSError:
            pass
    return killed


def confirm_kill_resolve(yes=False):
    procs = resolve_processes()
    if not procs:
        return True
    print("WARNING: This will close DaVinci Resolve before patching.", file=sys.stderr)
    print("Save your project first. Unsaved timelines, grades, and project changes may be lost.", file=sys.stderr)
    print("Resolve process(es):", file=sys.stderr)
    for pid, cmd in procs:
        print(f"  {pid}: {cmd}", file=sys.stderr)
    if yes:
        return True
    if not sys.stdin.isatty():
        die("Refusing to kill Resolve non-interactively without --yes.")
    return input("Type KILL to close Resolve and continue: ").strip() == "KILL"


def kill_resolve_with_warning(yes=False):
    if not confirm_kill_resolve(yes):
        die("Aborted; Resolve was not killed.")
    killed = terminate_resolve_processes()
    if killed:
        print("Requested Resolve shutdown:")
        for pid, cmd in killed:
            print(f"  {pid}: {cmd}")
    if resolve_running():
        die("Resolve still appears to be running after termination attempt. Close it manually or use --force.")


def ensure_resolve_closed(force=False, wait=False, kill=False, yes=False, action="patch"):
    if force or not resolve_running():
        return

    msg = ("A 'resolve' process appears to be running. Close DaVinci Resolve before "
           f"this {action}, or force it if you know the detection is stale.")

    if kill:
        kill_resolve_with_warning(yes=yes)
        return

    if wait:
        print(msg)
        print("Waiting for Resolve to close... (Ctrl-C to abort)")
        try:
            while resolve_running():
                time.sleep(1)
        except KeyboardInterrupt:
            die("Aborted while waiting for Resolve to close.")
        print("Resolve is closed; continuing.")
        return

    if sys.stdin.isatty():
        while resolve_running():
            ans = input(msg + "\n[R]etry / [W]ait / [K]ill Resolve / [F]orce / [A]bort? ").strip().lower()
            if ans in ("", "r", "retry"):
                continue
            if ans in ("w", "wait"):
                return ensure_resolve_closed(force=False, wait=True, action=action)
            if ans in ("k", "kill"):
                return ensure_resolve_closed(force=False, kill=True, yes=False, action=action)
            if ans in ("f", "force"):
                warn("Forcing despite running Resolve. This may fail if the OS locks the executable.")
                return
            if ans in ("a", "abort", "q", "quit", "n", "no"):
                die("Aborted because Resolve is running.")
            print("Please answer retry, wait, kill, force, or abort.")
        return

    die(msg + "\nUse --wait-resolve to wait, --kill-resolve --yes to close it, or --force to bypass this guard.")


def can_write(path):
    try:
        with open(path, "r+b"):
            return True
    except OSError:
        return False


def escalate(path, argv):
    """Re-exec this script with admin rights if we cannot write the binary."""
    sysname = _sysname()
    script = os.path.abspath(__file__)
    if sysname == "Windows":
        import ctypes

        params = _windows_cmdline([script] + argv)
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        if rc <= 32:
            die(f"Windows elevation failed (ShellExecuteW returned {rc}).")
    else:
        print(f"Need write access to {path}; re-running with sudo...")
        os.execvp("sudo", ["sudo", "--", sys.executable, script] + argv)
    sys.exit(0)


# ----------------------------------------------------------------------------
# Patching
# ----------------------------------------------------------------------------

def make_backup(path, backup_dir):
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = _timestamp_filename()
    dst = backup_dir / f"resolve.{ts}.bak"
    n = 1
    while dst.exists():
        dst = backup_dir / f"resolve.{ts}.{n}.bak"
        n += 1
    _copy_file(path, dst)
    # keep a stable 'latest' pointer
    latest = backup_dir / "resolve.latest.bak"
    try:
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(dst.name)
    except OSError:
        _copy_file(path, latest)
    return dst


def latest_backup(backup_dir):
    p = Path(backup_dir) / "resolve.latest.bak"
    return p if p.exists() else None


def _real_user_home():
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root" and _sysname() != "Windows":
        try:
            import pwd
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except Exception:
            pass
    return Path.home()


def _default_type8_xml_path():
    return _real_user_home() / ".local/share/colorchecker_patch" / TYPE8_DEFAULT_XML_NAME


def _default_type9_xml_path():
    return _real_user_home() / ".local/share/colorchecker_patch" / TYPE9_DEFAULT_XML_NAME


def _type8_xml_output_path(path_arg):
    if path_arg:
        out = Path(path_arg).expanduser()
        if not out.is_absolute():
            out = (Path.cwd() / out).resolve()
        return out
    return _default_type8_xml_path()


def _type9_xml_output_path(path_arg):
    if path_arg:
        out = Path(path_arg).expanduser()
        if not out.is_absolute():
            out = (Path.cwd() / out).resolve()
        return out
    return _default_type9_xml_path()


def _has_patch_input(args):
    return any((args.table, args.preset, args.json, args.csv, args.xml, args.set))


def _type8_file_offset(vaddr, data):
    off = vaddr - TYPE8_IMAGE_BASE
    if off < 0 or off > len(data):
        die(f"type-8 patch address {vaddr:#x} is outside this binary")
    return off


def _rel32_jmp(src, dst):
    rel = (dst - (src + 5)) & 0xffffffff
    return b"\xe9" + rel.to_bytes(4, "little")


def _rel32_call(src, dst):
    rel = (dst - (src + 5)) & 0xffffffff
    return b"\xe8" + rel.to_bytes(4, "little")


def _ascii_c_string(text, capacity, field):
    if "\x00" in text:
        die(f"{field} contains a NUL byte")
    try:
        raw = text.encode("ascii")
    except UnicodeEncodeError:
        die(f"{field} must be ASCII for this Resolve binary patch")
    if len(raw) + 1 > capacity:
        die(f"{field} is too long: {len(raw)} bytes > {capacity - 1} bytes")
    return raw + b"\x00" + (b"\x00" * (capacity - len(raw) - 1))


def _type8_path_table_bytes():
    ptrs = list(TYPE8_STOCK_PATH_PTRS) + [TYPE8_XML_PATH_CAVE]
    return b"".join(int(p).to_bytes(8, "little") for p in ptrs)


def _type9_path_table_bytes():
    ptrs = list(TYPE8_STOCK_PATH_PTRS) + [TYPE8_XML_PATH_CAVE, TYPE9_XML_PATH_CAVE]
    return b"".join(int(p).to_bytes(8, "little") for p in ptrs)


def _type8_path_table_bytes_padded_for_type9():
    return _type8_path_table_bytes() + (0).to_bytes(8, "little")


def _dropdown_item_block(vaddr, chart_type, label_vaddr):
    out = bytearray()

    def emit(b):
        out.extend(b)

    def cur():
        return vaddr + len(out)

    emit(bytes.fromhex("48 c7 c6 98 e9 be 26"))       # mov rsi, QString encoding helper data
    emit(bytes.fromhex("48 c7 c2") + int(label_vaddr).to_bytes(4, "little"))
    emit(bytes.fromhex("48 8d 7c 24 08 31 c9 41 b8 ff ff ff ff"))
    emit(_rel32_call(cur(), 0x77FF40))
    emit(bytes.fromhex("c7 44 24 14") + int(chart_type).to_bytes(4, "little"))
    emit(bytes.fromhex("48 8d 7c 24 18 48 8d 54 24 14 be 02 00 00 00 31 c9"))
    emit(_rel32_call(cur(), 0x787E50))
    emit(bytes.fromhex("48 8d 74 24 08 48 8d 54 24 18 4c 89 f7 31 c9"))
    emit(_rel32_call(cur(), 0x1813FE0))
    emit(bytes.fromhex("48 8d 7c 24 18"))
    emit(_rel32_call(cur(), 0x7912C0))
    emit(bytes.fromhex("48 8d 7c 24 08"))
    emit(_rel32_call(cur(), 0xDBDCE0))
    return bytes(out)


def _type8_dropdown_cave_bytes():
    return bytes.fromhex(
        "48 8b 43 58 4c 8b 70 30 48 c7 c6 98 e9 be 26 "
        "48 c7 c2 20 c4 a9 09 48 8d 7c 24 08 31 c9 "
        "41 b8 ff ff ff ff e8 78 3c ce f6 c7 44 24 14 "
        "08 00 00 00 48 8d 7c 24 18 48 8d 54 24 14 be "
        "02 00 00 00 31 c9 e8 6a bb ce f6 48 8d 74 24 "
        "08 48 8d 54 24 18 4c 89 f7 31 c9 e8 e6 7c d7 "
        "f7 48 8d 7c 24 18 e8 bc 4f cf f6 48 8d 7c 24 "
        "08 e8 d2 19 32 f7 48 8b 43 58 48 8b 78 30 e9 "
        "cc 13 c4 f7"
    )


def _type9_dropdown_cave_bytes():
    out = bytearray()

    def emit(b):
        out.extend(b)

    def cur():
        return TYPE9_DROPDOWN_CAVE + len(out)

    emit(bytes.fromhex("48 8b 43 58 4c 8b 70 30"))
    emit(_dropdown_item_block(cur(), 8, TYPE8_LABEL_CAVE))
    emit(_dropdown_item_block(cur(), 9, TYPE9_LABEL_CAVE))
    emit(bytes.fromhex("48 8b 43 58 48 8b 78 30"))
    emit(_rel32_jmp(cur(), 0x16DD6E7))
    return bytes(out)


def _type8_ui_alias_cave_bytes():
    return bytes.fromhex(
        "83 f8 08 75 05 b8 07 00 00 00 89 c5 48 89 e7 "
        "e9 08 c5 c3 f7"
    )


def _type9_ui_alias_cave_bytes():
    base = TYPE8_UI_ALIAS_CAVE
    out = bytearray(bytes.fromhex(
        "83 f8 08 72 0a 83 f8 09 77 05 b8 07 00 00 00 "
        "89 c5 48 89 e7"
    ))
    out.extend(_rel32_jmp(base + len(out), 0x16D885C))
    return bytes(out)


def _type8_prop_cave_a_bytes():
    return bytes.fromhex(
        "83 ff 07 75 05 bf 06 00 00 00 83 ff 07 0f 83 "
        "c5 31 9d fb 89 f8 e9 92 31 9d fb"
    )


def _type9_prop_cave_a_bytes():
    base = TYPE8_PROP_CAVE_A
    out = bytearray(bytes.fromhex(
        "83 ff 07 72 0a 83 ff 08 77 0c bf 06 00 00 00 89 f8"
    ))
    out.extend(_rel32_jmp(base + len(out), 0x546F72C))
    out.extend(_rel32_jmp(base + len(out), 0x546F758))
    return bytes(out)


def _type9_prop_cave_b_bytes():
    base = TYPE8_PROP_CAVE_B
    out = bytearray(bytes.fromhex(
        "83 fd 07 72 0a 83 fd 08 77 0c bd 06 00 00 00 89 e8"
    ))
    out.extend(_rel32_jmp(base + len(out), 0x546FE2E))
    out.extend(_rel32_jmp(base + len(out), 0x546FF37))
    return bytes(out)


def _type8_prop_cave_b_bytes():
    return bytes.fromhex(
        "83 fd 07 75 05 bd 06 00 00 00 83 fd 07 0f 83 "
        "64 39 9d fb 89 e8 e9 54 38 9d fb"
    )


def _matcher_type_alias_cave_bytes():
    base = TYPE8_MATCHER_TYPE_ALIAS_CAVE
    out = bytearray(bytes.fromhex(
        "41 89 eb "                 # mov r11d, ebp
        "41 83 fb 08 72 0c "        # if type < 8, keep it
        "41 83 fb 09 77 06 "        # if type > 9, keep it
        "41 bb 07 00 00 00 "        # type 8/9 use Classic matcher geometry
        "44 89 1f "                 # mov [rdi], r11d
        "89 57 04"                  # mov [rdi+4], edx
    ))
    out.extend(_rel32_jmp(base + len(out), TYPE8_MATCHER_TYPE_STORE_HOOK + 5))
    return bytes(out)


def _type8_install_specs():
    getter_alias = _rel32_jmp(TYPE8_MATCH_GETTER, 0x9A9C360)
    return [
        ("extra dropdown hook", TYPE8_DROPDOWN_HOOK,
         bytes.fromhex("48 8b 43 58 48 8b 78 30"),
         _rel32_jmp(TYPE8_DROPDOWN_HOOK, TYPE8_DROPDOWN_CAVE) + b"\x90\x90\x90", []),
        ("UI sampling aliases type 8 to type 7", TYPE8_UI_ALIAS_HOOK,
         bytes.fromhex("89 c5 48 89 e7"),
         _rel32_jmp(TYPE8_UI_ALIAS_HOOK, TYPE8_UI_ALIAS_CAVE), []),
        ("Match getter returns real type 8", TYPE8_MATCH_GETTER,
         bytes.fromhex("89 c3 48 89 e7"),
         bytes.fromhex("89 c3 48 89 e7"), [getter_alias]),
        ("chart vector size", 0x5478A75,
         bytes.fromhex("be 08 00 00 00"), bytes.fromhex("be 09 00 00 00"), []),
        ("chart vector remainder guard", 0x5478AB7,
         bytes.fromhex("48 83 f9 07"), bytes.fromhex("48 83 f9 08"), []),
        ("chart vector fill count", 0x5478ABD,
         bytes.fromhex("be 08 00 00 00"), bytes.fromhex("be 09 00 00 00"), []),
        ("chart XML path table load A", 0x5478B89,
         bytes.fromhex("4c 8d 25 70 cd 92 21"), bytes.fromhex("49 c7 c4 80 c4 a9 09"), []),
        ("chart XML load loop includes type 8", 0x5478BC3,
         bytes.fromhex("49 83 ff 08"), bytes.fromhex("49 83 ff 09"), []),
        ("chart XML path table load B", 0x5478EC4,
         bytes.fromhex("4c 8d 25 35 ca 92 21"), bytes.fromhex("49 c7 c4 80 c4 a9 09"), []),
        ("chart accessor bound A", 0x5479140,
         bytes.fromhex("83 fe 08"), bytes.fromhex("83 fe 09"), []),
        ("chart accessor bound B", 0x5479170,
         bytes.fromhex("83 fe 08"), bytes.fromhex("83 fe 09"), []),
        ("chart accessor bound C", 0x54791CC,
         bytes.fromhex("83 fa 08"), bytes.fromhex("83 fa 09"), []),
        ("chart accessor bound D", 0x54792D3,
         bytes.fromhex("83 fa 08"), bytes.fromhex("83 fa 09"), []),
        ("BuildChartProp index hook A", 0x546F725,
         bytes.fromhex("83 ff 07 73 2e 89 f8"),
         _rel32_jmp(0x546F725, TYPE8_PROP_CAVE_A) + b"\x90\x90", []),
        ("BuildChartProp index hook B", 0x546FE23,
         bytes.fromhex("83 fd 07 0f 83 0b 01 00 00"),
         _rel32_jmp(0x546FE23, TYPE8_PROP_CAVE_B) + b"\x90\x90\x90\x90", []),
        ("ColorChartMatcher stores safe extra type", TYPE8_MATCHER_TYPE_STORE_HOOK,
         bytes.fromhex("89 2f 89 57 04"),
         _rel32_jmp(TYPE8_MATCHER_TYPE_STORE_HOOK, TYPE8_MATCHER_TYPE_ALIAS_CAVE), []),
    ]


def _type9_install_specs():
    type8_hook = _rel32_jmp(TYPE8_DROPDOWN_HOOK, TYPE8_DROPDOWN_CAVE) + b"\x90\x90\x90"
    type9_hook = _rel32_jmp(TYPE8_DROPDOWN_HOOK, TYPE9_DROPDOWN_CAVE) + b"\x90\x90\x90"
    ui_hook = _rel32_jmp(TYPE8_UI_ALIAS_HOOK, TYPE8_UI_ALIAS_CAVE)
    getter_alias = _rel32_jmp(TYPE8_MATCH_GETTER, 0x9A9C360)
    prop_hook_a = _rel32_jmp(0x546F725, TYPE8_PROP_CAVE_A) + b"\x90\x90"
    prop_hook_b = _rel32_jmp(0x546FE23, TYPE8_PROP_CAVE_B) + b"\x90\x90\x90\x90"
    matcher_type_hook = _rel32_jmp(TYPE8_MATCHER_TYPE_STORE_HOOK, TYPE8_MATCHER_TYPE_ALIAS_CAVE)
    return [
        ("extra dropdown hook", TYPE8_DROPDOWN_HOOK,
         bytes.fromhex("48 8b 43 58 48 8b 78 30"), type9_hook, [type8_hook]),
        ("UI sampling aliases type 8/9 to type 7", TYPE8_UI_ALIAS_HOOK,
         bytes.fromhex("89 c5 48 89 e7"), ui_hook, []),
        ("Match getter returns real extra type", TYPE8_MATCH_GETTER,
         bytes.fromhex("89 c3 48 89 e7"), bytes.fromhex("89 c3 48 89 e7"), [getter_alias]),
        ("chart vector size", 0x5478A75,
         bytes.fromhex("be 08 00 00 00"), bytes.fromhex("be 0a 00 00 00"), [bytes.fromhex("be 09 00 00 00")]),
        ("chart vector remainder guard", 0x5478AB7,
         bytes.fromhex("48 83 f9 07"), bytes.fromhex("48 83 f9 09"), [bytes.fromhex("48 83 f9 08")]),
        ("chart vector fill count", 0x5478ABD,
         bytes.fromhex("be 08 00 00 00"), bytes.fromhex("be 0a 00 00 00"), [bytes.fromhex("be 09 00 00 00")]),
        ("chart XML path table load A", 0x5478B89,
         bytes.fromhex("4c 8d 25 70 cd 92 21"), bytes.fromhex("49 c7 c4 80 c4 a9 09"), []),
        ("chart XML load loop includes type 9", 0x5478BC3,
         bytes.fromhex("49 83 ff 08"), bytes.fromhex("49 83 ff 0a"), [bytes.fromhex("49 83 ff 09")]),
        ("chart XML path table load B", 0x5478EC4,
         bytes.fromhex("4c 8d 25 35 ca 92 21"), bytes.fromhex("49 c7 c4 80 c4 a9 09"), []),
        ("chart accessor bound A", 0x5479140,
         bytes.fromhex("83 fe 08"), bytes.fromhex("83 fe 0a"), [bytes.fromhex("83 fe 09")]),
        ("chart accessor bound B", 0x5479170,
         bytes.fromhex("83 fe 08"), bytes.fromhex("83 fe 0a"), [bytes.fromhex("83 fe 09")]),
        ("chart accessor bound C", 0x54791CC,
         bytes.fromhex("83 fa 08"), bytes.fromhex("83 fa 0a"), [bytes.fromhex("83 fa 09")]),
        ("chart accessor bound D", 0x54792D3,
         bytes.fromhex("83 fa 08"), bytes.fromhex("83 fa 0a"), [bytes.fromhex("83 fa 09")]),
        ("BuildChartProp index hook A", 0x546F725,
         bytes.fromhex("83 ff 07 73 2e 89 f8"), prop_hook_a, []),
        ("BuildChartProp index hook B", 0x546FE23,
         bytes.fromhex("83 fd 07 0f 83 0b 01 00 00"), prop_hook_b, []),
        ("ColorChartMatcher stores safe extra type", TYPE8_MATCHER_TYPE_STORE_HOOK,
         bytes.fromhex("89 2f 89 57 04"), matcher_type_hook, []),
    ]


def _type8_restore_specs():
    out = []
    for name, vaddr, stock, patched, extra_expected in _type8_install_specs():
        expected = [patched] + list(extra_expected)
        for t9_name, t9_vaddr, _t9_stock, t9_patched, t9_extra in _type9_install_specs():
            if t9_vaddr == vaddr:
                expected.append(t9_patched)
                expected.extend(t9_extra)
        out.append((name, vaddr, patched, stock, expected))
    return out


def _add_type8_plan(plan, data, name, vaddr, replacement, expected=None):
    off = _type8_file_offset(vaddr, data)
    current = data[off:off + len(replacement)]
    if len(current) != len(replacement):
        die(f"{name}: could not read enough bytes at {vaddr:#x}")
    if current == replacement:
        status = "already"
    elif expected is None or any(current == item for item in expected):
        status = "write"
    else:
        exp = ", ".join(item.hex(" ") for item in expected)
        die(
            f"{name}: unexpected bytes at {vaddr:#x}: {current.hex(' ')}\n"
            f"Expected one of: {exp}\n"
            "This Resolve binary layout is not the one this type-8 patch supports."
        )
    plan.append({"name": name, "vaddr": vaddr, "off": off, "data": replacement, "status": status})


def _type8_install_plan(data, label, xml_path):
    label_bytes = _ascii_c_string(label, TYPE8_LABEL_CAPACITY, "--type8-label")
    path_bytes = _ascii_c_string(str(xml_path), TYPE8_XML_PATH_CAPACITY, "--type8-xml")
    plan = []
    for name, vaddr, stock, patched, extra_expected in _type8_install_specs():
        _add_type8_plan(plan, data, name, vaddr, patched, [stock] + list(extra_expected))
    _add_type8_plan(plan, data, "dropdown cave", TYPE8_DROPDOWN_CAVE,
                    _type8_dropdown_cave_bytes(), [b"\x00" * len(_type8_dropdown_cave_bytes())])
    _add_type8_plan(plan, data, "UI sampling alias cave", TYPE8_UI_ALIAS_CAVE,
                    _type8_ui_alias_cave_bytes(), [b"\x00" * len(_type8_ui_alias_cave_bytes())])
    _add_type8_plan(plan, data, "path table cave", TYPE8_PATH_TABLE_CAVE,
                    _type8_path_table_bytes(), [b"\x00" * len(_type8_path_table_bytes())])
    _add_type8_plan(plan, data, "BuildChartProp cave A", TYPE8_PROP_CAVE_A,
                    _type8_prop_cave_a_bytes(), [b"\x00" * len(_type8_prop_cave_a_bytes())])
    _add_type8_plan(plan, data, "BuildChartProp cave B", TYPE8_PROP_CAVE_B,
                    _type8_prop_cave_b_bytes(), [b"\x00" * len(_type8_prop_cave_b_bytes())])
    matcher_type_alias = _matcher_type_alias_cave_bytes()
    _add_type8_plan(plan, data, "ColorChartMatcher type alias cave", TYPE8_MATCHER_TYPE_ALIAS_CAVE,
                    matcher_type_alias, [b"\x00" * len(matcher_type_alias)])
    _add_type8_plan(plan, data, "type-8 dropdown label", TYPE8_LABEL_CAVE, label_bytes, None)
    _add_type8_plan(plan, data, "type-8 XML path", TYPE8_XML_PATH_CAVE, path_bytes, None)
    return plan


def _pad_expected(blob, length):
    if len(blob) > length:
        return blob[:length]
    return blob + (b"\x00" * (length - len(blob)))


def _type9_install_plan(data, type8_label, type8_xml_path, type9_label, type9_xml_path):
    type8_label_bytes = _ascii_c_string(type8_label, TYPE8_LABEL_CAPACITY, "--type8-label")
    type8_path_bytes = _ascii_c_string(str(type8_xml_path), TYPE8_XML_PATH_CAPACITY, "--type8-xml")
    type9_label_bytes = _ascii_c_string(type9_label, TYPE9_LABEL_CAPACITY, "--type9-label")
    type9_path_bytes = _ascii_c_string(str(type9_xml_path), TYPE9_XML_PATH_CAPACITY, "--type9-xml")
    plan = []
    for name, vaddr, stock, patched, extra_expected in _type9_install_specs():
        _add_type8_plan(plan, data, name, vaddr, patched, [stock] + list(extra_expected))
    type9_dropdown = _type9_dropdown_cave_bytes()
    type9_ui_alias = _type9_ui_alias_cave_bytes()
    type9_path_table = _type9_path_table_bytes()
    type9_prop_a = _type9_prop_cave_a_bytes()
    type9_prop_b = _type9_prop_cave_b_bytes()
    matcher_type_alias = _matcher_type_alias_cave_bytes()
    _add_type8_plan(plan, data, "two-entry dropdown cave", TYPE9_DROPDOWN_CAVE,
                    type9_dropdown, [b"\x00" * len(type9_dropdown)])
    _add_type8_plan(plan, data, "UI sampling alias cave", TYPE8_UI_ALIAS_CAVE,
                    type9_ui_alias, [
                        b"\x00" * len(type9_ui_alias),
                        _pad_expected(_type8_ui_alias_cave_bytes(), len(type9_ui_alias)),
                    ])
    _add_type8_plan(plan, data, "path table cave", TYPE8_PATH_TABLE_CAVE,
                    type9_path_table, [
                        b"\x00" * len(type9_path_table),
                        _type8_path_table_bytes_padded_for_type9(),
                    ])
    _add_type8_plan(plan, data, "BuildChartProp cave A", TYPE8_PROP_CAVE_A,
                    type9_prop_a, [
                        b"\x00" * len(type9_prop_a),
                        _pad_expected(_type8_prop_cave_a_bytes(), len(type9_prop_a)),
                    ])
    _add_type8_plan(plan, data, "BuildChartProp cave B", TYPE8_PROP_CAVE_B,
                    type9_prop_b, [
                        b"\x00" * len(type9_prop_b),
                        _pad_expected(_type8_prop_cave_b_bytes(), len(type9_prop_b)),
                    ])
    _add_type8_plan(plan, data, "ColorChartMatcher type alias cave", TYPE8_MATCHER_TYPE_ALIAS_CAVE,
                    matcher_type_alias, [b"\x00" * len(matcher_type_alias)])
    _add_type8_plan(plan, data, "type-8 dropdown label", TYPE8_LABEL_CAVE, type8_label_bytes, None)
    _add_type8_plan(plan, data, "type-8 XML path", TYPE8_XML_PATH_CAVE, type8_path_bytes, None)
    _add_type8_plan(plan, data, "type-9 dropdown label", TYPE9_LABEL_CAVE, type9_label_bytes, None)
    _add_type8_plan(plan, data, "type-9 XML path", TYPE9_XML_PATH_CAVE, type9_path_bytes, None)
    return plan


def _type8_restore_plan(data):
    plan = []
    for name, vaddr, _patched, stock, expected in _type8_restore_specs():
        _add_type8_plan(plan, data, name, vaddr, stock, expected)
    return plan


def _write_type8_plan(path, plan):
    with open(path, "r+b") as f:
        for item in plan:
            if item["status"] == "already":
                continue
            f.seek(item["off"])
            f.write(item["data"])
        f.flush()
        os.fsync(f.fileno())


def _verify_type8_plan(path, plan):
    data = Path(path).read_bytes()
    for item in plan:
        off = item["off"]
        if data[off:off + len(item["data"])] != item["data"]:
            die(f"VERIFICATION FAILED: {item['name']} did not match expected bytes after write.")


def _show_type8_plan(plan):
    writes = 0
    for item in plan:
        if item["status"] == "already":
            print(f"Already      : {item['name']} @ {item['vaddr']:#x}")
        else:
            writes += 1
            print(f"Patch        : {item['name']} @ {item['vaddr']:#x} ({len(item['data'])} bytes)")
    return writes


def _build_type8_xml(chart, edits, label, rgb_overrides=None):
    return _build_extra_type_xml(chart, edits, label, "8", rgb_overrides)


def _build_extra_type_xml(chart, edits, label, chart_type, rgb_overrides=None):
    xml = build_resolve_xml(chart, edits)
    if rgb_overrides:
        xml, _changed = _replace_rgb_values(xml, rgb_overrides)
    xml, renamed = _rename_chart_xml(xml, label)
    if not renamed:
        die(f"Could not rewrite type-{chart_type} XML chart name.")

    def repl(m):
        return m.group(1) + str(chart_type).encode("ascii") + m.group(2)

    xml, n = re.subn(rb'(<colorchart\b[^>]*\btype=")\d+("[^>]*>)', repl, xml, count=1)
    if n != 1:
        die(f"Could not rewrite type-{chart_type} XML chart type.")

    attrs = _colorchart_attrs(xml)
    if attrs.get("type") != str(chart_type) or len(_extract_values(xml)) != 24:
        die(f"Generated type-{chart_type} XML must be a colorchart with type={chart_type} and 24 grids.")
    return xml


def _maybe_chown_to_sudo_user(path):
    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if not uid or not gid or _sysname() == "Windows":
        return
    try:
        os.chown(path, int(uid), int(gid))
    except OSError:
        pass


def _write_type8_xml(path, xml_bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(xml_bytes)
    _maybe_chown_to_sudo_user(path.parent)
    _maybe_chown_to_sudo_user(path)


def install_type8_entry(path, chart, edits, label, xml_path_arg, rgb_overrides,
                        dry_run, backup_dir, yes, force, wait_resolve=False,
                        kill_resolve=False):
    xml_path = _type8_xml_output_path(xml_path_arg)
    xml_bytes = _build_type8_xml(chart, edits, label, rgb_overrides)
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        die(f"Could not read {path}: {e}")
    plan = _type8_install_plan(data, label, xml_path)

    print(f"Type-8 label : {label!r}")
    print(f"Type-8 XML   : {xml_path}")
    print(f"Template     : {chart.name!r} (type={chart.ctype})")
    print(f"Patches      : {len(edits)} staged reference value(s)")
    writes = _show_type8_plan(plan)
    if writes == 0 and xml_path.exists() and xml_path.read_bytes() == xml_bytes:
        print("Status       : binary hooks and XML are already current")

    if dry_run:
        print("\n--dry-run: no changes written.")
        return

    ensure_resolve_closed(force=force, wait=wait_resolve, kill=kill_resolve, yes=yes, action="type-8 install")
    if not yes:
        if input(f"\nInstall/update type-8 Color Match entry in {path}? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return
    if not can_write(path):
        escalate(path, _raw_argv())

    bk = make_backup(path, backup_dir)
    print(f"Backup       : {bk}")
    _write_type8_xml(xml_path, xml_bytes)
    print(f"Wrote XML    : {xml_path}")
    _write_type8_plan(path, plan)
    _verify_type8_plan(path, plan)
    print("Verified     : type-8 binary hooks and external XML are installed.")
    print("Done. Restart DaVinci Resolve and select the new Color Match chart entry.")


def install_type9_entries(path, chart, type9_edits, type9_rgb_overrides,
                          type8_label, type8_xml_arg, type9_label, type9_xml_arg,
                          dry_run, backup_dir, yes, force, wait_resolve=False,
                          kill_resolve=False):
    type8_xml_path = _type8_xml_output_path(type8_xml_arg)
    type9_xml_path = _type9_xml_output_path(type9_xml_arg)
    try:
        type8_edits = _edits_from_builtin("aliexpress")
    except Exception as e:
        die(f"Could not load default type-8 AliExpress table: {e}")
    type8_rgb_overrides = _rgb_metadata_from_named_table("aliexpress")
    type8_xml_bytes = _build_extra_type_xml(chart, type8_edits, type8_label, "8", type8_rgb_overrides)
    type9_xml_bytes = _build_extra_type_xml(chart, type9_edits, type9_label, "9", type9_rgb_overrides)
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        die(f"Could not read {path}: {e}")
    plan = _type9_install_plan(data, type8_label, type8_xml_path, type9_label, type9_xml_path)

    print(f"Type-8 label : {type8_label!r}")
    print(f"Type-8 XML   : {type8_xml_path}")
    print(f"Type-9 label : {type9_label!r}")
    print(f"Type-9 XML   : {type9_xml_path}")
    print(f"Template     : {chart.name!r} (type={chart.ctype})")
    print(f"Type-9 data  : {len(type9_edits)} staged reference value(s)")
    writes = _show_type8_plan(plan)
    xml_current = (
        type8_xml_path.exists() and type8_xml_path.read_bytes() == type8_xml_bytes and
        type9_xml_path.exists() and type9_xml_path.read_bytes() == type9_xml_bytes
    )
    if writes == 0 and xml_current:
        print("Status       : two extra entries and XML files are already current")

    if dry_run:
        print("\n--dry-run: no changes written.")
        return

    ensure_resolve_closed(force=force, wait=wait_resolve, kill=kill_resolve, yes=yes, action="type-9 install")
    if not yes:
        if input(f"\nInstall/update two extra Color Match entries in {path}? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return
    if not can_write(path):
        escalate(path, _raw_argv())

    bk = make_backup(path, backup_dir)
    print(f"Backup       : {bk}")
    _write_type8_xml(type8_xml_path, type8_xml_bytes)
    print(f"Wrote XML    : {type8_xml_path}")
    _write_type8_xml(type9_xml_path, type9_xml_bytes)
    print(f"Wrote XML    : {type9_xml_path}")
    _write_type8_plan(path, plan)
    _verify_type8_plan(path, plan)
    print("Verified     : type-8/type-9 binary hooks and external XML files are installed.")
    print("Done. Restart DaVinci Resolve and select either extra Color Match chart entry.")


def restore_type8_entry(backup_dir, path, force=False, wait_resolve=False,
                        kill_resolve=False, yes=False, dry_run=False):
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        die(f"Could not read {path}: {e}")
    plan = _type8_restore_plan(data)
    writes = _show_type8_plan(plan)
    if writes == 0:
        print("Status       : type-8 hooks are already absent")
    print("Note         : external XML data is left in place; use --restore for a byte-for-byte backup restore.")

    if dry_run:
        print("\n--dry-run: no changes written.")
        return

    ensure_resolve_closed(force=force, wait=wait_resolve, kill=kill_resolve, yes=yes, action="type-8 restore")
    if not yes:
        if input(f"\nRemove type-8 Color Match hooks from {path}? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return
    if not can_write(path):
        escalate(path, _raw_argv())

    bk = make_backup(path, backup_dir)
    print(f"Safety backup: {bk}")
    _write_type8_plan(path, plan)
    _verify_type8_plan(path, plan)
    print("Verified     : type-8 hook sites are restored to stock bytes.")


def patch(path, chart, edits, dry_run, backup_dir, yes, force, wait_resolve=False, kill_resolve=False, rename_label=None):
    edits = edits or {}
    new_xml = chart.xml
    changed = 0
    if edits:
        new_xml, changed = _replace_xyz_values(new_xml, edits, 6)

    rename_xml_changed = False
    label_plan = None
    rename_from = None
    if rename_label:
        old_label = chart_ui_label(chart)
        with open(path, "rb") as f:
            binary_data = f.read()
        current_label = _label_slot_layout(binary_data).get(chart.ctype)
        rename_from = current_label[2] if current_label else old_label
        try:
            label_plan = _label_patch_plan(binary_data, old_label, rename_label, chart.ctype)
        except ValueError as e:
            die(str(e))
        xml_candidate, did_xml = _rename_chart_xml(new_xml, rename_label)
        if did_xml and len(xml_candidate) <= chart.unc_len:
            new_xml = xml_candidate
            rename_xml_changed = True
        elif did_xml:
            warn("Embedded XML chart name was not changed because the requested name is longer than the original XML slot; UI label will still be renamed.")

    if changed == 0 and not rename_label:
        die("No selected grid values differ from the current chart. Nothing to change.")

    print(f"Target chart : {chart.name!r} (type={chart.ctype}) @ offset {chart.off}")
    if rename_label:
        print(f"Rename       : {rename_from!r} -> {rename_label!r}")
    print(f"Slot         : {chart.comp_stored} bytes stored, stream={chart.comp_len}, "
          f"uncompressed={chart.unc_len}")
    print(f"Patches      : {changed} <grid> value(s) changed")

    slot_len = chart.comp_stored  # max bytes we may overwrite safely
    xml_changed = (changed > 0) or rename_xml_changed
    result = best_compress(new_xml, slot_len) if xml_changed else (None, None)
    if xml_changed and result is None:
        # try trimming precision to fit
        trimmed = _trim_precision(new_xml, edits)
        result2 = best_compress(trimmed, slot_len) if trimmed else None
        if result2 is None:
            die(f"Edited chart does not fit its slot even after trimming precision "
                f"(need <={slot_len} compressed bytes). Aborting to avoid corruption.")
        new_xml, result, note = trimmed, result2, " (precision trimmed to fit slot)"
    else:
        note = ""
    level, new_stream = result
    if xml_changed:
        print(f"Compressed   : level {level} -> {len(new_stream)} bytes (<= {slot_len} slot){note}")
    else:
        print("Compressed   : skipped (UI label rename only)")

    if dry_run:
        print("\n--dry-run: no changes written.")
        if edits:
            _show_value_diff(chart.xml, new_xml, edits)
        if rename_label and label_plan:
            print(f"UI label hits : {len(label_plan[0])} occurrence(s) will be patched")
        return

    ensure_resolve_closed(force=force, wait=wait_resolve, kill=kill_resolve, yes=yes, action="patch")

    if not yes:
        print()
        _show_value_diff(chart.xml, new_xml, edits)
        if input("\nApply this patch to %s? [y/N] " % path).strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return

    if not can_write(path):
        escalate(path, _raw_argv())

    bk = make_backup(path, backup_dir)
    print(f"Backup       : {bk}")

    # Overwrite [off : off+len(new_stream)] and leave the slot tail as slack.
    with open(path, "r+b") as f:
        if xml_changed:
            f.seek(chart.off)
            f.write(new_stream)
        if label_plan:
            offsets, _old, label_bytes = label_plan
            _write_label_patch(f, offsets, label_bytes)
        f.flush()
        os.fsync(f.fileno())

    # ---- verify by re-reading and re-decompressing ----
    if xml_changed:
        with open(path, "rb") as f:
            f.seek(chart.off)
            verify_blob = f.read(slot_len)
        dv = zlib.decompressobj()
        try:
            recovered = dv.decompress(verify_blob) + dv.flush()
        except Exception as e:
            die(f"VERIFICATION FAILED: patched stream did not decompress ({e}).\n"
                f"Restore from {bk} !")
        if recovered != new_xml:
            die("VERIFICATION FAILED: round-trip XML does not match what we wrote.\n"
                f"Restore from {bk} !")
        recovered_vals = _extract_values(recovered)
        expected_vals = _extract_values(new_xml)
        ok = all(
            recovered_vals.get(idx) == expected_vals.get(idx)
            for idx in edits
        )
        if not ok:
            warn("Verification: could not confirm all edited values in round-trip "
                 "(decimal formatting); re-check with --print-current.")
    if rename_label and label_plan:
        data_after = Path(path).read_bytes()
        if label_plan[2] not in data_after:
            die(f"VERIFICATION FAILED: renamed UI label was not found. Restore from {bk} !")
    if xml_changed:
        print("Verified     : patched stream decompresses and round-trips correctly.")
    else:
        print("Verified     : UI label patch present.")
    print("Done. Restart DaVinci Resolve, then use Color Match on your custom chart.")


def _trim_precision(xml_bytes, edits):
    """Rebuild XML with edited values at lower precision (5, then 4 decimals)
    so the compressed stream shrinks to fit the slot."""
    for decimals in (5, 4, 3):
        out, changed = _replace_xyz_values(xml_bytes, edits, decimals)
        if len(out) < len(xml_bytes):
            return out
    return None


def _show_value_diff(old_xml, new_xml, edits):
    old_vals = _extract_values(old_xml)
    new_vals = _extract_values(new_xml)
    print("Changes (index: old xyz -> new xyz):")
    shown = 0
    for idx in sorted(edits):
        old = old_vals.get(idx, "?")
        new = new_vals.get(idx, "?")
        if old == new:
            continue
        shown += 1
        print(f"  {idx:>2}: {old:<28} -> {new}")
    if shown == 0:
        print("  (none)")


def _extract_values(xml_bytes):
    vals = {}
    for m in GRID_RE.finditer(xml_bytes):
        idx = int(m.group(1))
        xm = XYZ_RE.search(m.group(2))
        if xm:
                vals[idx] = xm.group(1).decode("ascii", "replace")
    return vals


def _extract_rgb_values(xml_bytes):
    vals = {}
    for m in GRID_RE.finditer(xml_bytes):
        idx = int(m.group(1))
        rm = RGB_RE.search(m.group(2))
        if rm:
            vals[idx] = rm.group(1).decode("ascii", "replace")
    return vals


def _extract_patch_info(xml_bytes):
    """Return {index: {name, xyz}} from a Resolve colorchart XML."""
    vals = _extract_values(xml_bytes)
    info = {}
    for m in GRID_BLOCK_RE.finditer(xml_bytes):
        attrs = {
            k.decode("ascii", "replace"): v.decode("utf-8", "replace")
            for k, v in ATTR_RE.findall(m.group(1))
        }
        if "index" not in attrs:
            continue
        idx = int(attrs["index"])
        info[idx] = {
            "name": attrs.get("name", f"Patch {idx}"),
            "xyz": vals.get(idx, ""),
        }
    return info


def _parse_xyz_string(s):
    parts = [p for p in re.split(r"[,\s]+", s.strip()) if p]
    if len(parts) != 3:
        raise ValueError(f"expected 3 XYZ components, got {s!r}")
    return tuple(float(p) for p in parts)


def _xyz_to_srgb8(xyz):
    """Approximate D65 XYZ -> display sRGB for terminal previews."""
    x, y, z = xyz
    r = 3.2406 * x - 1.5372 * y - 0.4986 * z
    g = -0.9689 * x + 1.8758 * y + 0.0415 * z
    b = 0.0557 * x - 0.2040 * y + 1.0570 * z

    def enc(c):
        c = max(0.0, min(1.0, c))
        if c <= 0.0031308:
            c = 12.92 * c
        else:
            c = 1.055 * (c ** (1 / 2.4)) - 0.055
        return max(0, min(255, int(round(c * 255))))

    return enc(r), enc(g), enc(b)


def _ansi_bg(rgb, text="  "):
    r, g, b = rgb
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    fg = (0, 0, 0) if lum > 150 else (255, 255, 255)
    return (f"\x1b[48;2;{r};{g};{b}m\x1b[38;2;{fg[0]};{fg[1]};{fg[2]}m"
            f"{text}\x1b[0m")


def _ansi_fg(rgb, text, bold=False):
    r, g, b = rgb
    prefix = "\x1b[1m" if bold else ""
    return f"{prefix}\x1b[38;2;{r};{g};{b}m{text}\x1b[0m"


def _ansi_bold(text):
    return f"\x1b[1m{text}\x1b[0m"


def _ansi_dim(text):
    return f"\x1b[2m{text}\x1b[0m"


def _rgb_hex(rgb):
    r, g, b = rgb
    return f"#{r:02X}{g:02X}{b:02X}"


def _clip_text(text, width):
    text = str(text)
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[:width - 3] + "..."


def print_current(chart):
    vals = _extract_values(chart.xml)
    print(f"# {chart.name} (type={chart.ctype})")
    for idx in range(24):
        print(f"  {idx:>2}: {vals.get(idx, '(missing)')}")


def _merged_xyz_values(chart, edits=None):
    edits = edits or {}
    info = _extract_patch_info(chart.xml)
    values = {}
    for idx in range(24):
        if idx in edits:
            values[idx] = edits[idx]
        elif idx in info and info[idx].get("xyz"):
            values[idx] = _parse_xyz_string(info[idx]["xyz"])
    return values


def build_canonical_json(chart, edits=None):
    info = _extract_patch_info(chart.xml)
    values = _merged_xyz_values(chart, edits)
    patches = []
    for idx in range(24):
        if idx not in values:
            continue
        xyz = values[idx]
        stock_xyz = _parse_xyz_string(info[idx]["xyz"]) if idx in info and info[idx].get("xyz") else xyz
        patches.append({
            "index": idx,
            "name": info.get(idx, {}).get("name", f"Patch {idx}"),
            "xyz": [round(float(v), 6) for v in xyz],
            "preview_srgb": list(_xyz_to_srgb8(xyz)),
            "modified": _fmt_xyz(xyz) != _fmt_xyz(stock_xyz),
        })
    return {
        "format": FORMAT_ID,
        "chart": chart.name,
        "chart_type": chart.ctype,
        "encoding": "CIEXYZ",
        "patch_count": len(patches),
        "patches": patches,
    }


def build_resolve_xml(chart, edits=None):
    values = _merged_xyz_values(chart, edits)
    xml, _changed = _replace_xyz_values(chart.xml, values, 6)
    return xml


def write_export_json(path, chart, edits=None):
    out = Path(path)
    if out.parent != Path("."):
        out.parent.mkdir(parents=True, exist_ok=True)
    payload = build_canonical_json(chart, edits)
    out.write_text(json.dumps(payload, indent=2) + "\n")


def write_export_xml(path, chart, edits=None):
    out = Path(path)
    if out.parent != Path("."):
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(build_resolve_xml(chart, edits))


class TerminalSession:
    """Tiny dependency-free TUI layer with ANSI truecolor and arrow keys."""
    def __init__(self):
        self.sysname = _sysname()
        self.old_attrs = None
        self.old_winch_handler = None
        self.fd = None
        self.last_size = _terminal_size((100, 32))

    def __enter__(self):
        if self.sysname == "Windows":
            self._enable_windows_vt()
        else:
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self.old_attrs = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            if hasattr(signal, "SIGWINCH"):
                self.old_winch_handler = signal.getsignal(signal.SIGWINCH)
                signal.signal(signal.SIGWINCH, _mark_tui_resized)
        self.write("\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1006h\x1b[2J\x1b[H")
        return self

    def __exit__(self, exc_type, exc, tb):
        cleanup_error = None
        try:
            self.write("\x1b[0m\x1b[?1006l\x1b[?1000l\x1b[?25h\x1b[?1049l")
        except Exception as e:
            cleanup_error = e
        if self.old_attrs is not None:
            import termios
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_attrs)
            except Exception as e:
                cleanup_error = cleanup_error or e
        if self.old_winch_handler is not None and hasattr(signal, "SIGWINCH"):
            try:
                signal.signal(signal.SIGWINCH, self.old_winch_handler)
            except Exception as e:
                cleanup_error = cleanup_error or e
        if cleanup_error is not None:
            _log_tui_exception(cleanup_error)

    def _enable_windows_vt(self):
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass

    def write(self, s):
        sys.stdout.write(s)
        sys.stdout.flush()

    def read_key(self):
        global _TUI_RESIZED
        if self.sysname == "Windows":
            import msvcrt
            while not msvcrt.kbhit():
                if self._terminal_resized():
                    return "resize"
                time.sleep(0.1)
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                ch2 = msvcrt.getwch()
                return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(ch2, ch2)
            if ch == "\r":
                return "enter"
            if ch == "\x1b":
                return "esc"
            return ch

        import select
        while True:
            if _TUI_RESIZED:
                _TUI_RESIZED = False
                self.last_size = _terminal_size((100, 32))
                return "resize"
            if self._terminal_resized():
                return "resize"
            if select.select([self.fd], [], [], 0.2)[0]:
                break
        b = os.read(self.fd, 1)
        if not b:
            return ""
        if b == b"\x03":
            raise KeyboardInterrupt
        if b in (b"\r", b"\n"):
            return "enter"
        if b != b"\x1b":
            return b.decode("utf-8", "ignore")
        seq = b""
        while select.select([self.fd], [], [], 0.03)[0]:
            seq += os.read(self.fd, 1)
        mouse = _decode_mouse_sequence(seq)
        if mouse:
            return mouse
        return {
            b"[A": "up", b"[B": "down", b"[D": "left", b"[C": "right",
            b"OA": "up", b"OB": "down", b"OD": "left", b"OC": "right",
            b"[Z": "shift-tab",
        }.get(seq, "esc")

    def _terminal_resized(self):
        size = _terminal_size((100, 32))
        if size != self.last_size:
            self.last_size = size
            return True
        return False

    def prompt(self, label):
        rows = _terminal_size((100, 32)).lines
        self.write(f"\x1b[{rows};1H\x1b[2K\x1b[?25h{label}")
        if self.old_attrs is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_attrs)
        try:
            ans = input()
        finally:
            if self.old_attrs is not None:
                import tty
                tty.setcbreak(self.fd)
            self.write("\x1b[?25l")
        return ans


def _decode_mouse_sequence(seq):
    m = MOUSE_RE.fullmatch(seq)
    if not m:
        return None
    button, x, y = (int(m.group(i)) for i in (1, 2, 3))
    pressed = m.group(4) == b"M"
    return ("mouse", x, y, button, pressed)


def _tui_order_base_charts(charts):
    ordered = []
    seen = set()
    by_type = {}
    for chart in charts:
        by_type.setdefault(chart.ctype, chart)
    for ctype, _label in CHART_UI_LABEL_ORDER:
        chart = by_type.get(ctype)
        if chart and chart.off not in seen:
            ordered.append(chart)
            seen.add(chart.off)
    for chart in charts:
        if chart.off not in seen:
            ordered.append(chart)
            seen.add(chart.off)
    return ordered or list(charts)


def _tui_chart_index(charts, chart):
    for idx, candidate in enumerate(charts):
        if candidate.off == chart.off:
            return idx
    return 0


def _tui_chart_index_by_type(charts, ctype):
    for idx, candidate in enumerate(charts):
        if candidate.ctype == str(ctype):
            return idx
    return None


def _tui_chart_info_stock(chart):
    info = _extract_patch_info(chart.xml)
    stock = {idx: _parse_xyz_string(info[idx]["xyz"]) for idx in info if info[idx].get("xyz")}
    return info, stock


def run_tui(path, chart, args, charts=None):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        die("--tui requires an interactive terminal.")

    base_charts = _tui_order_base_charts(charts or [chart])
    chart_index = _tui_chart_index(base_charts, chart)
    info, stock = _tui_chart_info_stock(chart)
    selected = 0
    edits = {}
    table_rows = list_all_tables(args.table_dir)
    table_index = -1
    active_table = None
    active_table_row = None
    status = "Ready."

    def switch_base_type(ctype):
        nonlocal chart_index, chart, info, stock, selected
        idx = _tui_chart_index_by_type(base_charts, ctype)
        if idx is None:
            return False
        chart_index = idx
        chart = base_charts[chart_index]
        info, stock = _tui_chart_info_stock(chart)
        selected = _clamp_patch_index(selected)
        return True

    def finish_loaded_table(row, loaded_status):
        nonlocal active_table, active_table_row
        active_table = row[0]
        active_table_row = row
        preferred = _preferred_base_type_for_table_row(row)
        if preferred and chart.ctype != str(preferred):
            if not switch_base_type(preferred):
                return f"{loaded_status} preferred base missing: {_slot_hint(preferred)}."
            try:
                _tui_load_table_row(row, chart, edits, args.table_dir)
            except Exception as e:
                return _tui_error_status("Table loaded, preferred base reload failed", e)
            return f"{loaded_status} base: {_slot_hint(chart.ctype)}."
        return loaded_status

    def clear_active_table():
        nonlocal active_table, active_table_row
        active_table = None
        active_table_row = None

    def step_table(direction):
        nonlocal table_rows, table_index
        table_rows = list_all_tables(args.table_dir)
        table_index, loaded_status = _tui_step_table(
            chart, edits, table_rows, table_index, direction, args.table_dir)
        if loaded_status.startswith("Loaded") and 0 <= table_index < len(table_rows):
            return finish_loaded_table(table_rows[table_index], loaded_status)
        return loaded_status

    with TerminalSession() as term:
        layout = []
        while True:
            try:
                selected = _clamp_patch_index(selected)
                layout = _draw_tui(term, path, chart, info, stock, edits, selected, status, active_table)
                try:
                    key = term.read_key()
                except KeyboardInterrupt:
                    return None

                if isinstance(key, tuple) and key and key[0] == "mouse":
                    _, x, y, button, pressed = key
                    if pressed and button == 0:
                        hit = _hit_test_patch(layout, x, y)
                        if hit is not None:
                            selected = hit
                            status = f"Selected patch {hit}. Press Enter/e/m to edit it."
                    continue

                if key in ("q", "esc"):
                    if edits:
                        ans = term.prompt("Discard staged edits? [y/N] ").strip().lower()
                        if ans not in ("y", "yes"):
                            status = "Quit cancelled."
                            continue
                    return None
                if key in ("?", "h"):
                    _draw_help(term)
                    term.read_key()
                    continue
                if key == "resize":
                    continue
                grid_cols = _tui_grid_columns(_terminal_size((110, 34)).columns)
                if key == "left":
                    selected = _move_patch_selection(selected, -1, 0, grid_cols)
                elif key in ("right", "l"):
                    selected = _move_patch_selection(selected, 1, 0, grid_cols)
                elif key in ("up", "k"):
                    selected = _move_patch_selection(selected, 0, -1, grid_cols)
                elif key in ("down", "j"):
                    selected = _move_patch_selection(selected, 0, 1, grid_cols)
                elif key in ("enter", "e", "m"):
                    status = _tui_edit_patch(term, selected, info, stock, edits)
                    clear_active_table()
                elif key == "r":
                    if selected in edits:
                        del edits[selected]
                        clear_active_table()
                        status = f"Reset patch {selected}."
                    else:
                        status = f"Patch {selected} is already stock."
                elif key == "R":
                    if term.prompt("Reset all staged edits? [y/N] ").strip().lower() in ("y", "yes"):
                        edits.clear()
                        clear_active_table()
                        status = "All staged edits cleared."
                elif key == "b":
                    chart_index = (chart_index + 1) % len(base_charts)
                    chart = base_charts[chart_index]
                    info, stock = _tui_chart_info_stock(chart)
                    selected = _clamp_patch_index(selected)
                    if active_table_row:
                        try:
                            _tui_load_table_row(active_table_row, chart, edits, args.table_dir)
                            status = f"Switched base to {_slot_hint(chart.ctype)}. Reloaded table."
                        except Exception as e:
                            status = _tui_error_status("Base chart switched, table reload failed", e)
                    else:
                        status = f"Switched base to {_slot_hint(chart.ctype)}. Kept {len(edits)} staged value(s)."
                elif key == "x":
                    status = _tui_export_json(term, chart, edits)
                elif key == "X":
                    status = _tui_export_xml(term, chart, edits)
                elif key == "i":
                    status = _tui_import_json(term, chart, edits)
                    clear_active_table()
                elif key == "o":
                    status = _tui_import_xml(term, chart, edits)
                    clear_active_table()
                elif key == "p":
                    picked_row, status = _tui_import_table(term, chart, edits, args.table_dir)
                    table_rows = list_all_tables(args.table_dir)
                    if picked_row:
                        for idx, row in enumerate(table_rows):
                            if row == picked_row:
                                table_index = idx
                                break
                        if status.startswith("Loaded"):
                            status = finish_loaded_table(picked_row, status)
                elif key == "w":
                    status = step_table(1)
                elif key == "s":
                    status = step_table(-1)
                elif key == "a":
                    if not edits and not args.rename_label:
                        status = "Nothing to apply. Edit at least one patch first."
                        continue
                    target = f"{len(edits)} staged patch(es)"
                    if args.rename_label:
                        target += " and label rename"
                    ans = term.prompt(f"Apply {target} to Resolve binary? [y/N] ").strip().lower()
                    if ans in ("y", "yes"):
                        return chart, dict(edits)
                    status = "Apply cancelled."
            except SystemExit as e:
                status = _tui_error_status("TUI error", e)
            except Exception as e:
                status = _tui_error_status("TUI error", e)


def _draw_tui(term, path, chart, info, stock, edits, selected, status, active_table=None):
    size = _terminal_size((110, 34))
    width = max(1, min(size.columns, 120))
    cols = _tui_grid_columns(size.columns)
    cell_w = max(1, min(18, (width - (cols - 1)) // cols))
    layout = []
    selected = _clamp_patch_index(selected)
    selected_name = info.get(selected, {}).get("name", f"Patch {selected}")
    status_text = _clip_text(status, width)
    staged_table = active_table or "-"
    context = f"base: {_slot_hint(chart.ctype)}    table: {staged_table}"
    lines = [
        _ansi_bold("colortablething"),
        _ansi_dim(_clip_text(context, width)),
        status_text,
        "",
    ]
    grid_start_y = len(lines) + 1

    for row_start in range(0, 24, cols):
        parts = []
        row = row_start // cols
        for col, idx in enumerate(range(row_start, min(row_start + cols, 24))):
            name = info.get(idx, {}).get("name", f"Patch {idx}")
            xyz = edits.get(idx, stock.get(idx, (0.0, 0.0, 0.0)))
            rgb = _xyz_to_srgb8(xyz)
            mark = "*" if idx in edits else " "
            label = _clip_text(name, max(0, cell_w - 4))
            cell_text = _clip_text(f"{mark}{idx:02d} {label}", cell_w).ljust(cell_w)
            cell = _ansi_bg(rgb, cell_text)
            if idx == selected:
                cell = f"\x1b[1;4m{cell}\x1b[0m"
            parts.append(cell)
            x1 = 1 + col * (cell_w + 1)
            x2 = x1 + cell_w - 1
            y = grid_start_y + row * 2
            layout.append((x1, x2, y, idx))
        lines.append(" ".join(parts))
        lines.append("")

    stock_xyz = stock.get(selected, (0.0, 0.0, 0.0))
    current_xyz = edits.get(selected, stock_xyz)
    current_rgb = _xyz_to_srgb8(current_xyz)
    stock_rgb = _xyz_to_srgb8(stock_xyz)
    is_modified = selected in edits
    delta = tuple(current_xyz[i] - stock_xyz[i] for i in range(3))
    delta_text = ", ".join(f"{v:+.6f}" for v in delta)
    detail_title = f"Patch {selected:02d}  {selected_name}"
    if is_modified:
        detail_title += "  (modified)"
    lines += [
        "-" * min(width, 80),
        _ansi_bold(_clip_text(detail_title, width)),
        f"Stock   {_ansi_bg(stock_rgb, '      ')}  {_ansi_fg(stock_rgb, _rgb_hex(stock_rgb), bold=True)}  {_fmt_xyz(stock_xyz)}",
        f"Current {_ansi_bg(current_rgb, '      ')}  {_ansi_fg(current_rgb, _rgb_hex(current_rgb), bold=True)}  {_fmt_xyz(current_xyz)}",
        f"Delta   {delta_text}" if is_modified else _ansi_dim("Delta   no staged change"),
        "",
        _ansi_dim(f"{len(edits)} staged | b base | p presets | w next | s prev | e edit | a apply | h help | q"),
    ]
    term.write("\x1b[H\x1b[2J" + "\n".join(lines))
    return layout


def _clamp_patch_index(idx):
    try:
        idx = int(idx)
    except Exception:
        return 0
    return max(0, min(23, idx))


def _tui_grid_columns(width):
    width = max(1, int(width or 1))
    for cols in (6, 4, 3, 2):
        if (width - (cols - 1)) // cols >= 12:
            return cols
    return 1


def _move_patch_selection(idx, dx, dy, cols=6):
    idx = _clamp_patch_index(idx)
    cols = max(1, min(6, int(cols or 6)))
    rows = (24 + cols - 1) // cols
    row, col = divmod(idx, cols)
    row = max(0, min(rows - 1, row + dy))
    row_len = min(cols, 24 - row * cols)
    col = max(0, min(row_len - 1, col + dx))
    return _clamp_patch_index(row * cols + col)


def _hit_test_patch(layout, x, y):
    for x1, x2, row, idx in layout:
        if 0 <= idx <= 23 and row == y and x1 <= x <= x2:
            return idx
    return None


def _draw_help(term):
    term.write("\x1b[H\x1b[2J" + "\n".join([
        _ansi_bold("colortablething"),
        _ansi_dim("Same patching engine, cleaner editor view."),
        "",
        "Navigation",
        "  arrows, j/k/l         move selection",
        "  h, ?                  show help",
        "  mouse click           select a swatch",
        "  b                     switch to next base chart",
        "",
        "Editing",
        "  Enter, e, m           edit selected patch XYZ",
        "  r                     reset selected patch",
        "  R                     reset all staged edits",
        "",
        "Import / Export",
        "  p                     open presets/table picker",
        "  w                     load next preset/table",
        "  s                     load previous preset/table",
        "                        picker: arrows move, a add, r rename, d delete",
        "  i                     import JSON edits",
        "  o                     import Resolve XML edits",
        "  x                     export canonical JSON",
        "  X                     export Resolve XML",
        "",
        "Adding Tables",
        "  In the preset picker, press a to save a JSON/XML/CSV table into the app library.",
        "  Resolve XML needs <colorchart> with 24 <grid index=\"N\"> entries.",
        "  Each grid should contain <color xyz=\"X, Y, Z\"/>. N is 0..23.",
        "  RGB table XML with <color no=\"001\"><R>...</R><G>...</G><B>...</B> also works.",
        "",
        "Apply / Quit",
        "  a                     apply staged edits to Resolve binary",
        "  q, Esc                quit without applying",
        "",
        "Skip The TUI",
        "  python3 colortablething.py --base classic --table \"AliExpress 8.5x5.8\" --dry-run",
        "  python3 colortablething.py --base classic --table /path/to/mychart.xml --dry-run",
        "  python3 colortablething.py --base classic --xml /path/to/mychart.xml --yes",
        "  --base chooses the Resolve slot; --table/--xml chooses the values to swap in.",
        "  Use --dry-run to preview. Use --yes to write without the prompt.",
        "",
        _ansi_dim("Terminal colors are approximate previews. Resolve Color Match uses XYZ."),
        "",
        "Press any key to return.",
    ]))


def _tui_edit_patch(term, idx, info, stock, edits):
    cur = edits.get(idx, stock.get(idx, (0.0, 0.0, 0.0)))
    name = info.get(idx, {}).get("name", f"Patch {idx}")
    ans = term.prompt(f"XYZ for {idx} {name} [{_fmt_xyz(cur)}]: ").strip()
    if not ans:
        return "Edit cancelled."
    try:
        xyz = _xyz_or_error(ans.split(",") if "," in ans else ans.split())
    except Exception as e:
        return f"Invalid XYZ: {e}"
    edits[idx] = xyz
    return f"Staged patch {idx}: {_fmt_xyz(xyz)}"


def _tui_export_json(term, chart, edits):
    path = term.prompt("Export JSON path [colorchecker_edits.json]: ").strip() or "colorchecker_edits.json"
    try:
        write_export_json(path, chart, edits)
        return f"Saved full canonical JSON to {path} ({len(edits)} staged edit(s))."
    except OSError as e:
        return f"Export failed: {e}"


def _tui_export_xml(term, chart, edits):
    path = term.prompt("Export XML path [colorchecker_chart.xml]: ").strip() or "colorchecker_chart.xml"
    try:
        write_export_xml(path, chart, edits)
        return f"Saved Resolve-style XML to {path} ({len(edits)} staged edit(s))."
    except OSError as e:
        return f"XML export failed: {e}"


def _tui_import_json(term, chart, edits):
    path = term.prompt("Import JSON path: ").strip()
    if not path:
        return "Import cancelled."
    try:
        obj = json.loads(Path(path).read_text())
        imported = _edits_from_json_obj(obj)
        imported = _filter_unchanged_edits(chart.xml, imported)
        edits.update(imported)
        return f"Imported {len(imported)} edit(s)."
    except Exception as e:
        return f"Import failed: {e}"


def _tui_import_xml(term, chart, edits):
    path = term.prompt("Import Resolve colorchart XML path: ").strip()
    if not path:
        return "XML import cancelled."
    try:
        imported = _edits_from_xml_bytes(Path(path).read_bytes())
        imported = _filter_unchanged_edits(chart.xml, imported)
        edits.update(imported)
        return f"Imported {len(imported)} XML patch value(s)."
    except Exception as e:
        return f"XML import failed: {e}"


def _tui_import_table(term, chart, edits, table_dir=None):
    try:
        row, pick_status = _tui_pick_table(term, table_dir)
        if not row:
            return None, pick_status
        return row, _tui_load_table_row(row, chart, edits, table_dir)
    except SystemExit as e:
        return None, _tui_error_status("Table load failed", e)
    except Exception as e:
        return None, _tui_error_status("Table load failed", e)


def _tui_load_table(name, chart, edits, table_dir=None):
    imported = _edits_from_table(name, table_dir)
    return _tui_stage_table_edits(name, imported, chart, edits)


def _tui_load_table_row(row, chart, edits, table_dir=None):
    name, kind, path = row
    if kind == "built-in":
        imported = _edits_from_builtin(name)
    elif kind == "extra":
        if not path:
            raise ValueError(f"extra table {name!r} is missing its XML path")
        imported = _edits_from_xml_bytes(path.read_bytes())
    elif kind == "user":
        if not path:
            raise ValueError(f"user table {name!r} is missing its JSON path")
        imported = _edits_from_json_obj(json.loads(path.read_text()))
    else:
        imported = _edits_from_table(name, table_dir)
    return _tui_stage_table_edits(name, imported, chart, edits)


def _tui_stage_table_edits(name, imported, chart, edits):
    imported = _filter_unchanged_edits(chart.xml, imported)
    edits.clear()
    edits.update(imported)
    return f"Loaded {len(imported)} changed patch value(s)."


def _tui_step_table(chart, edits, table_rows, current_index, step, table_dir=None):
    if not table_rows:
        return -1, "No tables available."
    next_index = (current_index + step) % len(table_rows)
    row = table_rows[next_index]
    try:
        return next_index, _tui_load_table_row(row, chart, edits, table_dir)
    except SystemExit as e:
        return next_index, _tui_error_status("Table switch failed", e)
    except Exception as e:
        return next_index, _tui_error_status("Table switch failed", e)


def _tui_error_status(prefix, exc):
    _log_tui_exception(exc)
    if isinstance(exc, SystemExit):
        return f"{prefix}: internal exit {exc.code}. See tui-error.log."
    return f"{prefix}: {type(exc).__name__}: {exc}. See tui-error.log."


def _log_tui_exception(exc):
    try:
        log_dir = _real_user_home() / ".local/share/colorchecker_patch"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "tui-error.log"
        with log_path.open("a", encoding="utf-8") as f:
            import traceback

            f.write("\n--- TUI exception %s ---\n" % _timestamp_seconds())
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    except Exception:
        pass


def _tui_pick_table(term, table_dir=None):
    rows = list_all_tables(table_dir)
    selected = 0
    status = "Enter loads. Numbers select visible rows. a adds. r/d manage user presets."
    while True:
        if rows:
            selected = max(0, min(selected, len(rows) - 1))
        else:
            selected = 0
        layout = _draw_table_picker(term, rows, selected, status)
        try:
            key = term.read_key()
        except KeyboardInterrupt:
            return None, "Table load cancelled."

        if isinstance(key, tuple) and key and key[0] == "mouse":
            _, x, y, button, pressed = key
            if pressed and button == 0:
                hit = _hit_test_table(layout, x, y)
                if hit is not None:
                    selected = hit
                    if 0 <= selected < len(rows):
                        status = f"Selected {rows[selected][0]}. Press Enter to load."
            continue

        if key in ("q", "esc"):
            return None, "Table load cancelled."
        if key == "resize":
            continue
        if key in ("up", "k"):
            if rows:
                selected = max(0, selected - 1)
            continue
        if key in ("down", "j"):
            if rows:
                selected = min(len(rows) - 1, selected + 1)
            continue
        if key == "left":
            if rows:
                selected = max(0, selected - 10)
            continue
        if key in ("right", "l"):
            if rows:
                selected = min(len(rows) - 1, selected + 10)
            continue
        if isinstance(key, str) and key.isdigit() and key != "0":
            if layout:
                slot = int(key) - 1
                if 0 <= slot < len(layout):
                    selected = layout[slot][3]
                    status = f"Selected {rows[selected][0]}. Press Enter to load."
                else:
                    status = f"No visible table at number {key}."
            continue
        if key in ("?", "h"):
            _draw_table_help(term)
            term.read_key()
            continue
        if key == "enter":
            if not rows:
                return None, "No tables available."
            return rows[selected], f"Selected table: {rows[selected][0]}"
        if key == "a":
            status = _tui_add_table(term, table_dir)
            rows = list_all_tables(table_dir)
            selected = max(0, min(selected, len(rows) - 1)) if rows else 0
            continue
        if key in ("r", "R"):
            status = _tui_rename_selected_table(term, rows, selected, table_dir)
            rows = list_all_tables(table_dir)
            selected = max(0, min(selected, len(rows) - 1)) if rows else 0
            continue
        if key in ("d", "D"):
            status = _tui_delete_selected_table(term, rows, selected, table_dir)
            rows = list_all_tables(table_dir)
            selected = max(0, min(selected, len(rows) - 1)) if rows else 0


def _draw_table_picker(term, rows, selected, status):
    size = _terminal_size((100, 32))
    width = max(1, min(size.columns, 120))
    visible = max(8, min(size.lines - 9, 18))
    start = 0
    if rows:
        start = max(0, min(selected - visible // 2, len(rows) - visible))
    shown = rows[start:start + visible]
    lines = [
        _ansi_bold("Presets"),
        _ansi_dim("Built-ins are read-only. User presets can be renamed or deleted."),
        _clip_text(f"Status: {status}", width),
        "",
    ]
    layout = []
    for pos, (name, kind, path) in enumerate(shown, start=start):
        marker = ">" if pos == selected else " "
        visible_num = pos - start + 1
        key_hint = str(visible_num) if visible_num <= 9 else " "
        label = f"{marker} {key_hint:>1} {kind:<7} {_clip_text(name, width - 28)}"
        if kind in ("user", "extra") and path:
            label += " " + _clip_text(path.name, 18)
        label = _clip_text(label, width)
        if pos == selected:
            label = f"\x1b[7m{label.ljust(width)}\x1b[0m"
        lines.append(label)
        layout.append((1, width, len(lines), pos))
    if not rows:
        lines.append(_ansi_dim("No tables found."))
    lines += [
        "",
        _ansi_dim(_clip_text("Enter load | arrows/j/k move | 1-9 select | a add | r rename | d delete | h help | q", width)),
    ]
    term.write("\x1b[H\x1b[2J" + "\n".join(lines))
    return layout


def _draw_table_help(term):
    term.write("\x1b[H\x1b[2J" + "\n".join([
        _ansi_bold("Preset Picker Help"),
        "",
        "Keys",
        "  Enter                 load selected table",
        "  arrows, j/k           move selection",
        "  1-9                   select visible row",
        "  a                     add a preset/table to the user library",
        "  r                     rename selected user table",
        "  d                     delete selected user table",
        "  q                     return without loading",
        "",
        "Add Preset",
        "  Press a, paste a JSON/XML/CSV path, then give it a name.",
        "  The preset is copied into ~/.local/share/colorchecker_patch/tables.",
        "  Built-ins are read-only; added user presets can be renamed/deleted.",
        "",
        "Resolve XML Format",
        "  Root: <colorchart ...>",
        "  Needs 24 grids with index=\"0\" through index=\"23\".",
        "  Each grid contains a color with xyz=\"X, Y, Z\".",
        "",
        "Minimal Shape",
        "  <colorchart name=\"My Chart\" type=\"7\">",
        "    <grid index=\"0\" name=\"Dark Skin\"><color xyz=\"0.111, 0.101, 0.070\"/></grid>",
        "    ... 22 more grids ...",
        "    <grid index=\"23\" name=\"Black\"><color xyz=\"0.030, 0.032, 0.035\"/></grid>",
        "  </colorchart>",
        "",
        "RGB XML Also Works",
        "  <color no=\"001\"><R>115</R><G>82</G><B>69</B></color>",
        "  no=\"001\" maps to patch index 0; no=\"024\" maps to index 23.",
        "",
        "Press any key to return.",
    ]))


def _hit_test_table(layout, x, y):
    for x1, x2, row, idx in layout:
        if row == y and x1 <= x <= x2:
            return idx
    return None


def _tui_add_table(term, table_dir=None):
    source = term.prompt("Add preset file path (JSON/XML/CSV; h in picker explains XML): ").strip()
    if not source:
        return "Add cancelled."
    name = term.prompt("Table name [file name]: ").strip() or None
    try:
        out, table_name = add_user_table(source, name, table_dir)
        return f"Added user table: {table_name} ({out.name})"
    except Exception as e:
        return f"Add failed: {e}"


def _tui_rename_selected_table(term, rows, selected, table_dir=None):
    if not rows or not 0 <= selected < len(rows):
        return "No table selected."
    name, kind, _path = rows[selected]
    if kind != "user":
        return "Built-in tables cannot be renamed."
    new_name = term.prompt(f"Rename {name!r} to: ").strip()
    if not new_name:
        return "Rename cancelled."
    try:
        old, out = rename_user_table(name, new_name, table_dir)
        return f"Renamed user table: {old} -> {new_name} ({out.name})"
    except Exception as e:
        return f"Rename failed: {e}"


def _tui_delete_selected_table(term, rows, selected, table_dir=None):
    if not rows or not 0 <= selected < len(rows):
        return "No table selected."
    name, kind, _path = rows[selected]
    if kind != "user":
        return "Built-in tables cannot be deleted."
    ans = term.prompt(f"Delete user table {name!r}? Type DELETE to confirm: ").strip()
    if ans != "DELETE":
        return "Delete cancelled."
    try:
        old, path = remove_user_table(name, table_dir)
        return f"Deleted user table: {old} ({path.name})"
    except Exception as e:
        return f"Delete failed: {e}"


def _fmt_xyz(xyz):
    return "%.6f, %.6f, %.6f" % xyz


def _fmt_rgb(rgb):
    if isinstance(rgb, str):
        parts = [p for p in re.split(r"[,\s]+", rgb.strip()) if p]
        if len(parts) != 3:
            raise ValueError(f"expected 3 RGB components, got {rgb!r}")
        return "%d, %d, %d" % tuple(int(float(p)) for p in parts)
    return "%d, %d, %d" % tuple(int(v) for v in rgb)


def _filter_unchanged_edits(xml_bytes, edits):
    stock = _extract_values(xml_bytes)
    return {
        idx: xyz
        for idx, xyz in edits.items()
        if stock.get(idx) != _fmt_xyz(xyz)
    }


def chart_ui_label(chart):
    return CHART_UI_LABELS.get(chart.ctype, chart.name)


def _rename_chart_xml(xml_bytes, new_name):
    repl = b'<colorchart name="' + new_name.encode("utf-8") + b'"'
    new_xml, n = re.subn(rb'<colorchart\s+name="[^"]*"', repl, xml_bytes, count=1)
    return new_xml, n > 0


def _label_patch_plan(data, old_label, new_label, ctype=None):
    new = new_label.encode("utf-8")
    if ctype is not None:
        slots = _label_slot_layout(data)
        slot = slots.get(str(ctype))
        if slot:
            off, max_len, current = slot
            if len(new) > max_len:
                raise ValueError(
                    f"label too long for this Resolve slot: {len(new)} bytes > {max_len} bytes. "
                    f"Use a shorter name, e.g. 'AliExpress 8.5x5.8'."
                )
            old_needle = data[off:off + max_len + 1]
            new_needle = new + b"\x00" + (b"\x00" * (max_len - len(new)))
            if old_needle == new_needle:
                return [], old_needle, new_needle
            return [off], old_needle, new_needle

    old = old_label.encode("utf-8")
    if len(new) > len(old):
        raise ValueError(
            f"label too long for this Resolve slot: {len(new)} bytes > {len(old)} bytes. "
            f"Use a shorter name, e.g. 'AliExpress 8.5x5.8'."
        )
    old_needle = old + b"\x00"
    new_needle = new + b"\x00" + (b"\x00" * (len(old) - len(new)))
    offsets = [m.start() for m in re.finditer(re.escape(old_needle), data)]
    if offsets:
        return offsets, old_needle, new_needle
    if new_needle in data:
        return [], old_needle, new_needle
    raise ValueError(f"could not find UI label {old_label!r} in Resolve binary")


def _label_slot_layout(data):
    offsets = {}
    pos = 0
    for ctype, label in CHART_UI_LABEL_ORDER:
        offsets[ctype] = pos
        pos += len(label.encode("utf-8")) + 1

    candidates = []
    for ctype, label in CHART_UI_LABEL_ORDER:
        needle = label.encode("utf-8") + b"\x00"
        for m in re.finditer(re.escape(needle), data):
            base = m.start() - offsets[ctype]
            if base < 0:
                continue
            slots = {}
            valid = 0
            exact = 0
            for slot_type, slot_label in CHART_UI_LABEL_ORDER:
                max_len = len(slot_label.encode("utf-8"))
                off = base + offsets[slot_type]
                current = _read_label_slot(data, off, max_len)
                if current is None:
                    break
                valid += 1
                if current == slot_label:
                    exact += 1
                slots[slot_type] = (off, max_len, current)
            if valid == len(CHART_UI_LABEL_ORDER):
                candidates.append((exact, base, slots))
    if not candidates:
        return {}
    strong = [c for c in candidates if c[0] >= 2]
    exact, base, slots = max(strong or candidates, key=lambda item: (item[0], -item[1]))
    return slots


def _read_label_slot(data, off, max_len):
    if off < 0 or off + max_len >= len(data):
        return None
    end = data.find(b"\x00", off, off + max_len + 1)
    if end < 0:
        return None
    return data[off:end].decode("utf-8", "replace")


def _write_label_patch(f, offsets, new_needle):
    for off in offsets:
        f.seek(off)
        f.write(new_needle)


def print_slots(charts, data):
    labels = _label_slot_layout(data)
    by_type = {c.ctype: c for c in charts}
    print("Resolve Color Match slots:")
    for ctype, nominal in CHART_UI_LABEL_ORDER:
        chart = by_type.get(ctype)
        current_label = labels.get(ctype, (None, None, nominal))[2]
        slot_name = _slot_hint(ctype)
        embedded = chart.name if chart else "missing embedded XML stream"
        extra = ""
        if current_label != nominal:
            extra = f"  nominal={nominal!r}"
        print(f"  - {slot_name:<9} type={ctype}  ui={current_label!r}  xml={embedded!r}{extra}")


def _slot_hint(ctype):
    hints = {
        "1": "legacy",
        "2": "spyder",
        "3": "smpte",
        "4": "chroma",
        "5": "video",
        "6": "passport",
        "7": "classic",
    }
    return hints.get(str(ctype), str(ctype))


def restore_chart_slots(backup_dir, path, slot_types, force=False, wait_resolve=False,
                        kill_resolve=False, yes=False, dry_run=False):
    bk = latest_backup(backup_dir)
    if not bk:
        die(f"No backup found in {backup_dir}.")
    try:
        current_data = Path(path).read_bytes()
        backup_data = Path(bk).read_bytes()
    except OSError as e:
        die(f"Could not read binary/backup: {e}")

    current_charts = {c.ctype: c for c in find_charts(current_data)}
    backup_charts = {c.ctype: c for c in find_charts(backup_data)}
    current_labels = _label_slot_layout(current_data)
    backup_labels = _label_slot_layout(backup_data)

    stream_plans = []
    label_plans = []
    for ctype in slot_types:
        ctype = str(ctype)
        cur = current_charts.get(ctype)
        old = backup_charts.get(ctype)
        if not cur:
            die(f"Current binary does not contain chart type {ctype}.")
        if not old:
            die(f"Backup {bk} does not contain chart type {ctype}.")
        if cur.off != old.off:
            die(
                f"Backup layout for chart type {ctype} does not match current binary. "
                f"Current off={cur.off}, backup off={old.off}."
            )
        if cur.off + old.comp_stored > len(current_data):
            die(f"Backup slot for chart type {ctype} would run past the current binary size.")
        stream_plans.append((ctype, cur.off, old.comp_stored, old.name))

        cur_label = current_labels.get(ctype)
        old_label = backup_labels.get(ctype)
        if cur_label and old_label:
            cur_off, cur_len, _cur_text = cur_label
            old_off, old_len, old_text = old_label
            if cur_off == old_off and cur_len == old_len:
                label_plans.append((ctype, cur_off, cur_len + 1, old_text))
            else:
                warn(f"Skipping UI label restore for type {ctype}; label slot layout differs from backup.")
        else:
            warn(f"Skipping UI label restore for type {ctype}; could not locate label slot in current binary/backup.")

    print(f"Backup       : {bk}")
    for ctype, off, slot_len, old_name in stream_plans:
        print(f"Restore slot : {_slot_hint(ctype)} (type={ctype}) stream @ {off}, {slot_len} bytes, xml={old_name!r}")
    for ctype, off, length, old_text in label_plans:
        print(f"Restore label: {_slot_hint(ctype)} (type={ctype}) @ {off}, {length} bytes -> {old_text!r}")

    if dry_run:
        print("\n--dry-run: no changes written.")
        return

    ensure_resolve_closed(force=force, wait=wait_resolve, kill=kill_resolve, yes=yes, action="slot restore")
    if not can_write(path):
        escalate(path, _raw_argv())
    safety = make_backup(path, backup_dir)
    print(f"Safety backup: {safety}")

    with open(path, "r+b") as f:
        for _ctype, off, slot_len, _old_name in stream_plans:
            f.seek(off)
            f.write(backup_data[off:off + slot_len])
        for _ctype, off, length, _old_text in label_plans:
            f.seek(off)
            f.write(backup_data[off:off + length])
        f.flush()
        os.fsync(f.fileno())

    after = Path(path).read_bytes()
    for ctype, off, slot_len, _old_name in stream_plans:
        if after[off:off + slot_len] != backup_data[off:off + slot_len]:
            die(f"VERIFICATION FAILED: restored stream for type {ctype} differs from backup. Restore from {safety} !")
    for ctype, off, length, _old_text in label_plans:
        if after[off:off + length] != backup_data[off:off + length]:
            die(f"VERIFICATION FAILED: restored label for type {ctype} differs from backup. Restore from {safety} !")
    print("Verified     : selected chart slot data matches the backup.")


def restore(backup_dir, path, force=False, wait_resolve=False, kill_resolve=False, yes=False):
    bk = latest_backup(backup_dir)
    if not bk:
        die(f"No backup found in {backup_dir}.")
    ensure_resolve_closed(force=force, wait=wait_resolve, kill=kill_resolve, yes=yes, action="restore")
    if not can_write(path):
        escalate(path, _raw_argv())
    _copy_file(bk, path)
    print(f"Restored {path} from {bk}")


# ----------------------------------------------------------------------------
# Misc
# ----------------------------------------------------------------------------

def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


def die(msg, code=2):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def print_format_help():
    print(f"""Accepted ColorChecker patch formats

Canonical JSON ({FORMAT_ID})
  {{
    "format": "{FORMAT_ID}",
    "chart": "X-Rite ColorChecker",
    "encoding": "CIEXYZ",
    "patches": [
      {{"index": 0, "name": "Dark Skin", "xyz": [0.124176, 0.110316, 0.071575]}},
      {{"index": 1, "name": "Light Skin", "xyz": [0.418200, 0.377982, 0.277551]}}
    ]
  }}

Simple JSON variants accepted by --json and TUI import
  {{"0": [0.124176, 0.110316, 0.071575], "19": [0.634288, 0.670302, 0.724040]}}
  {{"0": {{"xyz": [0.124176, 0.110316, 0.071575]}}}}
  [[0.124176, 0.110316, 0.071575], [0.418200, 0.377982, 0.277551]]
  {{"values": {{"0": [0.124176, 0.110316, 0.071575]}}}}

CSV accepted by --csv
  index,x,y,z
  0,0.124176,0.110316,0.071575
  19,0.634288,0.670302,0.724040

CLI patch specs accepted by --set
  --set 0=0.124176,0.110316,0.071575 --set 19=0.634288,0.670302,0.724040

Built-in presets accepted by --preset and TUI built-in load
  --preset type8       # AliExpress 8.5x5.8
  --preset type9       # AliExpress Chart 2026
  --preset aliexpress  # alias for type8

App-side table library (adds tables to this patcher, not Resolve's internal dropdown)
  --add-table ali.xml --table-name "My Printed Chart"
  --rename-table "My Printed Chart" --to "AliExpress Print"
  --remove-table "AliExpress Print"
  --list-tables
  --base classic --table "My Printed Chart" --dry-run
  --base classic --table /path/to/mychart.xml --dry-run
  External dropdown XMLs are listed as read-only "extra" tables only when not already built in.

Resolve XML accepted by --xml and TUI XML import
  Any Resolve-style <colorchart> XML with <grid index="N"><color xyz="X, Y, Z"/>.
  Use 24 grids, index="0" through index="23". The color tag can have other
  attributes; only xyz is read for matching.
  Minimal shape:
    <colorchart name="My Chart" type="7">
      <grid index="0" name="Dark Skin"><color xyz="0.111, 0.101, 0.070"/></grid>
      ...
      <grid index="23" name="Black"><color xyz="0.030, 0.032, 0.035"/></grid>
    </colorchart>
  Also accepts RGB-table XML with <color no="001"><R>...</R><G>...</G><B>...</B>.
  no="001" maps to patch 0; no="024" maps to patch 23.

Round-trip commands
  --export-json chart.json                    # current chart -> canonical JSON
  --set 0=0.5,0.5,0.5 --export-json edit.json # staged edits -> canonical JSON
  --json edit.json --export-xml chart.xml     # canonical JSON -> Resolve XML
  --base classic --xml chart.xml --dry-run    # Resolve XML -> patch preview

Resolve UI chart aliases
  --base legacy    # Calibrite ColorChecker Classic - Legacy (embedded type 1)
  --base classic   # Calibrite ColorChecker Classic/current (embedded type 7)
  --chart is an alias for --base

Resolve UI label rename (same-or-shorter only)
  --base legacy --rename-label "AliExpress 8.5x5.8 Legacy"

Fixed Resolve slot workflow
  --list-slots
  --install-table-slot legacy --table aliexpress --rename-label "AliExpress 8.5x5.8 Legacy"
  --restore-slot legacy
  --restore-all-slots

True extra type-8 dropdown entry (Linux Resolve 21 layout)
  --install-type8                         # defaults to built-in aliexpress table
  --install-type8 --xml chart.xml          # custom type-8 data source
  --install-type8 --type8-label "My Chart" --type8-xml ~/.local/share/colorchecker_patch/mychart-type8.xml
  --restore-type8                          # removes hooks; leaves external XML/code cave data unused

Two extra dropdown entries
  --install-type9 --xml AliExpressChart2026.xml
  # type 8: AliExpress 8.5x5.8; type 9: AliExpress Chart 2026
""")


_RAW = None
def _raw_argv():
    return _RAW


def main():
    global _RAW
    _RAW = sys.argv[1:]
    ap = argparse.ArgumentParser(
        description="colortablething: edit DaVinci Resolve ColorChecker reference tables (multi-OS).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    src = ap.add_argument_group("input (one or more)")
    src.add_argument("--table", help="load a built-in/user table name or JSON/XML/CSV file path")
    src.add_argument("--json")
    src.add_argument("--csv")
    src.add_argument("--xml")
    src.add_argument("--preset", help="built-in preset name or alias")
    src.add_argument("--set", action="append", metavar="N=x,y,z")
    ap.add_argument("--chart", "--base", dest="chart", help="base Resolve chart/slot to target")
    ap.add_argument("--install-table-slot", metavar="SLOT", help="install staged edits into a named fixed Resolve slot")
    ap.add_argument("--binary", help="path to the Resolve binary")
    ap.add_argument("--list", action="store_true", help="list charts and exit")
    ap.add_argument("--list-slots", action="store_true", help="list fixed Resolve Color Match slots and labels")
    ap.add_argument("--print-current", action="store_true", help="print target chart's xyz values and exit")
    ap.add_argument("--tui", action="store_true", help="open an interactive color-preview editor")
    ap.add_argument("--export-json", metavar="PATH", help="write full chart values as canonical JSON and exit")
    ap.add_argument("--export-xml", metavar="PATH", help="write full chart values as Resolve-style XML and exit")
    ap.add_argument("--format-help", action="store_true", help="print accepted input/output formats and exit")
    ap.add_argument("--list-presets", action="store_true", help="print built-in preset names and exit")
    ap.add_argument("--list-tables", action="store_true", help="print built-in and user-added tables and exit")
    ap.add_argument("--add-table", metavar="PATH", help="add JSON/XML/CSV table to the app-side table library and exit")
    ap.add_argument("--table-name", help="name to use with --add-table")
    ap.add_argument("--table-dir", default=str(DEFAULT_TABLE_DIR), help="user table directory")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--restore-slot", metavar="SLOT", help="restore one chart slot from the latest backup")
    ap.add_argument("--restore-all-slots", action="store_true", help="restore all chart slots from the latest backup")
    ap.add_argument("--install-type8", action="store_true", help="install a true extra Color Match dropdown entry backed by external type=8 XML")
    ap.add_argument("--install-type9", action="store_true", help="install two extra entries: type 8 default AliExpress and type 9 from supplied data")
    ap.add_argument("--restore-type8", action="store_true", help="remove the type=8 binary hooks")
    ap.add_argument("--type8-label", default=TYPE8_DEFAULT_LABEL, help="visible label for --install-type8")
    ap.add_argument("--type8-xml", help="external XML path used by --install-type8")
    ap.add_argument("--type9-label", default=TYPE9_DEFAULT_LABEL, help="visible label for --install-type9")
    ap.add_argument("--type9-xml", help="external type-9 XML path used by --install-type9")
    ap.add_argument("--backup-dir", default=str(Path.home() / ".local/share/colorchecker_backups"))
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--wait-resolve", action="store_true", help="wait until Resolve exits before patch/restore")
    ap.add_argument("--kill-resolve", action="store_true", help="warn, terminate Resolve, then patch/restore")
    ap.add_argument("--rename-label", help="rename the visible Resolve dropdown label for the selected chart slot")
    ap.add_argument("--rename-table", help="rename an app-side user table")
    ap.add_argument("--remove-table", help="delete an app-side user table")
    ap.add_argument("--to", help="new name for --rename-table")
    args = ap.parse_args()

    if not _RAW:
        if sys.stdin.isatty() and sys.stdout.isatty():
            args.tui = True
        else:
            ap.print_help()
            return

    if args.format_help:
        print_format_help()
        return

    if args.list_presets:
        print("Built-in presets:")
        for name in list_builtin_presets():
            print(f"  - {name}")
        return

    if args.list_tables:
        print("Tables:")
        for name, kind, path0 in list_all_tables(args.table_dir):
            suffix = f"  ({path0})" if path0 else ""
            print(f"  - [{kind}] {name}{suffix}")
        return

    if args.add_table:
        try:
            out, name = add_user_table(args.add_table, args.table_name, args.table_dir)
        except Exception as e:
            die(f"Could not add table: {e}")
        print(f"Added table: {name}")
        print(f"Saved: {out}")
        return

    if args.rename_table:
        if not args.to:
            die("--rename-table requires --to NEW_NAME")
        try:
            old, out = rename_user_table(args.rename_table, args.to, args.table_dir)
        except Exception as e:
            die(f"Could not rename table: {e}")
        print(f"Renamed table: {old} -> {args.to}")
        print(f"Saved: {out}")
        return

    if args.remove_table:
        try:
            old, removed = remove_user_table(args.remove_table, args.table_dir)
        except Exception as e:
            die(f"Could not remove table: {e}")
        print(f"Removed table: {old}")
        print(f"Deleted: {removed}")
        return

    if args.table and _looks_like_table_path(args.table):
        table_source = Path(os.path.expandvars(os.path.expanduser(str(args.table))))
        if not table_source.is_file():
            die(f"table file not found: {table_source}")

    path = find_binary(args.binary)
    print(f"Resolve binary: {path}  ({os.path.getsize(path)/1e6:.0f} MB)")

    if sum(1 for flag in (args.install_type8, args.install_type9, args.restore_type8) if flag) > 1:
        die("Use only one of --install-type8, --install-type9, or --restore-type8.")
    if args.restore_type8 and (args.restore or args.restore_slot or args.restore_all_slots):
        die("Use --restore-type8 separately from --restore/--restore-slot/--restore-all-slots.")

    if args.restore:
        restore(args.backup_dir, path, args.force, args.wait_resolve, args.kill_resolve, args.yes)
        return

    if args.restore_type8:
        restore_type8_entry(args.backup_dir, path, args.force, args.wait_resolve,
                            args.kill_resolve, args.yes, args.dry_run)
        return

    if args.restore_slot or args.restore_all_slots:
        if args.restore_slot and args.restore_all_slots:
            die("Use either --restore-slot SLOT or --restore-all-slots, not both.")
        if args.restore_all_slots:
            slot_types = [ctype for ctype, _label in CHART_UI_LABEL_ORDER]
        else:
            slot_types = [resolve_slot_type(args.restore_slot)]
        restore_chart_slots(args.backup_dir, path, slot_types, args.force,
                            args.wait_resolve, args.kill_resolve, args.yes, args.dry_run)
        return

    if args.install_table_slot and args.chart:
        die("Use either --install-table-slot SLOT or --base/--chart, not both.")
    if (args.install_type8 or args.install_type9) and args.install_table_slot:
        die("Use extra-entry installers separately from --install-table-slot.")
    if (args.install_type8 or args.install_type9) and args.rename_label:
        die("Use --type8-label/--type9-label with extra-entry installers; --rename-label is for fixed stock slots.")
    if (args.install_type8 or args.install_type9) and (args.tui or args.print_current or args.export_json or args.export_xml):
        die("Use extra-entry installers separately from --tui/--print-current/--export-json/--export-xml.")

    print("Scanning binary for chart streams...")
    t0 = time.time()
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        die(f"Could not read {path}: {e}")
    charts = find_charts(data)
    print(f"Found {len(charts)} chart(s) in {time.time()-t0:.1f}s.")

    if args.list or args.list_slots or not charts:
        for c in charts:
            print(f"  - {c.name!r} (type={c.ctype})  off={c.off}  "
                  f"comp={c.comp_len}  unc={c.unc_len}  slot={c.comp_stored}")
        if args.list_slots:
            print()
            print_slots(charts, data)
            return
        if args.list:
            return
        if not charts:
            die("No <colorchart> streams found in this binary.")

    if args.install_type8:
        if not _has_patch_input(args):
            args.table = "aliexpress"
        chart = select_chart(charts, args.chart or "classic")
        edits = parse_edits(args)
        rgb_overrides = _rgb_metadata_from_args(args)
        install_type8_entry(path, chart, edits, args.type8_label, args.type8_xml,
                            rgb_overrides, args.dry_run, args.backup_dir,
                            args.yes, args.force, args.wait_resolve,
                            args.kill_resolve)
        return

    if args.install_type9:
        if not _has_patch_input(args):
            die("--install-type9 needs chart data for type 9, e.g. --xml AliExpressChart2026.xml")
        chart = select_chart(charts, args.chart or "classic")
        type9_edits = parse_edits(args)
        type9_rgb_overrides = _rgb_metadata_from_args(args)
        install_type9_entries(path, chart, type9_edits, type9_rgb_overrides,
                              args.type8_label, args.type8_xml, args.type9_label,
                              args.type9_xml, args.dry_run, args.backup_dir,
                              args.yes, args.force, args.wait_resolve,
                              args.kill_resolve)
        return

    chart_filter = args.install_table_slot or args.chart
    preferred_base = _preferred_base_type_from_args(args)
    if preferred_base and not chart_filter:
        chart = select_chart_by_type(charts, preferred_base)
    else:
        chart = select_chart(charts, chart_filter)

    if args.export_json or args.export_xml:
        edits = parse_edits(args)
        if args.export_json:
            write_export_json(args.export_json, chart, edits)
            print(f"Wrote canonical JSON: {args.export_json}")
        if args.export_xml:
            write_export_xml(args.export_xml, chart, edits)
            print(f"Wrote Resolve-style XML: {args.export_xml}")
        return

    if args.tui:
        result = run_tui(path, chart, args, charts)
        if result:
            chart, edits = result
            patch(path, chart, edits or {}, args.dry_run, args.backup_dir, True, args.force, args.wait_resolve, args.kill_resolve, args.rename_label)
        return

    if args.print_current:
        print_current(chart)
        return

    edits = parse_edits(args)
    if not edits and not args.rename_label:
        die("No edits supplied. Use --table / --json / --csv / --xml / --set, or --tui / --print-current / --list.")

    patch(path, chart, edits, args.dry_run, args.backup_dir, args.yes, args.force, args.wait_resolve, args.kill_resolve, args.rename_label)


if __name__ == "__main__":
    main()
