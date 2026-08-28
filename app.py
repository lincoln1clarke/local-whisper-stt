"""Entry point.

    pythonw app.py              start (this is what the Startup shortcut runs)
    python  app.py --status     inspect the running instance
    python  app.py --reload     re-read config.json and the word lists
    python  app.py --quit       stop it
    python  app.py --list-devices

A second launch does not start a rival hook: it becomes a client and talks to
the running instance over the control pipe.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from lwstt import control
from lwstt.core.config import load_config
from lwstt.win.system import SingleInstance, message_box

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
LOG_PATH = ROOT / "logs" / "app.log"


def write_log(message: str) -> None:
    stamped = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(stamped + "\n")
    except OSError:
        pass
    try:
        print(stamped, file=sys.stderr, flush=True)
    except Exception:
        pass  # pythonw has no console


def list_devices() -> int:
    from lwstt.win.audio import list_devices as enumerate_devices

    devices = enumerate_devices()
    if not devices:
        print("no capture devices found")
        return 1
    print("index  channels  name")
    for device in devices:
        print(f"{device.index:>5}  {device.channels:>8}  {device.name}")
    print('\nSet "audio": {"input_device": <index>} in config.json, or null for the default.')
    return 0


def send(command: str) -> int:
    try:
        reply = control.send_command(command)
    except FileNotFoundError:
        print("not running")
        return 1
    except OSError as exc:
        print(f"could not reach the running instance: {exc}")
        return 1
    print(json.dumps(reply, indent=2))
    return 0 if reply.get("ok", False) else 1


def run_supervisor() -> int:
    guard = SingleInstance()
    if not guard.acquire():
        print("already running -- use --status, --reload or --quit")
        return 1

    config, warnings = load_config(CONFIG_PATH)
    for warning in warnings:
        write_log(f"config: {warning}")

    from lwstt.supervisor import Supervisor

    try:
        supervisor = Supervisor(config, ROOT, log=write_log)
    except Exception as exc:
        # A fatal startup error is otherwise completely silent: no tray icon, no
        # console. The tool would look installed and simply do nothing.
        write_log(f"fatal: {exc}")
        message_box(f"local-whisper-stt could not start:\n\n{exc}\n\nSee {LOG_PATH}")
        guard.release()
        return 1

    def handle(command: str) -> dict:
        if command == "quit":
            threading_stop()
            return {"ok": True, "message": "shutting down"}
        if command == "reload":
            new_config, new_warnings = load_config(CONFIG_PATH)
            supervisor.config = new_config
            for warning in new_warnings:
                write_log(f"config: {warning}")
            return {
                "ok": True,
                "warnings": new_warnings,
                "note": "hotkey changes need a restart; word lists reload per dictation",
            }
        if command == "status":
            from lwstt.win.system import battery_percent, on_battery

            return {
                "ok": True,
                "hotkey": config.hotkey.keys,
                "armed": supervisor.machine.armed,
                "dictating": supervisor.dictating,
                "worker_alive": supervisor.worker.alive,
                "worker_models_loaded": supervisor.worker.models_loaded,
                "worker_idle_s": round(supervisor.worker.idle_seconds(), 1)
                if supervisor.worker.alive
                else None,
                "on_battery": on_battery(),
                "battery_percent": battery_percent(),
            }
        return {"ok": False, "error": f"unknown command: {command!r}"}

    def threading_stop() -> None:
        supervisor.stopping.set()
        if supervisor.hook:
            supervisor.hook.stop()

    server = control.ControlServer(handle, log=write_log)
    try:
        server.start()
    except OSError as exc:
        write_log(f"control pipe unavailable: {exc}")

    write_log(f"started; hotkey = {' + '.join(config.hotkey.keys)}")
    try:
        supervisor.run()
    except Exception as exc:
        write_log(f"fatal: {exc}")
        message_box(f"local-whisper-stt stopped:\n\n{exc}\n\nSee {LOG_PATH}")
        return 1
    finally:
        server.stop()
        guard.release()
        write_log("stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local-whisper-stt", description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--quit", action="store_true", help="stop the running instance")
    group.add_argument("--reload", action="store_true", help="re-read config and word lists")
    group.add_argument("--status", action="store_true", help="report worker and power state")
    group.add_argument("--list-devices", action="store_true", help="enumerate microphones")
    args = parser.parse_args(argv)

    if args.list_devices:
        return list_devices()
    if args.quit:
        return send("quit")
    if args.reload:
        return send("reload")
    if args.status:
        return send("status")
    return run_supervisor()


if __name__ == "__main__":
    sys.exit(main())
