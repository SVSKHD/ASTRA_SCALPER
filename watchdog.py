from __future__ import annotations

# =============================================================================
# WATCHDOG — monitors runner.py, auto-restarts on crash
#
# Usage:
#   python watchdog.py                           # runs runner.py forever
#   python watchdog.py --script runner.py        # explicit script
#   python watchdog.py --max-restarts 10         # limit restarts
#   python watchdog.py --cooldown 30             # seconds between restarts
#   python watchdog.py --backoff                 # exponential backoff
#   python watchdog.py --notify <discord_url>    # crash alerts
#
# FIX: stdout/stderr now use subprocess.PIPE (not sys.stdout directly)
# so the watchdog works correctly when stdout is redirected in tests or
# non-terminal environments (cron, IDE, pytest).
# Output is forwarded line-by-line to the watchdog's own stdout.
# =============================================================================

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

LOG_FILE = "watchdog.log"
logging.basicConfig(
    level    = logging.INFO,
    format   = "%(asctime)s [%(levelname)s] %(message)s",
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("watchdog")

CRASH_LOG = Path("crash.log")


# =============================================================================
# HELPERS
# =============================================================================

def _discord_notify(webhook_url: str, title: str, message: str):
    """Send Discord webhook. Fails silently."""
    try:
        import urllib.request, json as _json
        payload = _json.dumps({
            "embeds": [{"title": title, "description": message,
                        "color": 16711680 if "CRASH" in title else 255}]
        }).encode()
        req = urllib.request.Request(
            webhook_url, data=payload,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        log.warning(f"Discord notify failed: {e}")


def _write_crash_log(restart_no: int, returncode: int, output_tail: str):
    ts = datetime.now(timezone.utc).isoformat()
    CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with CRASH_LOG.open("a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"CRASH #{restart_no} at {ts}\n")
        f.write(f"Exit code: {returncode}\n")
        f.write(f"Output tail:\n{output_tail or '(none)'}\n")
        f.write(f"{'='*60}\n")


def _forward_output(stream, prefix=""):
    """Read lines from subprocess stream and print them. Runs in thread."""
    try:
        for line in iter(stream.readline, ""):
            print(f"{prefix}{line}", end="", flush=True)
    except Exception:
        pass


# =============================================================================
# WATCHDOG LOOP
# =============================================================================

def run_watchdog(
    script:         str   = "runner.py",
    max_restarts:   int   = 0,
    cooldown:       float = 5.0,
    backoff:        bool  = False,
    notify_webhook: str   = "",
    python_exe:     str   = sys.executable,
):
    log.info("=" * 60)
    log.info(f"  WATCHDOG STARTING")
    log.info(f"  Script       : {script}")
    log.info(f"  Max restarts : {'unlimited' if max_restarts == 0 else max_restarts}")
    log.info(f"  Cooldown     : {cooldown}s  backoff={backoff}")
    log.info("=" * 60)

    restart_count    = 0
    current_cooldown = cooldown
    child: subprocess.Popen | None = None

    def _kill_child():
        nonlocal child
        if child and child.poll() is None:
            log.info(f"Terminating child PID {child.pid}...")
            try:
                child.terminate()
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
        child = None

    def _sighandler(signum, frame):
        log.info("\n🛑 Watchdog received signal — shutting down.")
        _kill_child()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _sighandler)
    signal.signal(signal.SIGTERM, _sighandler)

    while True:
        if max_restarts > 0 and restart_count >= max_restarts:
            log.error(f"❌ Max restarts ({max_restarts}) reached. Watchdog stopping.")
            break

        log.info(f"🚀 Starting '{script}' (launch #{restart_count + 1}) ...")

        # FIX: Use subprocess.PIPE for both stdout and stderr
        # Forward output via threads so it still appears in the terminal.
        # This avoids fileno() errors when stdout is redirected.
        try:
            child = subprocess.Popen(
                [python_exe, script],
                stdout = subprocess.PIPE,
                stderr = subprocess.PIPE,
                text   = True,
                bufsize = 1,
            )
            log.info(f"   Child PID: {child.pid}")

            # Forward stdout + stderr in background threads
            stdout_buf = []
            stderr_buf = []

            def _read_stdout():
                for line in iter(child.stdout.readline, ""):
                    print(line, end="", flush=True)
                    stdout_buf.append(line)

            def _read_stderr():
                for line in iter(child.stderr.readline, ""):
                    print(line, end="", file=sys.stderr, flush=True)
                    stderr_buf.append(line)

            t_out = threading.Thread(target=_read_stdout, daemon=True)
            t_err = threading.Thread(target=_read_stderr, daemon=True)
            t_out.start()
            t_err.start()

            child.wait()
            t_out.join(timeout=2)
            t_err.join(timeout=2)

            returncode   = child.returncode
            output_tail  = "".join(stdout_buf[-50:] + stderr_buf[-50:])

        except FileNotFoundError:
            log.error(f"❌ Script not found: {script}")
            break
        except Exception as e:
            log.error(f"❌ Launch failed: {e}")
            returncode  = -1
            output_tail = str(e)

        # Clean exit (code 0) → intentional stop
        if returncode == 0:
            log.info("✅ Runner exited cleanly (code 0). Watchdog stopping.")
            break

        # Restart requested via /restart command (code 42) → relaunch without crash log
        if returncode == 42:
            log.info("🔄 Restart requested (code 42) — relaunching runner.py...")
            time.sleep(3.0)
            continue

        # Crash
        restart_count += 1
        output_snippet = output_tail[-800:]

        log.error(
            f"\n{'!'*60}\n"
            f"  💥 CRASH #{restart_count} | exit={returncode}\n"
            f"  Tail:\n{output_snippet or '(none)'}\n"
            f"{'!'*60}"
        )

        _write_crash_log(restart_count, returncode, output_snippet)

        if notify_webhook:
            _discord_notify(
                notify_webhook,
                title   = f"💥 RUNNER CRASH #{restart_count}",
                message = (
                    f"Script: `{script}`\n"
                    f"Exit code: `{returncode}`\n"
                    f"Restarting in `{current_cooldown:.0f}s`\n"
                    f"```{output_snippet[-400:]}```"
                ),
            )

        log.info(f"⏳ Cooling down {current_cooldown:.1f}s...")
        time.sleep(current_cooldown)

        if backoff:
            current_cooldown = min(current_cooldown * 2, 120.0)
        else:
            current_cooldown = cooldown

    log.info("Watchdog exiting.")


# =============================================================================
# CLI
# =============================================================================

def parse_args():
    p = argparse.ArgumentParser(
        description     = "Watchdog — monitors and auto-restarts the strategy runner",
        formatter_class = argparse.RawDescriptionHelpFormatter,
        epilog = """
Examples:
  python watchdog.py
  python watchdog.py --cooldown 10 --backoff
  python watchdog.py --max-restarts 5
  python watchdog.py --notify https://discord.com/api/webhooks/...
        """
    )
    p.add_argument("--script",       type=str,   default="runner.py")
    p.add_argument("--max-restarts", type=int,   default=0)
    p.add_argument("--cooldown",     type=float, default=5.0)
    p.add_argument("--backoff",      action="store_true")
    p.add_argument("--notify",       type=str,   default="")
    p.add_argument("--python",       type=str,   default=sys.executable)
    return p.parse_args()


def main():
    args = parse_args()
    run_watchdog(
        script         = args.script,
        max_restarts   = args.max_restarts,
        cooldown       = args.cooldown,
        backoff        = args.backoff,
        notify_webhook = args.notify,
        python_exe     = args.python,
    )


if __name__ == "__main__":
    main()