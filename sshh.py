#!/usr/bin/env python3
"""sshh - interactive SSH manager with folder-based grouping."""

import curses
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "sshh" / "hosts.json"


# ── Config ──────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        default = {"groups": {"Default": []}}
        save_config(default)
        return default
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ── SSH reachability check ────────────────────────────────────────────────────

def check_host(host: dict, results: dict):
    addr = host["host"]
    port = host.get("port", 22)
    try:
        with socket.create_connection((addr, port), timeout=3):
            results[addr] = True
    except Exception:
        results[addr] = False


def check_all_hosts(cfg: dict) -> dict:
    results = {}
    threads = []
    for hosts in cfg["groups"].values():
        for h in hosts:
            t = threading.Thread(target=check_host, args=(h, results), daemon=True)
            threads.append(t)
            t.start()
    for t in threads:
        t.join()
    return results


# ── SSH ──────────────────────────────────────────────────────────────────────

def connect(host: dict):
    parts = ["ssh"]
    if host.get("port") and host["port"] != 22:
        parts += ["-p", str(host["port"])]
    if host.get("identity"):
        parts += ["-i", host["identity"]]
    if "ghostty" in os.environ.get("TERM", ""):
        parts += ["-o", "SetEnv=TERM=xterm-256color"]
    target = f"{host['user']}@{host['host']}" if host.get("user") else host["host"]
    parts.append(target)
    os.execvp("ssh", parts)


# ── TUI ──────────────────────────────────────────────────────────────────────

class Item:
    def __init__(self, kind, label, data=None, depth=0):
        self.kind = kind      # "group" | "host"
        self.label = label
        self.data = data      # host dict or group name
        self.depth = depth


def build_items(cfg: dict, collapsed: set, reachable: dict) -> list:
    items = []
    for group, hosts in cfg["groups"].items():
        items.append(Item("group", group, group, 0))
        if group not in collapsed:
            for h in hosts:
                label = h.get("name") or h["host"]
                info = f"  {h.get('user', '')}@{h['host']}"
                if h.get("port") and h["port"] != 22:
                    info += f":{h['port']}"
                status = reachable.get(h["host"])
                if status is True:
                    sym = "●"
                elif status is False:
                    sym = "○"
                else:
                    sym = "…"
                items.append(Item("host", label, h, 1))
                items[-1].info = info
                items[-1].status_sym = sym
                items[-1].reachable = status
    return items


def _catppuccin_rgb(h: str):
    """Convert hex color to curses RGB (0-1000 scale)."""
    h = h.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return int(r / 255 * 1000), int(g / 255 * 1000), int(b / 255 * 1000)


def find_host(items: list, search_term: str) -> int:
    """Find the best matching host: startswith takes priority over contains."""
    term = search_term.lower()
    for i, item in enumerate(items):
        if item.kind == "host" and item.label.lower().startswith(term):
            return i
    for i, item in enumerate(items):
        if item.kind == "host" and term in item.label.lower():
            return i
    return -1


def run_tui(stdscr, cfg: dict, reachable: dict):
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()

    if curses.can_change_color() and curses.COLORS >= 16:
        # Catppuccin Mocha
        curses.init_color(8,  *_catppuccin_rgb("#cba6f7"))  # Mauve
        curses.init_color(9,  *_catppuccin_rgb("#a6e3a1"))  # Green
        curses.init_color(10, *_catppuccin_rgb("#cdd6f4"))  # Text
        curses.init_color(11, *_catppuccin_rgb("#a6adc8"))  # Subtext0
        curses.init_color(12, *_catppuccin_rgb("#f38ba8"))  # Red
        curses.init_color(13, *_catppuccin_rgb("#6c7086"))  # Overlay0 (dimmed)
        curses.init_color(14, *_catppuccin_rgb("#89b4fa"))  # Blue
        curses.init_pair(1, 8,  -1)   # group header:  Mauve
        curses.init_pair(2, 9,  -1)   # selected:      Green
        curses.init_pair(3, 10, -1)   # host text:     Text
        curses.init_pair(4, 11, -1)   # info/IP:       Subtext0
        curses.init_pair(5, 12, -1)   # unreachable:   Red
        curses.init_pair(6, 13, -1)   # pending:       Overlay0
        curses.init_pair(7, 14, -1)   # title/divider: Blue
    else:
        curses.init_pair(1, curses.COLOR_CYAN,    -1)
        curses.init_pair(2, curses.COLOR_GREEN,   -1)
        curses.init_pair(3, curses.COLOR_WHITE,   -1)
        curses.init_pair(4, curses.COLOR_YELLOW,  -1)
        curses.init_pair(5, curses.COLOR_RED,     -1)
        curses.init_pair(6, curses.COLOR_WHITE,   -1)
        curses.init_pair(7, curses.COLOR_CYAN,    -1)

    curses.mousemask(
        curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED | curses.REPORT_MOUSE_POSITION
    )

    collapsed: set = set()
    cur = 0
    search = ""
    last_click: tuple = (-1, -1, 0.0)  # (row, idx, time)
    stdscr.timeout(300)

    while True:
        items = build_items(cfg, collapsed, reachable)
        h, w = stdscr.getmaxyx()

        stdscr.erase()

        # Header
        title = " SEX SELECTOR "
        stdscr.addstr(0, (w - len(title)) // 2, title,
                      curses.color_pair(7) | curses.A_BOLD)
        stdscr.addstr(1, 0, "─" * w, curses.color_pair(6))

        list_h = h - 5
        # Keep cur in bounds
        if not items:
            cur = 0
        else:
            cur = max(0, min(cur, len(items) - 1))

        # Scroll offset
        offset = max(0, cur - list_h + 1)

        host_items = [it for it in items if it.kind == "host"]
        col_ip = max((len("    " + it.label) for it in host_items), default=20) + 2
        col_status = col_ip + max((len(getattr(it, "info", "")) for it in host_items), default=20) + 2

        for i, item in enumerate(items[offset:offset + list_h]):
            idx = i + offset
            y = 2 + i
            is_sel = idx == cur

            if item.kind == "group":
                arrow = "▼" if item.data not in collapsed else "▶"
                text = f" {arrow} {item.label}"
                attr = curses.color_pair(1) | curses.A_BOLD
                if is_sel:
                    attr |= curses.A_REVERSE
                stdscr.addstr(y, 0, text.ljust(w - 1)[:w - 1], attr)
            else:
                indent = "    "
                text = f"{indent}{item.label}"
                info = getattr(item, "info", "")
                sym = getattr(item, "status_sym", "…")
                is_reachable = getattr(item, "reachable", None)
                host_color = curses.color_pair(5) if is_reachable is False else curses.color_pair(3)
                sym_color = curses.color_pair(2) if is_reachable is True else (curses.color_pair(5) if is_reachable is False else curses.color_pair(6))
                if is_sel:
                    sel_color = curses.color_pair(5) if is_reachable is False else curses.color_pair(2)
                    stdscr.addstr(y, 0, text.ljust(w - 1)[:w - 1],
                                  sel_color | curses.A_REVERSE)
                else:
                    stdscr.addstr(y, 0, text.ljust(col_ip)[:col_ip], host_color)
                    if col_ip < w - 2:
                        stdscr.addstr(y, col_ip, info[:col_status - col_ip], curses.color_pair(4))
                    if col_status < w - 2:
                        stdscr.addstr(y, col_status, sym, sym_color)

        # Search bar
        search_prefix = " > "
        search_line = (search_prefix + search)[:w - 2]
        stdscr.addstr(h - 3, 0, search_line, curses.color_pair(7) | curses.A_BOLD)
        cursor_x = min(len(search_line), w - 2)
        stdscr.addstr(h - 3, cursor_x, "█", curses.color_pair(7))

        # Footer
        stdscr.addstr(h - 2, 0, "─" * w, curses.color_pair(6))
        stdscr.addstr(h - 1, 0, " ", curses.color_pair(6))
        stdscr.addstr(h - 1, 1, "↑↓", curses.color_pair(7) | curses.A_BOLD)
        stdscr.addstr(h - 1, 3, "/click navigate  ", curses.color_pair(6))
        stdscr.addstr(h - 1, 19, "Enter", curses.color_pair(7) | curses.A_BOLD)
        stdscr.addstr(h - 1, 24, "/dblclick connect  ", curses.color_pair(6))
        stdscr.addstr(h - 1, 43, "Space", curses.color_pair(7) | curses.A_BOLD)
        stdscr.addstr(h - 1, 48, " collapse  ", curses.color_pair(6))
        stdscr.addstr(h - 1, 59, "e", curses.color_pair(7) | curses.A_BOLD)
        stdscr.addstr(h - 1, 60, " edit  ", curses.color_pair(6))
        stdscr.addstr(h - 1, 67, "Esc", curses.color_pair(7) | curses.A_BOLD)
        stdscr.addstr(h - 1, 70, " clear/quit  ", curses.color_pair(6))
        

        stdscr.refresh()
        key = stdscr.getch()

        if key == 27:  # ESC: clear search or quit
            if search:
                search = ""
            else:
                return None

        elif key == curses.KEY_UP:
            cur = max(0, cur - 1)

        elif key == curses.KEY_DOWN:
            cur = min(len(items) - 1, cur + 1)

        elif key in (curses.KEY_ENTER, 10, 13, ord(" ")):
            if not items:
                continue
            item = items[cur]
            if item.kind == "group":
                if item.data in collapsed:
                    collapsed.discard(item.data)
                else:
                    collapsed.add(item.data)
            elif key in (curses.KEY_ENTER, 10, 13):
                return item.data

        elif key == ord("e"):
            curses.endwin()
            subprocess.run(["vim", str(CONFIG_PATH)])
            cfg = load_config()
            reachable = {}
            for hosts in cfg["groups"].values():
                for h in hosts:
                    threading.Thread(target=check_host, args=(h, reachable), daemon=True).start()
            time.sleep(0.1)

        elif key == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
            except curses.error:
                continue
            # Map screen row to items list index
            item_row = my - 2  # rows 2..h-3 are list items
            idx = item_row + offset
            if 0 <= idx < len(items):
                item = items[idx]
                now = time.monotonic()
                is_double = (
                    bstate & curses.BUTTON1_DOUBLE_CLICKED
                    or (
                        last_click[1] == idx
                        and now - last_click[2] < 0.5
                    )
                )
                last_click = (my, idx, now)
                cur = idx
                if item.kind == "group" and is_double:
                    if item.data in collapsed:
                        collapsed.discard(item.data)
                    else:
                        collapsed.add(item.data)
                elif item.kind == "host" and is_double:
                    return item.data

        elif key in (curses.KEY_BACKSPACE, 127, 8):
            if search:
                search = search[:-1]
                found = find_host(items, search) if search else -1
                if found != -1:
                    cur = found

        elif 32 <= key <= 126:
            search += chr(key)
            if search == ":q":
                return None
            found = find_host(items, search)
            if found != -1:
                cur = found


# ── CLI commands ─────────────────────────────────────────────────────────────

def cmd_add():
    cfg = load_config()
    groups = list(cfg["groups"].keys())

    print("Groups:", ", ".join(groups))
    group = input("Group (Enter = Default): ").strip() or "Default"
    if group not in cfg["groups"]:
        cfg["groups"][group] = []

    name = input("Name (display label): ").strip()
    host = input("Host / IP: ").strip()
    if not host:
        print("Host cannot be empty.")
        sys.exit(1)
    user = input("User (Enter = current): ").strip()
    port_s = input("Port (Enter = 22): ").strip()
    port = int(port_s) if port_s else 22
    identity = input("SSH key (path, Enter = /Users/user/.ssh/masterkey): ").strip() or "/Users/user/.ssh/masterkey"

    entry = {"host": host}
    if name:
        entry["name"] = name
    if user:
        entry["user"] = user
    if port != 22:
        entry["port"] = port
    if identity:
        entry["identity"] = identity

    cfg["groups"][group].append(entry)
    save_config(cfg)
    print(f"✓ Added: {name or host} → {group}")


def cmd_remove():
    cfg = load_config()
    all_hosts = []
    for group, hosts in cfg["groups"].items():
        for h in hosts:
            all_hosts.append((group, h))

    if not all_hosts:
        print("No hosts.")
        return

    for i, (group, h) in enumerate(all_hosts):
        label = h.get("name") or h["host"]
        print(f"  {i + 1}. [{group}] {label}  ({h.get('user', '')}@{h['host']})")

    choice = input("\nNumber to remove (Enter = cancel): ").strip()
    if not choice:
        return
    idx = int(choice) - 1
    if not 0 <= idx < len(all_hosts):
        print("Invalid number.")
        sys.exit(1)

    group, host = all_hosts[idx]
    cfg["groups"][group].remove(host)
    if not cfg["groups"][group]:
        del_group = input(f"Group '{group}' is empty. Delete it? [y/N] ").strip().lower()
        if del_group == "y":
            del cfg["groups"][group]
    save_config(cfg)
    print(f"✓ Removed: {host.get('name') or host['host']}")


def cmd_list():
    cfg = load_config()
    for group, hosts in cfg["groups"].items():
        print(f"\n[{group}]")
        for h in hosts:
            label = h.get("name") or h["host"]
            user = h.get("user", "")
            port = h.get("port", 22)
            line = f"  {label}"
            if user:
                line += f"  {user}@{h['host']}"
            else:
                line += f"  {h['host']}"
            if port != 22:
                line += f":{port}"
            print(line)


def usage():
    print(
        "sshh — interactive SSH manager\n"
        "\n"
        "Usage:\n"
        "  sshh           — open interactive selector\n"
        "  sshh add       — add a host\n"
        "  sshh remove    — remove a host\n"
        "  sshh list      — list all hosts\n"
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if args and args[0] == "add":
        cmd_add()
        return
    if args and args[0] in ("remove", "rm", "del"):
        cmd_remove()
        return
    if args and args[0] in ("list", "ls"):
        cmd_list()
        return
    if args and args[0] in ("-h", "--help", "help"):
        usage()
        return

    cfg = load_config()
    reachable: dict = {}
    for hosts in cfg["groups"].values():
        for h in hosts:
            threading.Thread(target=check_host, args=(h, reachable), daemon=True).start()
    host = curses.wrapper(run_tui, cfg, reachable)
    if host:
        connect(host)


if __name__ == "__main__":
    main()
