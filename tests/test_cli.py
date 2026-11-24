import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

import cf_share_tmux


def run_cli(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "cf_share_tmux", *args],
        capture_output=True, text=True, check=False, env=env, timeout=30,
    )


class ArgumentTests(unittest.TestCase):
    def test_help(self):
        result = run_cli("--help")
        self.assertEqual(result.returncode, 0)
        for option in ("--cf-tunnel", "--json", "--read-only", "--credential"):
            self.assertIn(option, result.stdout)

    def test_version(self):
        result = run_cli("--version")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, f"cf-share-tmux {cf_share_tmux.__version__}\n")

    def test_invalid_arguments_are_rejected(self):
        cases = {
            ("work:1", "--port", "0"): "Invalid port",
            ("work:1", "-c", "nopassword"): "USER:PASSWORD",
            ("work:1", "-c", ":secret"): "USER:PASSWORD",
            ("work:1", "--cf-tunnel"): "--cf-tunnel requires Basic Auth",
            ("work:1", "--json"): "--json requires --cf-tunnel",
        }
        env = {k: v for k, v in os.environ.items() if k != "TTYD_CREDENTIAL"}
        for args, message in cases.items():
            result = run_cli(*args, env=env)
            self.assertEqual(result.returncode, 2, args)
            self.assertIn(message, result.stderr, args)

    def test_credentials_from_environment(self):
        with tempfile.TemporaryDirectory() as empty:
            env = dict(os.environ, PATH=empty, TTYD_CREDENTIAL="user:secret")
            result = run_cli("work:1", "--cf-tunnel", env=env)
        # Arguments are accepted; it fails later, on the missing executables.
        self.assertEqual(result.returncode, 1)
        self.assertIn("Missing executable: tmux", result.stderr)

    def test_missing_executables(self):
        with tempfile.TemporaryDirectory() as empty:
            env = dict(os.environ, PATH=empty)
            result = run_cli("work:1", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Missing executable: tmux", result.stderr)
        self.assertEqual(result.stdout, "")


class FakeProcess:
    def __init__(self, lines):
        self.lines = iter(lines)
        self.stderr = self.lines


def watch(lines):
    process = FakeProcess(lines)
    urls = []
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        cf_share_tmux.watch_tunnel(process, urls.append)
    leftover = list(process.lines)
    return urls, out.getvalue(), err.getvalue(), leftover


URL_LINES = [
    "INF Requesting new quick Tunnel on trycloudflare.com...\n",
    "INF |  https://some-random-words.trycloudflare.com  |\n",
    "INF Registered tunnel connection\n",
]
API_ERROR = 'ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel"\n'


class WatchTunnelTests(unittest.TestCase):
    def test_reports_public_url_once_and_drains_log(self):
        urls, out, err, leftover = watch(URL_LINES + URL_LINES)
        self.assertEqual(urls, ["https://some-random-words.trycloudflare.com"])
        self.assertEqual((out, err), ("", ""), "the log itself stays hidden")
        self.assertEqual(leftover, [], "the log must be drained to the end")

    def test_ignores_api_host_and_shows_log_tail(self):
        urls, out, err, _ = watch([API_ERROR, "ERR giving up\n"])
        self.assertEqual(urls, [])
        self.assertEqual(out, "")
        self.assertEqual(err, API_ERROR + "ERR giving up\n")

    def test_log_tail_is_limited(self):
        lines = [f"line {i}\n" for i in range(50)]
        _, _, err, _ = watch(lines)
        self.assertEqual(err, "".join(lines[-20:]))


class HttpUrlTests(unittest.TestCase):
    def test_formats_hosts(self):
        self.assertEqual(cf_share_tmux.http_url("127.0.0.1", 7681), "http://127.0.0.1:7681")
        self.assertEqual(cf_share_tmux.http_url("::1", 7681), "http://[::1]:7681")
        self.assertEqual(cf_share_tmux.http_url("[::1]", 7681), "http://[::1]:7681")
