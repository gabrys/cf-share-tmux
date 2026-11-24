"""End-to-end tests with a real, isolated tmux server and fake ttyd/cloudflared."""
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent.parent

# The fakes record their PID (kept by exec) and arguments, then idle.
FAKE_TTYD = """#!/bin/sh
echo $$ > "$FAKE_OUT/ttyd.pid"
printf '%s\\n' "$@" > "$FAKE_OUT/ttyd-args"
exec sleep 300
"""
FAKE_CLOUDFLARED_OK = """#!/bin/sh
echo $$ > "$FAKE_OUT/cloudflared.pid"
echo "INF Requesting new quick Tunnel on trycloudflare.com..." >&2
echo "INF |  https://fake-tunnel-words.trycloudflare.com  |" >&2
exec sleep 300
"""
FAKE_CLOUDFLARED_FAIL = """#!/bin/sh
echo 'ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel"' >&2
exit 3
"""
PUBLIC_URL = "https://fake-tunnel-words.trycloudflare.com"


def running(pid):
    """True while pid exists and is not a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class SessionTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.tmp / "tmux").mkdir()
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TTYD_CREDENTIAL")}
        # TMUX_TMPDIR gives this test its own tmux server, separate from yours.
        env.update(PATH=f"{self.bin}{os.pathsep}{env['PATH']}",
                   TMUX_TMPDIR=str(self.tmp / "tmux"), FAKE_OUT=str(self.tmp))
        self.env = env
        self.fake("ttyd", FAKE_TTYD)
        self.fake("cloudflared", FAKE_CLOUDFLARED_OK)
        self.tmux("new-session", "-d", "-s", "work", "-n", "first")
        self.tmux("new-window", "-t", "work:", "-n", "second")
        self.addCleanup(subprocess.run, ["tmux", "kill-server"], env=self.env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    def fake(self, name, script):
        path = self.bin / name
        path.write_text(script)
        path.chmod(0o755)

    def tmux(self, *args):
        return subprocess.run(["tmux", *args], env=self.env, capture_output=True,
                              text=True, check=True).stdout.split()

    def sessions(self):
        return self.tmux("list-sessions", "-F", "#{session_name}")

    def windows(self, session):
        return self.tmux("list-windows", "-t", f"={session}", "-F", "#{window_name}")

    def temp_session(self):
        temp = [s for s in self.sessions() if s.startswith("serve-tmux-7681-")]
        self.assertEqual(len(temp), 1, self.sessions())
        return temp[0]

    def fake_pid(self, name):
        return int((self.tmp / f"{name}.pid").read_text())

    def wait_until(self, condition, message, timeout=10):
        deadline = time.monotonic() + timeout
        while not condition():
            self.assertLess(time.monotonic(), deadline, message)
            time.sleep(0.1)

    def kill_later(self, pid):
        def cleanup():
            if running(pid):
                os.kill(pid, signal.SIGKILL)
        self.addCleanup(cleanup)

    def start(self, *args):
        proc = subprocess.Popen(
            [sys.executable, "-m", "cf_share_tmux", *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env,
            cwd=PACKAGE_DIR,
        )
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def run_cli(self, *args):
        """Run in the default (background) mode; returns once the parent exits."""
        result = subprocess.run(
            [sys.executable, "-m", "cf_share_tmux", *args],
            capture_output=True, text=True, env=self.env, cwd=PACKAGE_DIR, timeout=30,
        )
        return result


class ForegroundTests(SessionTestCase):
    def test_tunnel_without_credentials(self):
        proc = self.start("work:first", "--foreground", "--cf-tunnel", "--json")
        [line] = self.read_lines(proc, 1)
        self.assertEqual(json.loads(line), {"url": PUBLIC_URL, "pid": proc.pid})
        args = (self.tmp / "ttyd-args").read_text().splitlines()
        self.assertNotIn("-c", args)
        self.assertEqual(self.stop(proc)[0], 128 + signal.SIGTERM)

    def test_base_path_in_json_and_ttyd_arguments(self):
        proc = self.start("work:first", "--foreground", "--cf-tunnel", "--json",
                          "-c", "user:secret", "--base-path", "/nested/terminal/")
        [line] = self.read_lines(proc, 1)
        self.assertEqual(json.loads(line),
                         {"url": PUBLIC_URL + "/nested/terminal", "pid": proc.pid})
        args = (self.tmp / "ttyd-args").read_text().splitlines()
        self.assertEqual(args[args.index("-b") + 1], "/nested/terminal")
        self.assertEqual(self.stop(proc)[0], 128 + signal.SIGTERM)

    def test_base_path_in_text_urls(self):
        proc = self.start("work:first", "--foreground", "--cf-tunnel",
                          "-c", "user:secret", "--base-path", "/terminal")
        lines = self.read_lines(proc, 7)
        self.assertEqual(lines[2], "Local:   http://127.0.0.1:7681/terminal\n")
        self.assertEqual(lines[6], f"Public:  {PUBLIC_URL}/terminal\n")
        self.assertEqual(self.stop(proc)[0], 128 + signal.SIGTERM)

    def read_lines(self, proc, count, timeout=10):
        lines = []
        while len(lines) < count:
            ready, _, _ = select.select([proc.stdout], [], [], timeout)
            self.assertTrue(ready, f"timed out; got {lines!r}")
            line = proc.stdout.readline()
            self.assertTrue(line, f"stdout closed; got {lines!r}")
            lines.append(line)
        return lines

    def stop(self, proc):
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=15)
        return proc.returncode, out, err

    def test_json_mode_links_window_and_cleans_up(self):
        proc = self.start("work:second", "--foreground", "--cf-tunnel", "--json",
                          "-c", "user:secret")
        [line] = self.read_lines(proc, 1)
        self.assertEqual(json.loads(line), {"url": PUBLIC_URL, "pid": proc.pid})

        session = self.temp_session()
        self.assertEqual(self.windows(session), ["second"])
        ttyd_args = (self.tmp / "ttyd-args").read_text().split("\n")
        for expected in ("-W", "-c", "user:secret", "attach-session", f"={session}"):
            self.assertIn(expected, ttyd_args)

        code, out, _ = self.stop(proc)
        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertEqual(out, "", "--json must print nothing else on stdout")
        self.assertEqual(self.sessions(), ["work"])
        self.assertEqual(self.windows("work"), ["first", "second"],
                         "the shared window must survive")
        self.assertFalse(running(self.fake_pid("ttyd")))
        self.assertFalse(running(self.fake_pid("cloudflared")))

    def test_text_mode_and_read_only(self):
        proc = self.start("work:first", "--foreground", "--cf-tunnel", "--read-only",
                          "-c", "user:secret")
        lines = self.read_lines(proc, 7)
        self.assertTrue(lines[0].startswith("Window:  work:first (@"))
        self.assertTrue(lines[1].startswith("Session: serve-tmux-7681-"))
        self.assertEqual(lines[2], "Local:   http://127.0.0.1:7681\n")
        self.assertEqual(lines[3], "Auth:    enabled\n")
        self.assertEqual(lines[4], f"PID:     {proc.pid} (stop with: kill {proc.pid})\n")
        self.assertEqual(lines[5], "Starting Cloudflare Quick Tunnel...\n")
        self.assertEqual(lines[6], f"Public:  {PUBLIC_URL}\n")
        self.assertNotIn("-W", (self.tmp / "ttyd-args").read_text().split("\n"))
        _, out, _ = self.stop(proc)
        self.assertEqual(out, "Shutting down...\n")
        self.assertEqual(self.sessions(), ["work"])

    def test_cloudflared_failure_shows_log_and_cleans_up(self):
        self.fake("cloudflared", FAKE_CLOUDFLARED_FAIL)
        proc = self.start("work:first", "--foreground", "--cf-tunnel", "--json",
                          "-c", "user:secret")
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(out, "")
        self.assertIn("api.trycloudflare.com/tunnel", err)
        self.assertIn("cloudflared exited with status 3", err)
        self.assertEqual(self.sessions(), ["work"])

    def test_unknown_window_fails_without_leftovers(self):
        proc = self.start("nosuch:9", "--foreground")
        _, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("nosuch:9", err)
        self.assertEqual(self.sessions(), ["work"])


class BackgroundTests(SessionTestCase):
    def start_daemon(self, *args):
        result = self.run_cli("work:second", "--cf-tunnel", "--json", "-c", "user:secret", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["url"], PUBLIC_URL)
        pid = data["pid"]
        self.kill_later(pid)
        return pid

    def test_returns_after_url_and_stops_on_kill(self):
        pid = self.start_daemon()
        self.assertTrue(running(pid), "the daemon keeps running after the command returns")
        session = self.temp_session()
        self.assertEqual(self.windows(session), ["second"])

        os.kill(pid, signal.SIGTERM)
        self.wait_until(lambda: not running(pid), "daemon did not stop")
        self.assertEqual(self.sessions(), ["work"])
        self.assertFalse(running(self.fake_pid("ttyd")))
        self.assertFalse(running(self.fake_pid("cloudflared")))

    def test_children_die_with_the_daemon_even_on_sigkill(self):
        pid = self.start_daemon()
        ttyd, cloudflared = self.fake_pid("ttyd"), self.fake_pid("cloudflared")
        os.kill(pid, signal.SIGKILL)
        self.wait_until(lambda: not running(ttyd) and not running(cloudflared),
                        "ttyd/cloudflared outlived the daemon")

    def test_stops_when_the_shared_window_is_closed(self):
        pid = self.start_daemon()
        self.tmux("kill-window", "-t", "work:second")
        self.wait_until(lambda: not running(pid), "daemon did not notice the closed window")
        self.assertEqual(self.sessions(), ["work"])
        self.assertFalse(running(self.fake_pid("ttyd")))

    def test_failure_before_ready_is_reported_by_the_command(self):
        self.fake("cloudflared", FAKE_CLOUDFLARED_FAIL)
        result = self.run_cli("work:first", "--cf-tunnel", "--json", "-c", "user:secret")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")
        self.assertIn("cloudflared exited with status 3", result.stderr)
        self.assertEqual(self.sessions(), ["work"])

    def test_without_tunnel_ready_after_ttyd_starts(self):
        result = self.run_cli("work:first", "--read-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Local:   http://127.0.0.1:7681\n", result.stdout)
        pid = int(result.stdout.split("PID:")[1].split()[0])
        self.kill_later(pid)
        self.assertTrue(running(pid))
        os.kill(pid, signal.SIGTERM)
        self.wait_until(lambda: not running(pid), "daemon did not stop")
        self.assertEqual(self.sessions(), ["work"])
