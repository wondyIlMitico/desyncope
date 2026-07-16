#!/usr/bin/env python3
"""
DeSYncoPe — select which iCloud Drive folders stay cloud-only, and keep them
that way (prevent unwanted local allocation).

macOS keeps iCloud Drive files either *downloaded* (bytes allocated on disk) or
*dataless* / evicted (cloud-only, zero local bytes). The supported way to evict
is `brctl evict <path>` — the same as Finder's "Remove Download". macOS has no
native "pin as cloud-only forever" flag, so anything that opens an evicted file
re-downloads it. DeSYncoPe re-evicts pinned folders on demand and via a launchd
watcher, so they stay allocation-free.

Usage:
    desyncope.py                 # interactive TUI (browse + pin + evict)
    desyncope.py list            # top-level folders + root files, LOCAL and CLOUD size
    desyncope.py status          # show pinned folders and their local footprint
    desyncope.py pin <name>...   # add folder(s) to the pinned (keep-evicted) set
    desyncope.py unpin <name>... # remove folder(s) from the pinned set
    desyncope.py evict <name>... # evict now (defaults to all pinned if no args)
    desyncope.py download <name>... # force local — download named entries (inverse of evict)
    desyncope.py monitor [secs]  # live download/upload progress (needs Full Disk Access)
    desyncope.py watch           # re-evict all pinned folders once (for launchd)
    desyncope.py install-agent   # install background watcher (launchd)
    desyncope.py uninstall-agent # remove background watcher
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ICLOUD = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
CONFIG_DIR = Path.home() / ".config" / "desyncope"
CONFIG_FILE = CONFIG_DIR / "config.json"

AGENT_LABEL = "com.desyncope.watcher"
AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"
BLOCK = 512  # st_blocks unit on macOS


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"pinned": []}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def pinned_set() -> set[str]:
    return set(load_config().get("pinned", []))


def set_pinned(names: set[str]) -> None:
    cfg = load_config()
    cfg["pinned"] = sorted(names)
    save_config(cfg)


# --------------------------------------------------------------------------- #
# filesystem inspection
# --------------------------------------------------------------------------- #
def local_bytes(path: Path) -> int:
    """Bytes physically allocated on disk. 0 == fully evicted (cloud-only).

    Uses `du` (allocated blocks) for directories — orders of magnitude faster
    than a Python os.walk over large trees, and dataless files count as 0.
    """
    if os.path.islink(path) or not path.exists():
        try:
            return os.lstat(path).st_blocks * BLOCK
        except OSError:
            return 0
    if path.is_file():
        try:
            return os.lstat(path).st_blocks * BLOCK
        except OSError:
            return 0
    try:
        r = subprocess.run(["du", "-sk", str(path)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and r.stdout:
            return int(r.stdout.split("\t", 1)[0]) * 1024
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    return 0


def human(n: int) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < step:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= step
    return f"{n:.1f}PB"


def top_entries() -> list[str]:
    """Top-level iCloud Drive entries — folders *and* loose root files —
    excluding hidden entries and symlinks."""
    if not ICLOUD.exists():
        return []
    out = []
    for entry in sorted(ICLOUD.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith("."):
            continue
        if entry.is_symlink():
            continue
        if entry.is_dir() or entry.is_file():
            out.append(entry.name)
    return out


def is_top_dir(name: str) -> bool:
    p = ICLOUD / name
    return p.is_dir() and not p.is_symlink()


# --------------------------------------------------------------------------- #
# eviction
# --------------------------------------------------------------------------- #
def _evict_path(path: Path) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["/usr/bin/brctl", "evict", str(path)],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def _reason(msg: str) -> str:
    """Turn a raw brctl error into a short human hint."""
    if "Code=-2008" in msg or "not been uploaded" in msg.lower():
        return "not yet uploaded to iCloud"
    if "No such file" in msg:
        return "no longer present"
    return msg.split("NSLocalizedDescription=", 1)[-1][:60] if msg else "unknown"


def evict_folder(name: str, progress: bool = False) -> tuple[int, int, int, str]:
    """Evict a folder's contents.

    Returns (freed_bytes, files_ok, files_err, sample_error_reason).
    When `progress` is set, prints a live counter as files are evicted.
    """
    root = ICLOUD / name
    before = local_bytes(root)
    ok = err = 0
    sample = ""
    if root.is_file():
        success, msg = _evict_path(root)
        if success:
            ok = 1
        else:
            err, sample = 1, _reason(msg)
    else:
        # Pass 1: collect the materialized files (those with local bytes) and the
        # total, so we can show a real progress bar. One walk, one lstat each.
        if progress:
            sys.stdout.write(f"\r  {name}: scanning…   ")
            sys.stdout.flush()
        targets: list[tuple[Path, int]] = []
        total_bytes = 0
        for dirpath, dirs, files in os.walk(root, topdown=False, followlinks=False):
            for fname in files:
                fp = Path(dirpath) / fname
                try:
                    b = os.lstat(fp).st_blocks * BLOCK
                except OSError:
                    continue
                if b == 0:
                    continue  # already cloud-only
                targets.append((fp, b))
                total_bytes += b
        n_total = len(targets)

        # Pass 2: evict each, updating a live bar (freed / total, percent).
        freed_run = 0
        for i, (fp, sz) in enumerate(targets, 1):
            success, msg = _evict_path(fp)
            if success:
                ok += 1
                freed_run += sz
            else:
                err += 1
                if not sample:
                    sample = _reason(msg)
            if progress and (i % 5 == 0 or i == n_total):
                pct = 100 * i / n_total if n_total else 100
                w = 22
                fill = int(w * i / n_total) if n_total else w
                bar = "█" * fill + "░" * (w - fill)
                skip = f" {err} skip" if err else ""
                sys.stdout.write(
                    f"\r  {name} [{bar}] {pct:3.0f}%  {i}/{n_total}  "
                    f"{human(freed_run)}/{human(total_bytes)}{skip}   ")
                sys.stdout.flush()
        if progress and n_total:
            sys.stdout.write("\r" + " " * 72 + "\r")   # clear the bar line
            sys.stdout.flush()
        _evict_path(root)  # best-effort on the folder itself
    freed = before - local_bytes(root)
    return freed, ok, err, sample


def evict_names(names: list[str]) -> None:
    if not names:
        print("Nothing to evict.")
        return
    prog = sys.stdout.isatty()          # live counter only for interactive use
    for name in names:
        target = ICLOUD / name
        if not target.exists():
            print(f"  ✗ {name}: not found in iCloud Drive")
            continue
        freed, ok, err, sample = evict_folder(name, progress=prog)
        tail = f", {err} skipped ({sample})" if err else ""
        print(f"  ✓ {name}: freed {human(max(freed, 0))} "
              f"({ok} file(s) evicted{tail})")


def _download_path(path: Path) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["/usr/bin/brctl", "download", str(path)],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def download_entry(name: str) -> tuple[int, int, str]:
    """Request download (materialization) of an entry — the inverse of evict.
    brctl fetches in the background, so this counts *requested* files rather
    than bytes landed. Returns (files_ok, files_err, sample_error).
    """
    root = ICLOUD / name
    ok = err = 0
    sample = ""
    if root.is_file():
        success, msg = _download_path(root)
        if success:
            ok = 1
        else:
            err, sample = 1, _reason(msg)
    else:
        for dirpath, _dirs, files in os.walk(root, followlinks=False):
            for fname in files:
                fp = Path(dirpath) / fname
                try:
                    if os.lstat(fp).st_blocks != 0:
                        continue  # already on disk
                except OSError:
                    continue
                success, msg = _download_path(fp)
                if success:
                    ok += 1
                else:
                    err += 1
                    if not sample:
                        sample = _reason(msg)
    return ok, err, sample


def download_names(names: list[str], assume_yes: bool = False) -> None:
    if not names:
        print("Nothing to download. Pass one or more entry names "
              "(or use the TUI: highlight an entry and press d).")
        return
    targets = []
    for name in names:
        if (ICLOUD / name).exists():
            targets.append(name)
        else:
            print(f"  ✗ {name}: not found in iCloud Drive")
    if not targets:
        return

    # Confirm — download pulls entire subtrees and can be tens of GB.
    if not assume_yes:
        cloud = cloud_sizes_by_top()
        est = (f"  (~{human(sum(cloud.get(n, 0) for n in targets))} total)"
               if cloud else "")
        print(f"Force LOCAL — download {len(targets)} entr(y/ies) recursively{est}:")
        print("  " + ", ".join(targets))
        if input("Proceed? [y/N] ").strip().lower() != "y":
            print("Cancelled.")
            return

    for name in targets:
        ok, err, sample = download_entry(name)
        tail = f", {err} failed ({sample})" if err else ""
        print(f"  ↓ {name}: requested {ok} file(s){tail}")

    # Downloading pinned entries fights the cloud-only enforcement.
    clash = [n for n in targets if n in pinned_set()]
    if clash and agent_installed():
        print(f"\n⚠ {len(clash)} of these are pinned and the watcher is running — "
              f"it will re-evict them. Unpin or `uninstall-agent` to keep them local.")
    elif clash:
        print(f"\n⚠ Still pinned: {', '.join(clash)} — a later `evict` or the watcher "
              f"will push them back to cloud-only.")
    print("\nDownloads run in the background — watch with `desyncope.py monitor` (q to stop).")


# --------------------------------------------------------------------------- #
# launchd watcher
# --------------------------------------------------------------------------- #
def plist_contents(interval: int = 900) -> str:
    script = str(Path(__file__).resolve())
    watch_paths = "".join(
        f"        <string>{ICLOUD / n}</string>\n" for n in sorted(pinned_set())
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{script}</string>
        <string>watch</string>
    </array>
    <key>StartInterval</key>
    <integer>{interval}</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>WatchPaths</key>
    <array>
{watch_paths}    </array>
    <key>StandardOutPath</key>
    <string>{CONFIG_DIR / "watcher.log"}</string>
    <key>StandardErrorPath</key>
    <string>{CONFIG_DIR / "watcher.log"}</string>
</dict>
</plist>
"""


def install_agent(interval: int = 900) -> None:
    if not pinned_set():
        print("No pinned folders yet. Pin at least one folder first "
              "(TUI or `desyncope.py pin <name>`).")
        return
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    AGENT_PLIST.write_text(plist_contents(interval))
    subprocess.run(["launchctl", "unload", str(AGENT_PLIST)],
                   capture_output=True)
    r = subprocess.run(["launchctl", "load", str(AGENT_PLIST)],
                       capture_output=True, text=True)
    if r.returncode == 0:
        print(f"Watcher installed → {AGENT_PLIST}")
        print(f"  Re-evicts {len(pinned_set())} pinned folder(s) on change "
              f"and every {interval}s.")
        print(f"  Log: {CONFIG_DIR / 'watcher.log'}")
    else:
        print(f"launchctl load failed: {r.stderr.strip()}")


def uninstall_agent() -> None:
    if AGENT_PLIST.exists():
        subprocess.run(["launchctl", "unload", str(AGENT_PLIST)],
                       capture_output=True)
        AGENT_PLIST.unlink()
        print("Watcher removed.")
    else:
        print("No watcher installed.")


def agent_installed() -> bool:
    return AGENT_PLIST.exists()


# --------------------------------------------------------------------------- #
# sync monitor  (reads the bird client.db — requires Full Disk Access)
# --------------------------------------------------------------------------- #
BIRD_DB = (Path.home() / "Library" / "Application Support" / "CloudDocs"
           / "session" / "db" / "client.db")


def _read_transfers() -> dict:
    """Snapshot the tiny client.db (+wal/shm) into a temp dir and read the copy,
    so we never contend with the live daemon and trigger no downloads.

    Raises RuntimeError with a human hint if the DB is missing (iCloud off) or
    unreadable (Full Disk Access not granted to the terminal).
    """
    import shutil
    import sqlite3
    import tempfile

    if not BIRD_DB.exists():
        raise RuntimeError(
            "iCloud sync database not found — is iCloud Drive enabled?\n"
            "(The database only exists while the sync daemon is running.)")
    tmp = tempfile.mkdtemp(prefix="desyncope_mon_")
    dst = Path(tmp) / "client.db"
    try:
        for ext in ("", "-wal", "-shm"):
            src = Path(str(BIRD_DB) + ext)
            if src.exists():
                shutil.copy2(src, str(dst) + ext)
    except PermissionError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(
            "Permission denied reading the iCloud sync database.\n"
            "Grant Full Disk Access to your terminal, then reopen it:\n"
            "  System Settings → Privacy & Security → Full Disk Access")
    try:
        con = sqlite3.connect(str(dst))
        con.row_factory = sqlite3.Row
        q = con.execute
        cat = q("SELECT count(*) c, coalesce(sum(version_size), 0) s "
                "FROM client_items WHERE item_type = 1").fetchone()
        items = q("SELECT rowid AS rid, item_id, item_parent_id, item_filename "
                  "FROM client_items").fetchall()
        dls = q("SELECT throttle_id, transfer_size, unit_count_completed, "
                "transfer_stage, download_error FROM client_downloads").fetchall()
        ups = q("SELECT throttle_id, transfer_size, transfer_stage, upload_error "
                "FROM client_uploads").fetchall()
        con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Resolve each transfer to a readable path. The download's throttle_id maps
    # to a client_items rowid; walk item_parent_id up to build the full path.
    byrow = {r["rid"]: r for r in items}
    byid = {r["item_id"]: r for r in items}

    def name_for(rid: int):
        r = byrow.get(rid)
        if r is None:
            return None
        parts, seen, cur = [], set(), r
        while cur is not None and cur["item_id"] not in seen:
            seen.add(cur["item_id"])
            nm = cur["item_filename"]
            if not nm:
                break
            parts.append(nm)
            cur = byid.get(cur["item_parent_id"])
        return "/".join(reversed(parts)) if parts else None

    downloads = {}
    for r in dls:
        d = dict(r)
        d["name"] = name_for(r["throttle_id"])
        downloads[r["throttle_id"]] = d
    uploads = {}
    for r in ups:
        u = dict(r)
        u["name"] = name_for(r["throttle_id"])
        uploads[r["throttle_id"]] = u
    return {
        "cat_n": cat["c"], "cat_b": cat["s"],
        "downloads": downloads,
        "uploads": uploads,
    }


def _short_label(path: str | None, fallback: str = "", width: int = 40) -> str:
    """Compact, readable label for a transfer: 'parent/filename' with the
    ' - <DeviceName>' suffix stripped from the parent, truncated to `width`."""
    if not path:
        return fallback[:width]
    parts = path.split("/")
    fname = parts[-1]
    if len(parts) >= 2:
        parent = parts[-2].split(" - ")[0]        # drop " - MacBookWondyMitico"
        label = f"{parent}/{fname}"
    else:
        label = fname
    return ("…" + label[-(width - 1):]) if len(label) > width else label


def cloud_sizes_by_top() -> dict[str, int] | None:
    """Total iCloud (catalog) bytes under each top-level entry name, read from the
    bird DB. This is the *full* size in iCloud regardless of what's downloaded
    locally. Returns None if the DB can't be read (iCloud off / no Full Disk
    Access), so callers can degrade gracefully.
    """
    import shutil
    import sqlite3
    import tempfile

    if not BIRD_DB.exists():
        return None
    tmp = tempfile.mkdtemp(prefix="desyncope_sz_")
    dst = Path(tmp) / "client.db"
    try:
        try:
            for ext in ("", "-wal", "-shm"):
                src = Path(str(BIRD_DB) + ext)
                if src.exists():
                    shutil.copy2(src, str(dst) + ext)
        except PermissionError:
            return None
        con = sqlite3.connect(str(dst))
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT item_id, item_parent_id, item_filename, "
                           "version_size FROM client_items").fetchall()
        con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    byid = {r["item_id"]: r for r in rows}
    totals: dict[str, int] = {}
    for r in rows:
        # walk up to the top-level ancestor (its parent is not in our set)
        cur, seen = r, set()
        top = None
        while cur is not None and cur["item_id"] not in seen:
            seen.add(cur["item_id"])
            parent = byid.get(cur["item_parent_id"])
            if parent is None:            # cur is a root-level entry
                top = cur["item_filename"]
                break
            cur = parent
        if top:
            totals[top] = totals.get(top, 0) + (r["version_size"] or 0)
    return totals


def cmd_monitor(interval: float = 3.0, once: bool = False) -> None:
    """Live view of iCloud Drive transfers: active downloads (bytes done/total,
    throughput, ETA), uploads, and per-transfer progress. Use it to catch a
    pinned folder re-downloading in real time. Press 'q' (or Ctrl-C) to stop."""
    import select
    import time

    def _quit_or_wait(timeout: float) -> bool:
        """Wait up to `timeout`s, returning True if the user pressed 'q'.
        Falls back to a plain sleep when stdin isn't an interactive terminal."""
        if once or not sys.stdin.isatty():
            time.sleep(timeout)
            return False
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
            if ready and sys.stdin.read(1).lower() == "q":
                return True
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        return False

    prev_dl: dict | None = None
    prev_up: dict | None = None
    dl_session = up_session = 0     # bytes moved since we started watching
    dl_rate = up_rate = 0.0         # smoothed B/s
    last_t = time.time()
    first = True

    def _smooth(old, inst):
        return inst if old == 0 else 0.6 * old + 0.4 * inst

    try:
        while True:
            try:
                st = _read_transfers()
            except RuntimeError as e:
                print(e)
                return
            now = time.time()
            dls, ups = st["downloads"], st["uploads"]
            dl_done = sum(d["unit_count_completed"] or 0 for d in dls.values())
            dl_total = sum(d["transfer_size"] or 0 for d in dls.values())
            up_total = sum(u["transfer_size"] or 0 for u in ups.values())
            dt = max(now - last_t, 1e-6)

            if prev_dl is not None:
                delta = 0
                for tid, pd in prev_dl.items():
                    if tid not in dls:      # finished between ticks
                        delta += (pd["transfer_size"] or 0) - (pd["unit_count_completed"] or 0)
                    else:                   # progressed
                        delta += (dls[tid]["unit_count_completed"] or 0) - (pd["unit_count_completed"] or 0)
                delta = max(delta, 0)
                dl_session += delta
                dl_rate = _smooth(dl_rate, delta / dt)
            if prev_up is not None:
                # uploads have no per-byte progress column; a transfer that
                # left the queue counts as its full size uploaded.
                udelta = sum((pu["transfer_size"] or 0)
                             for tid, pu in prev_up.items() if tid not in ups)
                up_session += udelta
                up_rate = _smooth(up_rate, udelta / dt)

            dl_eta = int(max(dl_total - dl_done, 0) / dl_rate) if dl_rate > 1 else None
            up_eta = int(up_total / up_rate) if up_rate > 1 else None

            # Human-readable phase — iCloud prioritises upload over download.
            if ups and dl_rate < 1:
                phase = "⬆ UPLOAD in corso — download in pausa"
            elif ups and dl_rate >= 1:
                phase = "⬆⬇ upload + download insieme"
            elif dl_rate >= 1:
                phase = "⬇ DOWNLOAD in corso"
            elif dls:
                phase = "⬇ download in coda (in attesa)"
            else:
                phase = "✓ in pari — nessun trasferimento attivo"

            if not once:
                os.system("clear")
            print(f" DeSYncoPe monitor  {time.strftime('%H:%M:%S')}  "
                  f"(every {interval:g}s — press q to stop)")
            print(f" iCloud catalog: {st['cat_n']} files, {human(st['cat_b'])}")
            print(f" FASE: {phase}")
            print(" " + "-" * 60)
            dl_eta_s = f"  ETA {dl_eta}s" if dl_eta else ""
            up_eta_s = f"  ETA {up_eta}s" if up_eta else ""
            print(f" ↓ downloads: {len(dls):>4}   {human(dl_done)} / {human(dl_total)}"
                  f"   {human(int(dl_rate))}/s{dl_eta_s}")
            up_rate_s = f"{human(int(up_rate))}/s{up_eta_s}" if up_rate >= 1 else "…"
            print(f" ↑ uploads  : {len(ups):>4}   {human(up_total)} rimanenti"
                  f"   {up_rate_s}")
            print(f" this run   : ↓ {human(dl_session)}   ↑ {human(up_session)}")

            if ups:
                print(" " + "-" * 60)
                print(" ↑ upload in corso:")
                for u in sorted(ups.values(),
                                key=lambda x: -(x["transfer_size"] or 0))[:8]:
                    err = "ERR " if u["upload_error"] else ""
                    lbl = _short_label(u.get("name"), u["transfer_stage"] or "")
                    print(f"   {human(u['transfer_size']):>9}   {err}{lbl}")
            if dls:
                print(" " + "-" * 60)
                print(" ↓ download in coda/corso:")
                for d in sorted(dls.values(),
                                key=lambda x: -(x["transfer_size"] or 0))[:8]:
                    pct = 100 * (d["unit_count_completed"] or 0) / (d["transfer_size"] or 1)
                    err = "ERR " if d["download_error"] else ""
                    lbl = _short_label(d.get("name"), d["transfer_stage"] or "")
                    print(f"   {human(d['unit_count_completed']):>9} /"
                          f" {human(d['transfer_size']):>9} {pct:5.1f}%  {err}{lbl}")
            if not dls and not ups and not first:
                print("   (nessun trasferimento attivo — tutto in pari)")
            if ups and up_rate < 1:
                print("\n nota: il ritmo upload ('…') si vede solo quando un file "
                      "completa;\n       file grandi ⇒ resta '…' anche mentre carica. "
                      "Se '↑ rimanenti' cala,\n       sta caricando; se resta fermo "
                      "per minuti, iCloud è in pausa.")

            prev_dl, prev_up = dls, ups
            last_t = now
            first = False
            if once:
                break
            if _quit_or_wait(interval):
                break
    except KeyboardInterrupt:
        print()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def cmd_list() -> None:
    if not ICLOUD.exists():
        print("iCloud Drive not found. Is iCloud Drive enabled?")
        return
    pins = pinned_set()
    cloud = cloud_sizes_by_top()          # None if no Full Disk Access / iCloud off
    # order by type: folders first, then loose root files, each alphabetical.
    entries = sorted(top_entries(), key=lambda n: (not is_top_dir(n), n.lower()))
    print(f"{'':2}{'NAME':<34}{'LOCAL':>10}{'CLOUD':>11}")
    for name in entries:
        p = ICLOUD / name
        mark = "●" if name in pins else " "
        disp = name + ("/" if is_top_dir(name) else "")
        if len(disp) > 33:
            disp = disp[:32] + "…"
        lb = human(local_bytes(p))
        cb = "—" if cloud is None else human(cloud.get(name, 0))
        print(f"{mark} {disp:<34}{lb:>10}{cb:>11}")
    print("\n● = pinned (kept cloud-only)   LOCAL = bytes on disk (0 = cloud-only)"
          "   CLOUD = total size in iCloud")
    if cloud is None:
        print("CLOUD is blank — grant Full Disk Access to your terminal to show it "
              "(System Settings → Privacy & Security → Full Disk Access).")


def cmd_status() -> None:
    pins = sorted(pinned_set())
    print(f"Watcher: {'installed' if agent_installed() else 'not installed'}")
    if not pins:
        print("No pinned folders.")
        return
    print(f"\nPinned folders ({len(pins)}):")
    leaked = 0
    for name in pins:
        p = ICLOUD / name
        lb = local_bytes(p)
        leaked += lb
        flag = "  ⚠ re-downloaded" if lb > 0 else ""
        exists = "" if p.exists() else "  (missing)"
        print(f"  ● {name:<32}{human(lb):>10}{flag}{exists}")
    if leaked:
        print(f"\n{human(leaked)} allocated locally across pinned folders. "
              f"Run `desyncope.py evict` to reclaim it.")
    else:
        print("\nAll pinned folders are fully cloud-only. ✓")


def cmd_pin(names: list[str], add: bool) -> None:
    pins = pinned_set()
    valid = set(top_entries())
    for n in names:
        if add and n not in valid:
            print(f"  ✗ {n}: not a top-level iCloud Drive entry (skipped)")
            continue
        if add:
            pins.add(n)
            print(f"  + pinned {n}")
        else:
            pins.discard(n)
            print(f"  - unpinned {n}")
    set_pinned(pins)
    if add:
        print("\nTip: `desyncope.py evict` to evict now, then "
              "`install-agent` to keep them cloud-only.")


# --------------------------------------------------------------------------- #
# TUI
# --------------------------------------------------------------------------- #
def run_tui() -> None:
    import curses

    if not ICLOUD.exists():
        print("iCloud Drive not found. Is iCloud Drive enabled?")
        return

    all_names = top_entries()
    if not all_names:
        print("No entries found in iCloud Drive.")
        return

    pins = pinned_set()
    dirs = {n for n in all_names if is_top_dir(n)}
    cloud = cloud_sizes_by_top() or {}   # {} if no Full Disk Access / iCloud off
    # size cache: entry name -> local bytes allocated on disk
    sizes: dict[str, int] = {}

    def size_of(name: str) -> int:
        if name not in sizes:
            sizes[name] = local_bytes(ICLOUD / name)
        return sizes[name]

    # du is fast; compute the local footprint up front with a progress line.
    print("Scanning local footprint of iCloud Drive entries…")
    for i, name in enumerate(all_names, 1):
        print(f"\r  {i}/{len(all_names)}  {name[:40]:<40}", end="", flush=True)
        size_of(name)
    print("\r" + " " * 60 + "\r", end="")

    # selectable ordering — cycle with the 'o' key
    SORTS = ["type", "name", "local", "cloud"]
    sort_i = 0

    def ordered() -> list[str]:
        m = SORTS[sort_i]
        if m == "name":
            key = lambda n: (n.lower(),)
        elif m == "local":
            key = lambda n: (-sizes.get(n, 0), n.lower())
        elif m == "cloud":
            key = lambda n: (-cloud.get(n, 0), n.lower())
        else:  # "type": folders first, then files, each alphabetical
            key = lambda n: (not is_top_dir(n), n.lower())
        return sorted(all_names, key=key)

    folders = ordered()

    def _main(stdscr):
        nonlocal pins, folders, sort_i
        curses.curs_set(0)
        curses.use_default_colors()
        if curses.has_colors():
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
            curses.init_pair(3, curses.COLOR_GREEN, -1)
        pos = 0
        top = 0
        status = ("Space: pin  e: evict  d: download  o: sort  "
                  "m: monitor  s: rescan  w: watcher  q: quit")

        def put(row, text, attr=curses.A_NORMAL):
            # Write a full-width line without touching the bottom-right cell,
            # which makes curses advance off-screen and raise ERR.
            _h, _w = stdscr.getmaxyx()
            if row >= _h:
                return
            try:
                stdscr.addnstr(row, 0, text.ljust(_w)[: _w - 1], _w - 1, attr)
            except curses.error:
                pass

        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            title = (f" DeSYncoPe — keep iCloud folders cloud-only"
                     f"    [sort: {SORTS[sort_i]}] ")
            put(0, title, curses.color_pair(1) | curses.A_BOLD)
            header = f"  {'NAME':<34}{'LOCAL':>9}{'CLOUD':>10}"
            put(1, header, curses.A_DIM)

            body_h = h - 4
            if pos < top:
                top = pos
            elif pos >= top + body_h:
                top = pos - body_h + 1

            for i in range(top, min(len(folders), top + body_h)):
                name = folders[i]
                row = 2 + (i - top)
                mark = "[●]" if name in pins else "[ ]"
                disp = (name + "/") if name in dirs else name
                if len(disp) > 33:
                    disp = disp[:32] + "…"
                szs = f"{human(sizes[name]):>9}" if name in sizes else f"{'—':>9}"
                cbs = f"{human(cloud[name]):>10}" if name in cloud else f"{'—':>10}"
                line = f" {mark} {disp:<33}{szs}{cbs}"
                attr = curses.A_REVERSE if i == pos else curses.A_NORMAL
                if name in pins and i != pos:
                    attr |= curses.color_pair(3)
                put(row, line, attr)

            put(h - 1, status, curses.A_DIM)
            stdscr.refresh()

            c = stdscr.getch()
            if c in (ord("q"), 27):
                break
            elif c in (curses.KEY_DOWN, ord("j")):
                pos = min(len(folders) - 1, pos + 1)
            elif c in (curses.KEY_UP, ord("k")):
                pos = max(0, pos - 1)
            elif c == curses.KEY_NPAGE:
                pos = min(len(folders) - 1, pos + body_h)
            elif c == curses.KEY_PPAGE:
                pos = max(0, pos - body_h)
            elif c in (ord("o"), ord("O")):
                cur = folders[pos] if folders else None
                sort_i = (sort_i + 1) % len(SORTS)
                folders = ordered()
                if cur in folders:
                    pos = folders.index(cur)     # keep cursor on same entry
                top = 0
            elif c == ord(" "):
                name = folders[pos]
                if name in pins:
                    pins.discard(name)
                else:
                    pins.add(name)
                set_pinned(pins)
            elif c in (ord("s"), ord("S")):
                status = "Rescanning sizes…"
                put(h - 1, status, curses.A_DIM)
                stdscr.refresh()
                sizes.clear()
                for name in folders:
                    size_of(name)
                status = ("Space: pin  e: evict  d: download  m: monitor  "
                          "s: rescan  w: watcher  q: quit")
            elif c in (ord("e"), ord("E")):
                targets = [n for n in folders if n in pins]
                if not targets:
                    status = "No pinned folders to evict."
                    continue
                curses.def_prog_mode()
                curses.endwin()
                print(f"\nEvicting {len(targets)} pinned folder(s):")
                evict_names(targets)
                for n in targets:          # refresh footprint of evicted ones
                    sizes.pop(n, None)
                    size_of(n)
                input("\nDone. Press Enter to return to DeSYncoPe…")
                curses.reset_prog_mode()
                stdscr.refresh()
            elif c in (ord("d"), ord("D")):
                name = folders[pos]            # the highlighted entry, not the pins
                curses.def_prog_mode()
                curses.endwin()
                print(f"\nForce local — download the highlighted entry:")
                download_names([name])
                sizes.pop(name, None)          # refresh its footprint
                size_of(name)
                input("\nPress Enter to return to DeSYncoPe…")
                curses.reset_prog_mode()
                stdscr.refresh()
            elif c in (ord("m"), ord("M")):
                curses.def_prog_mode()
                curses.endwin()
                cmd_monitor()          # returns when the user presses q
                curses.reset_prog_mode()
                stdscr.refresh()
            elif c in (ord("w"), ord("W")):
                curses.def_prog_mode()
                curses.endwin()
                if agent_installed():
                    print("\nWatcher is installed.")
                    ans = input("Remove it? [y/N] ").strip().lower()
                    if ans == "y":
                        uninstall_agent()
                else:
                    print("\nThe watcher re-evicts pinned folders on change "
                          "and every 15 min.")
                    ans = input("Install background watcher now? [y/N] ").strip().lower()
                    if ans == "y":
                        install_agent()
                input("\nPress Enter to return…")
                curses.reset_prog_mode()
                stdscr.refresh()

    curses.wrapper(_main)
    print("Config saved.", f"Pinned: {', '.join(sorted(pins)) or '(none)'}")
    if pins and not agent_installed():
        print("Tip: `desyncope.py evict` to evict now, "
              "`desyncope.py install-agent` to keep them cloud-only.")


# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    if not argv:
        run_tui()
        return 0

    cmd, rest = argv[0], argv[1:]
    if cmd in ("list", "ls"):
        cmd_list()
    elif cmd == "status":
        cmd_status()
    elif cmd == "pin":
        cmd_pin(rest, add=True)
    elif cmd == "unpin":
        cmd_pin(rest, add=False)
    elif cmd == "evict":
        evict_names(rest if rest else sorted(pinned_set()))
    elif cmd in ("download", "dl"):
        download_names(rest)
    elif cmd == "monitor":
        once = "--once" in rest
        nums = [a for a in rest if a.replace(".", "", 1).isdigit()]
        cmd_monitor(float(nums[0]) if nums else 3.0, once)
    elif cmd == "watch":
        evict_names(sorted(pinned_set()))
    elif cmd == "install-agent":
        install_agent(int(rest[0]) if rest else 900)
    elif cmd == "uninstall-agent":
        uninstall_agent()
    elif cmd in ("-h", "--help", "help"):
        print(__doc__)
    else:
        print(f"Unknown command: {cmd}\n")
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print()
        sys.exit(130)
