#!/usr/bin/env python3
"""
SysMon - Cross-platform CLI System Monitor
Displays real-time CPU, memory, and disk usage.
Logs data to JSON for analytics/graphing.

Requirements: pip install psutil rich
"""

import json
import os
import sys
import time
import signal
import argparse
import webbrowser
import threading
import http.server
import socketserver
from datetime import datetime
from pathlib import Path
from collections import deque

try:
    import psutil
except ImportError:
    print("Missing dependency: pip install psutil")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich import box
except ImportError:
    print("Missing dependency: pip install rich")
    sys.exit(1)


# ─── Config ────────────────────────────────────────────────────────────────────

OUTPUT_DIR        = Path(__file__).parent / "output"
RUN_TIMESTAMP     = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
DEFAULT_LOG_FILE  = OUTPUT_DIR / f"sysmon_{RUN_TIMESTAMP}.json"
DEFAULT_INTERVAL  = 1.0
HISTORY_LEN       = 60
MAX_LOG_ENTRIES   = 10_000
DASHBOARD_FILE    = Path(__file__).parent / "dashboard.html"
DASHBOARD_PORT    = 7477

# Processes to always hide (Windows idle / kernel noise)
FILTERED_PROCESSES = {"system idle process", "system", "registry", "memory compression"}


# ─── Helpers ───────────────────────────────────────────────────────────────────

SPARKS = " ▁▂▃▄▅▆▇█"

def sparkline(values, width: int = 20) -> str:
    if not values:
        return " " * width
    chunk = list(values)[-width:]
    result = ""
    for v in chunk:
        idx = int(v / 100 * (len(SPARKS) - 1))
        result += SPARKS[max(0, min(idx, len(SPARKS) - 1))]
    return result.ljust(width)


def color_for(pct: float) -> str:
    if pct >= 90:   return "bold red"
    elif pct >= 70: return "yellow"
    elif pct >= 40: return "green"
    else:           return "bright_green"


def disk_color(pct: float) -> str:
    if pct >= 90:   return "bold red"
    elif pct >= 75: return "yellow"
    else:           return "green"


# ─── Data collection ───────────────────────────────────────────────────────────

class SystemStats:
    def __init__(self, history_len: int = HISTORY_LEN):
        self.history = {
            "cpu":  deque(maxlen=history_len),
            "mem":  deque(maxlen=history_len),
            "swap": deque(maxlen=history_len),
        }
        psutil.cpu_percent(interval=None, percpu=False)

    def collect(self) -> dict:
        ts = datetime.now().isoformat(timespec="seconds")

        # CPU
        cpu_total    = psutil.cpu_percent(interval=None)
        cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
        cpu_freq     = psutil.cpu_freq()
        cpu_logical  = psutil.cpu_count(logical=True)
        cpu_physical = psutil.cpu_count(logical=False)

        # Memory
        mem  = psutil.virtual_memory()
        swap = psutil.swap_memory()

        # All disks
        disks = []
        for part in psutil.disk_partitions(all=False):
            if sys.platform == "win32" and part.fstype == "":
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disks.append({
                    "path":     part.mountpoint,
                    "device":   part.device,
                    "fstype":   part.fstype,
                    "total_gb": round(usage.total / 1e9, 1),
                    "used_gb":  round(usage.used  / 1e9, 1),
                    "free_gb":  round(usage.free  / 1e9, 1),
                    "percent":  usage.percent,
                })
            except (PermissionError, OSError):
                continue

        try:
            disk_io = psutil.disk_io_counters()
        except Exception:
            disk_io = None

        try:
            net_io = psutil.net_io_counters()
        except Exception:
            net_io = None

        # Processes — filter noise, build top-CPU and top-MEM lists
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "status"]):
            try:
                info = p.info
                name_lower = (info.get("name") or "").lower()
                if name_lower in FILTERED_PROCESSES:
                    continue
                procs.append(info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        top_cpu = sorted(procs, key=lambda x: x.get("cpu_percent") or 0, reverse=True)[:5]
        top_mem = sorted(procs, key=lambda x: x.get("memory_percent") or 0, reverse=True)[:5]

        snapshot = {
            "timestamp": ts,
            "cpu": {
                "total":          round(cpu_total, 1),
                "per_core":       [round(c, 1) for c in cpu_per_core],
                "freq_mhz":       round(cpu_freq.current, 0) if cpu_freq else None,
                "logical_cores":  cpu_logical,
                "physical_cores": cpu_physical,
            },
            "memory": {
                "total_gb":     round(mem.total     / 1e9, 2),
                "used_gb":      round(mem.used      / 1e9, 2),
                "available_gb": round(mem.available / 1e9, 2),
                "percent":      mem.percent,
            },
            "swap": {
                "total_gb": round(swap.total / 1e9, 2),
                "used_gb":  round(swap.used  / 1e9, 2),
                "percent":  swap.percent,
            },
            "disks": disks,
            "disk_io": {
                "read_mb":  round(disk_io.read_bytes  / 1e6, 1) if disk_io else None,
                "write_mb": round(disk_io.write_bytes / 1e6, 1) if disk_io else None,
            },
            "network": {
                "sent_mb":      round(net_io.bytes_sent / 1e6, 1) if net_io else None,
                "recv_mb":      round(net_io.bytes_recv / 1e6, 1) if net_io else None,
                "packets_sent": net_io.packets_sent if net_io else None,
                "packets_recv": net_io.packets_recv if net_io else None,
            },
            "top_cpu_processes": top_cpu,
            "top_mem_processes": top_mem,
        }

        self.history["cpu"].append(cpu_total)
        self.history["mem"].append(mem.percent)
        self.history["swap"].append(swap.percent)
        return snapshot


# ─── Rich UI panels ────────────────────────────────────────────────────────────

def build_header(snap: dict, log_path) -> Panel:
    platform = sys.platform
    hostname = os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", "?")
    text = Text()
    text.append("⚡ SysMon  ", style="bold cyan")
    text.append(f"{hostname}  ", style="white")
    text.append(f"[{platform}]  ", style="dim")
    text.append(snap["timestamp"], style="dim")
    if log_path:
        text.append(f"  · log → {log_path}", style="dim")
    return Panel(text, box=box.SIMPLE, style="on grey11")


def build_cpu_panel(snap: dict, history: dict) -> Panel:
    cpu   = snap["cpu"]
    total = cpu["total"]
    spark = sparkline(history["cpu"])
    color = color_for(total)

    t = Table.grid(padding=(0, 1))
    t.add_column(style="dim", width=18)
    t.add_column()

    t.add_row("Total usage", Text(f"{total:5.1f}%  {spark}", style=color))
    if cpu["freq_mhz"]:
        t.add_row("Frequency", f"{cpu['freq_mhz']:.0f} MHz")
    t.add_row("Cores", f"{cpu['physical_cores']} physical / {cpu['logical_cores']} logical")

    per_core_text = Text()
    for i, c in enumerate(cpu["per_core"]):
        bar_len = int(c / 100 * 8)
        bar = "█" * bar_len + "░" * (8 - bar_len)
        per_core_text.append(f"C{i:<2} ", style="dim")
        per_core_text.append(bar + f" {c:4.1f}%  ", style=color_for(c))
        if (i + 1) % 4 == 0:
            per_core_text.append("\n")

    t.add_row("Per core", per_core_text)
    return Panel(t, title="[bold cyan]CPU[/bold cyan]", border_style="cyan", box=box.ROUNDED)


def build_memory_panel(snap: dict, history: dict) -> Panel:
    mem = snap["memory"]
    sw  = snap["swap"]

    t = Table.grid(padding=(0, 1))
    t.add_column(style="dim", width=12)
    t.add_column()
    t.add_column(style="dim", width=12)
    t.add_column()

    t.add_row(
        "RAM",  Text(f"{mem['percent']:5.1f}%  {sparkline(history['mem'])}",  style=color_for(mem["percent"])),
        "Swap", Text(f"{sw['percent']:5.1f}%  {sparkline(history['swap'])}", style=color_for(sw["percent"])),
    )
    t.add_row("Used", f"{mem['used_gb']:.2f} / {mem['total_gb']:.2f} GB",
              "Used", f"{sw['used_gb']:.2f} / {sw['total_gb']:.2f} GB")
    t.add_row("Available", f"{mem['available_gb']:.2f} GB", "", "")
    return Panel(t, title="[bold magenta]Memory[/bold magenta]", border_style="magenta", box=box.ROUNDED)


def build_disk_panel(snap: dict) -> Panel:
    disks   = snap.get("disks", [])
    disk_io = snap.get("disk_io", {})

    t = Table.grid(padding=(0, 1))
    t.add_column(style="dim", width=6)
    t.add_column(width=36)
    t.add_column(style="dim")

    for d in disks:
        pct     = d["percent"]
        color   = disk_color(pct)
        bar_len = int(pct / 100 * 24)
        bar     = Text("█" * bar_len, style=color) + Text("░" * (24 - bar_len), style="dim")
        alert   = Text(" ⚠ FULL", style="bold red blink") if pct >= 90 else Text("")
        label   = d["path"].rstrip("\\").rstrip("/") or d["path"]
        t.add_row(
            label,
            Text(f"{pct:5.1f}%  ") + bar + alert,
            f"{d['used_gb']:.0f}/{d['total_gb']:.0f} GB  free {d['free_gb']:.0f} GB",
        )

    if disk_io.get("read_mb") is not None:
        t.add_row("I/O", Text(f"Read {disk_io['read_mb']:.0f} MB  Write {disk_io['write_mb']:.0f} MB", style="dim"), "")

    return Panel(t, title="[bold yellow]Disk[/bold yellow]", border_style="yellow", box=box.ROUNDED)


def build_network_panel(snap: dict) -> Panel:
    net = snap["network"]
    t = Table.grid(padding=(0, 1))
    t.add_column(style="dim", width=14)
    t.add_column()

    if net["sent_mb"] is not None:
        t.add_row("Sent (cumul.)", f"{net['sent_mb']:.1f} MB  ({net['packets_sent']:,} pkts)")
        t.add_row("Recv (cumul.)", f"{net['recv_mb']:.1f} MB  ({net['packets_recv']:,} pkts)")
    else:
        t.add_row("Network", "Unavailable")
    return Panel(t, title="[bold blue]Network[/bold blue]", border_style="blue", box=box.ROUNDED)


def build_process_panel(snap: dict) -> Panel:
    top_cpu = snap["top_cpu_processes"]
    top_mem = snap["top_mem_processes"]

    outer = Table.grid(padding=(0, 3))
    outer.add_column()
    outer.add_column()

    def proc_table(procs: list) -> Table:
        t = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold dim", padding=(0, 1))
        t.add_column("PID",    style="dim", width=7)
        t.add_column("Name",   width=20)
        t.add_column("CPU%",   width=7, justify="right")
        t.add_column("MEM%",   width=7, justify="right")
        t.add_column("Status", width=10)
        for p in procs:
            cpu_pct = p.get("cpu_percent") or 0.0
            mem_pct = p.get("memory_percent") or 0.0
            t.add_row(
                str(p.get("pid", "?")),
                (p.get("name") or "?")[:20],
                Text(f"{cpu_pct:.1f}", style=color_for(min(cpu_pct, 100))),
                Text(f"{mem_pct:.1f}", style=color_for(mem_pct)),
                p.get("status", "?"),
            )
        return t

    outer.add_row(Text("▶ Top 5 by CPU",    style="bold dim"),
                  Text("▶ Top 5 by Memory", style="bold dim"))
    outer.add_row(proc_table(top_cpu), proc_table(top_mem))

    return Panel(outer, title="[bold white]Processes[/bold white]", border_style="white", box=box.ROUNDED)


def build_layout(snap: dict, stats: SystemStats, log_path) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header",    size=3),
        Layout(name="top",       size=14),
        Layout(name="bottom",    size=10),
        Layout(name="processes"),
    )
    layout["top"].split_row(Layout(name="cpu"), Layout(name="mem"))
    layout["bottom"].split_row(Layout(name="disk"), Layout(name="net"))

    layout["header"].update(build_header(snap, log_path))
    layout["cpu"].update(build_cpu_panel(snap, stats.history))
    layout["mem"].update(build_memory_panel(snap, stats.history))
    layout["disk"].update(build_disk_panel(snap))
    layout["net"].update(build_network_panel(snap))
    layout["processes"].update(build_process_panel(snap))
    return layout


# ─── Logging ───────────────────────────────────────────────────────────────────

class DataLogger:
    def __init__(self, log_file: Path):
        self.log_file = log_file
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self._entries = []
        if log_file.exists():
            try:
                with open(log_file) as f:
                    self._entries = json.load(f)
            except Exception:
                self._entries = []

    def append(self, snap: dict):
        entry = {k: v for k, v in snap.items()}
        self._entries.append(entry)
        if len(self._entries) > MAX_LOG_ENTRIES:
            self._entries = self._entries[-MAX_LOG_ENTRIES:]

    def flush(self):
        try:
            with open(self.log_file, "w") as f:
                json.dump(self._entries, f, indent=2)
        except Exception:
            pass


# ─── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="SysMon — cross-platform CLI system monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python monitor.py                   # Run with defaults (1s refresh)
  python monitor.py -i 2              # Refresh every 2 seconds
  python monitor.py --no-log          # Don't write a log file
  python monitor.py -l ~/mylog.json   # Custom log path
  python monitor.py --dump            # Print one JSON snapshot and exit
  python monitor.py --dashboard       # Open analytics dashboard in browser
        """,
    )
    p.add_argument("-i", "--interval", type=float, default=DEFAULT_INTERVAL,
                   help="Refresh interval in seconds (default: 1.0)")
    p.add_argument("-l", "--log",      type=Path,  default=DEFAULT_LOG_FILE,
                   help=f"Log file path (default: {DEFAULT_LOG_FILE})")
    p.add_argument("--no-log",         action="store_true",
                   help="Disable data logging")
    p.add_argument("--dump",           action="store_true",
                   help="Print a single JSON snapshot and exit")
    p.add_argument("--dashboard",      action="store_true",
                   help="Open the analytics dashboard in your default browser")
    return p.parse_args()


def start_dashboard_server(directory: Path, port: int) -> bool:
    """Serve project folder over localhost so fetch() works."""
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)
        def log_message(self, *args):
            pass  # suppress access logs

    try:
        socketserver.TCPServer.allow_reuse_address = True
        server = socketserver.TCPServer(("127.0.0.1", port), Handler)
    except OSError:
        # Port already in use — server is likely already running from a prior start
        return True

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return True


def open_dashboard(log_file: Path):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Serve from the project root so both dashboard.html and output/ are accessible
    project_dir = DASHBOARD_FILE.parent
    ok = start_dashboard_server(project_dir, DASHBOARD_PORT)
    if ok:
        # Pass the log filename as a query param so the dashboard can pre-load it
        log_rel = log_file.relative_to(project_dir).as_posix() if log_file.is_relative_to(project_dir) else None
        query   = f"?log={log_rel}" if log_rel else ""
        url     = f"http://127.0.0.1:{DASHBOARD_PORT}/dashboard.html{query}"
        print(f"Dashboard: {url}")
        time.sleep(0.3)
        webbrowser.open(url)
    sys.exit(0)


def main():
    args = parse_args()

    if args.dashboard:
        open_dashboard(args.log)

    stats   = SystemStats()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logger  = None if args.no_log else DataLogger(args.log)
    console = Console()
    log_path = None if args.no_log else args.log

    psutil.cpu_percent(interval=0.1)  # warmup

    if args.dump:
        snap = stats.collect()
        print(json.dumps(snap, indent=2))
        return

    # Always serve dashboard in background so --dashboard or browser open works
    project_dir = DASHBOARD_FILE.parent
    start_dashboard_server(project_dir, DASHBOARD_PORT)

    # Build the live-mode URL (pre-loads the log file automatically)
    try:
        log_rel = log_path.relative_to(project_dir).as_posix() if log_path else None
    except ValueError:
        log_rel = None
    live_url = f"http://127.0.0.1:{DASHBOARD_PORT}/dashboard.html" + (f"?log={log_rel}" if log_rel else "")

    # ── Startup banner (printed before Live screen takes over) ──────────────
    console.print()
    console.print("  [bold cyan]⚡ SysMon[/bold cyan]  starting up…")
    console.print()
    console.print(f"  [dim]Serving from :[/dim]  [white]{project_dir}[/white]")
    console.print(f"  [dim]Log file     :[/dim]  [white]{log_path.name if log_path else 'disabled'}[/white]")
    console.print(f"  [dim]Dashboard    :[/dim]  [bold cyan]{live_url}[/bold cyan]")
    console.print()
    console.print("  [dim]Open the URL above in your browser for live analytics.[/dim]")
    console.print("  [dim]Live Mode will start automatically with the current log.[/dim]")
    console.print()
    console.print("  [dim]Starting monitor in 3 seconds…  (Ctrl+C to cancel)[/dim]")
    console.print()
    time.sleep(3)

    flush_every = max(1, int(10 / args.interval))
    tick = 0

    def on_exit(*_):
        if logger:
            logger.flush()
        sys.exit(0)

    signal.signal(signal.SIGINT,  on_exit)
    signal.signal(signal.SIGTERM, on_exit)

    try:
        with Live(console=console, refresh_per_second=int(1 / args.interval) + 1,
                  screen=True) as live:
            while True:
                snap = stats.collect()

                if logger:
                    logger.append(snap)
                    tick += 1
                    if tick % flush_every == 0:
                        logger.flush()

                layout = build_layout(snap, stats, log_path)

                footer_parts = ["[dim]Ctrl+C to exit[/dim]"]
                if log_path:
                    footer_parts.append(f"[dim]log:[/dim] [white]{log_path.name}[/white]")
                footer_parts.append(f"[dim]dashboard →[/dim] [cyan]http://127.0.0.1:{DASHBOARD_PORT}/dashboard.html[/cyan]")
                footer_parts.append(f"[dim]interval={args.interval}s[/dim]")
                footer_markup = "  [dim]·[/dim]  ".join(footer_parts)
                footer_text   = Text.from_markup(footer_markup, justify="center")

                root = Layout()
                root.split_column(
                    Layout(layout, name="main"),
                    Layout(Panel(footer_text,
                                 box=box.SIMPLE, style="on grey11"),
                           name="footer", size=3),
                )
                live.update(root)
                time.sleep(args.interval)

    except Exception:
        if logger:
            logger.flush()
        console.print_exception()


if __name__ == "__main__":
    main()