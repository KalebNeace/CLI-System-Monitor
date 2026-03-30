# ⚡ SysMon — CLI System Monitor

A cross-platform terminal system monitor with real-time stats and an analytics dashboard. Works on **Windows**, **macOS**, and **Linux**.

---

## Features

- Real-time CPU, memory, swap, disk, and network stats
- Per-core CPU bars with sparkline history
- **All disks shown** (C:\, D:\, etc. on Windows — or all mounts on Linux/macOS)
- **⚠ Alert highlight** when any disk reaches 90%+ usage
- Top 5 processes sorted by **CPU** and **Memory** side-by-side
- Filters out noisy Windows system processes (System Idle Process, etc.)
- Each run creates a **uniquely timestamped log file**
- Built-in HTTP server for live dashboard streaming

---

## Requirements

```bash
pip install psutil rich
```

---

## Usage

```bash
python monitor.py
```

Press **Ctrl+C** to exit. The log flushes to disk on exit (and every ~10s while running).

---

## Arguments

| Flag | Default | Description |
|------|---------|-------------|
| `-i`, `--interval` | `1.0` | Refresh interval in seconds |
| `-l`, `--log` | `./output/sysmon_YYYY-MM-DD_HH-MM-SS.json` | Custom log file path |
| `--no-log` | off | Disable logging entirely |
| `--dump` | off | Print a single JSON snapshot and exit |
| `--dashboard` | off | Open the analytics dashboard in your browser |

---

## Examples

```bash
# Default — 1 second refresh, timestamped log in ./output/
python monitor.py

# Faster refresh
python monitor.py -i 0.5

# Slower refresh, no log file
python monitor.py -i 5 --no-log

# Save log to a custom location
python monitor.py -l C:\Users\Owner\Desktop\sysmon.json

# One-shot JSON snapshot (good for scripting or cron)
python monitor.py --dump

# Open the analytics dashboard in your browser
python monitor.py --dashboard
```

---

## Analytics Dashboard

`dashboard.html` is served automatically over a local HTTP server when the monitor runs.

**Two ways to open it:**

1. Run `python monitor.py --dashboard` — opens automatically
2. While the monitor is running, visit: `http://127.0.0.1:7477/dashboard.html`

The monitor's footer bar always shows the URL and current log filename.

### ⏵ Live Mode (real-time updates)

1. Start the monitor: `python monitor.py`
2. Open `http://127.0.0.1:7477/dashboard.html` in your browser
3. Click **⏵ Live Mode** and enter the log filename shown in the monitor footer
4. The dashboard polls and refreshes every 2 seconds automatically

> Live Mode requires the dashboard to be opened over HTTP (via the built-in server), not via `file://`.
> If you run `python monitor.py --dashboard`, this is handled automatically.

### Review Mode

Click **Load Log File** to load any saved `.json` log from the `output/` folder for offline review.

### Dashboard features

- Stat cards: CPU avg/peak, memory, swap, disk, network
- **Time-range slider** — narrow the view to any portion of the run
- CPU usage over time (line chart)
- Memory usage over time (line chart)
- Swap usage over time (line chart)
- CPU load distribution (histogram)
- CPU vs Memory correlation (scatter plot)
- **Process Impact Rankings** — top 10 processes by average CPU and Memory across the entire run, with peak values and appearance counts

---

## Log Files

Each run creates a new file: `output/sysmon_YYYY-MM-DD_HH-MM-SS.json`

The `output/` folder is created automatically. Logs cap at **10,000 entries** and rotate automatically.

Example entry:

```json
{
  "timestamp": "2026-03-28T16:37:49",
  "cpu": { "total": 10.5, "per_core": [...], "freq_mhz": 3701, "logical_cores": 24, "physical_cores": 12 },
  "memory": { "total_gb": 34.28, "used_gb": 14.27, "available_gb": 20.01, "percent": 41.6 },
  "swap": { "total_gb": 2.15, "used_gb": 0.07, "percent": 3.4 },
  "disks": [{ "path": "C:\\", "total_gb": 1023, "used_gb": 989, "free_gb": 34, "percent": 96.7 }],
  "disk_io": { "read_mb": 247672, "write_mb": 204099 },
  "network": { "sent_mb": 556.8, "recv_mb": 19345.5, "packets_sent": 6300022, "packets_recv": 12991504 },
  "top_cpu_processes": [...],
  "top_mem_processes": [...]
}
```

---

## File Structure

```
├── monitor.py        # CLI monitor — run this
├── dashboard.html    # Analytics dashboard
└── output/
    ├── sysmon_2026-03-28_09-00-00.json   # Run 1
    ├── sysmon_2026-03-28_14-32-11.json   # Run 2
    └── ...
```

---

## Colour Coding

| Colour | Threshold |
|--------|-----------|
| 🟢 Green | < 40% |
| 🟡 Yellow | 40–70% (CPU/Mem) · 75–90% (Disk) |
| 🔴 Red | ≥ 90% |

Disk drives at ≥ 90% show a blinking **⚠ FULL** warning.