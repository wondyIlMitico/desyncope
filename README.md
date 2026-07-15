# DeSYncoPe

**De**-**Sync** **iC**loud — pick which iCloud Drive folders stay **cloud-only**, and
keep them that way so they don't quietly re-download and eat your disk.

## What it actually does (and the honest limits)

macOS keeps every iCloud Drive file in one of two states:

- **Downloaded** — bytes allocated on your disk.
- **Dataless / evicted** — cloud-only, **zero local bytes** (the little cloud
  icon in Finder).

The supported way to make a file cloud-only is `brctl evict <path>` — exactly
what Finder's **"Remove Download"** does. **macOS has no flag to pin a folder as
cloud-only forever.** Anything that opens an evicted file (Spotlight, a backup
app, Quick Look, you double-clicking it) makes the OS re-download it.

So DeSYncoPe does two things:

1. **Evict** the folders you choose, on demand.
2. **Re-evict** them automatically via a background `launchd` watcher, so
   re-downloaded ("re-allocated") files get pushed back to the cloud.

It never deletes anything — evicted files stay safe in iCloud and re-download
the moment you actually open them. Eviction is fully reversible.

> **Safety note:** `brctl` refuses to evict a file that hasn't finished
> uploading to iCloud yet (it won't remove your only copy). DeSYncoPe reports
> those as *skipped (not yet uploaded)* — let iCloud finish syncing, then run
> evict again.

## Requirements

- macOS with iCloud Drive enabled
- Python 3.9+ (uses the system `brctl`, `du`, and `launchctl`)

No third-party packages.

## Usage

Interactive TUI (browse, pin with **Space**, evict pinned with **e**):

```bash
python3 desyncope.py
```

Command line:

```bash
python3 desyncope.py list             # folders + root files, LOCAL and CLOUD size
python3 desyncope.py pin Downloads Backup   # mark folders to keep cloud-only
python3 desyncope.py evict             # evict all pinned folders now
python3 desyncope.py download Documents   # force local: download named entries (asks to confirm)
python3 desyncope.py status            # what's pinned + how much leaked back
python3 desyncope.py monitor           # live download/upload progress (Ctrl-C to stop)
python3 desyncope.py install-agent     # start the background re-evict watcher
python3 desyncope.py uninstall-agent   # stop it
```

### Typical flow

```bash
python3 desyncope.py pin "Old Projects" Downloads
python3 desyncope.py evict            # reclaim the space now
python3 desyncope.py install-agent    # keep it reclaimed
python3 desyncope.py status           # check anytime
```

## TUI keys

| Key         | Action                                        |
|-------------|-----------------------------------------------|
| `↑`/`↓` `j`/`k` | move                                      |
| `Space`     | pin / unpin the highlighted folder            |
| `e`         | evict all pinned folders now                  |
| `d`         | force local: download the **highlighted** entry (confirms) |
| `m`         | live download/upload monitor (Ctrl-C to exit) |
| `s`         | rescan local footprint                         |
| `w`         | install / remove the background watcher       |
| `q`         | quit                                          |

## Monitoring downloads

`monitor` shows iCloud Drive transfers **live** — active downloads (bytes done /
total, throughput, ETA), uploads, and per-transfer progress — so you can watch a
pinned folder re-download in real time instead of guessing from the log.

```bash
python3 desyncope.py monitor        # refresh every 3s
python3 desyncope.py monitor 5      # every 5s
python3 desyncope.py monitor --once # one snapshot, then exit
```

It reads the sync daemon's local database
(`~/Library/Application Support/CloudDocs/session/db/client.db`) by copying it to
a temp file first, so it never contends with the daemon and triggers **no**
downloads.

That database is TCC-protected, so anything that reads it needs **Full Disk
Access** for your terminal: *System Settings → Privacy & Security → Full Disk
Access → enable your terminal → reopen it.* This affects two things:

- `monitor` — without FDA it prints a hint and exits.
- the **CLOUD** column in `list` / the TUI — without FDA it shows `—` (LOCAL and
  everything else still work).

Eviction, pinning, status, and the watcher never need FDA.

## The background watcher

`install-agent` writes `~/Library/LaunchAgents/com.desyncope.watcher.plist` and
loads it. It runs `desyncope.py watch` (which re-evicts every pinned folder):

- **on change** to any pinned folder (`WatchPaths`), and
- **every 15 minutes** (`StartInterval`) as a fallback.

Logs: `~/.config/desyncope/watcher.log`.
Config: `~/.config/desyncope/config.json` (the list of pinned folders).

Re-run `install-agent` after changing your pinned set — the watched paths are
baked into the plist at install time.

## Uninstall

```bash
python3 desyncope.py uninstall-agent
rm -rf ~/.config/desyncope
```

Your files are untouched; any that were cloud-only re-download normally when opened.
