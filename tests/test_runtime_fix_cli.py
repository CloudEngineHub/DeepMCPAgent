"""``promptise runtime start | status | stop | restart | logs``: real control.

The subprocess tests run a real ``promptise runtime start`` with a scripted
model (a ``sitecustomize`` on ``PYTHONPATH`` swaps ``resolve_model`` for
:class:`_scripted_model.Scripted`, so detached children get it too) and a
private state directory (``$PROMPTISE_RUNTIME_STATE_DIR``).
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from promptise.runtime import _cli_state
from promptise.runtime._cli_state import RuntimeState, StateError
from promptise.runtime.cli import runtime_app

TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src"
POSIX = os.name == "posix"
READY_TIMEOUT = 90.0

_SITECUSTOMIZE = """\
import os
if os.environ.get("PROMPTISE_TEST_SCRIPTED_MODEL"):
    import sys
    sys.path.insert(0, os.environ["PROMPTISE_TEST_SCRIPTED_MODEL"])
    from _scripted_model import Scripted
    import promptise.models as _models

    def _resolve(spec, **kw):
        if os.environ.get("PROMPTISE_TEST_MODEL_FAILS"):
            raise RuntimeError("scripted model setup failure")
        return Scripted()

    _models.resolve_model = _resolve
"""

_MANIFEST = """\
version: "1.0"
name: {name}
model: openai:gpt-5-mini
instructions: Say ok.
triggers: []
journal:
  level: full
  backend: file
  path: journal
"""


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    path = tmp_path / "state"
    monkeypatch.setenv(_cli_state.STATE_DIR_ENV, str(path))
    monkeypatch.delenv(_cli_state.TOKEN_ENV, raising=False)
    yield path
    # Never leave a runtime behind, whatever the test did.
    for file in path.glob("*.json") if path.is_dir() else []:
        try:
            pid = int(json.loads(file.read_text(encoding="utf-8"))["pid"])
        except (OSError, ValueError, KeyError):
            continue
        if pid != os.getpid() and _cli_state.pid_alive(pid):
            _cli_state.kill_process(pid)


@pytest.fixture
def env(tmp_path, state_dir):
    """Environment for a ``promptise runtime`` subprocess."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(_SITECUSTOMIZE)
    e = dict(os.environ)
    e["PYTHONPATH"] = os.pathsep.join(
        [str(site), str(SRC_DIR)] + ([e["PYTHONPATH"]] if e.get("PYTHONPATH") else [])
    )
    e["PROMPTISE_TEST_SCRIPTED_MODEL"] = str(TESTS_DIR)
    e[_cli_state.STATE_DIR_ENV] = str(state_dir)
    e.pop(_cli_state.TOKEN_ENV, None)
    e["PYTHONUNBUFFERED"] = "1"
    e["PYTHONIOENCODING"] = "utf-8"
    e.setdefault("OPENAI_API_KEY", "sk-test-not-used")
    return e


@pytest.fixture
def workdir(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "hello.agent").write_text(_MANIFEST.format(name="hello"))
    return work


def _cli(*args: str, env: dict[str, str], cwd: Path, timeout: float = 60.0):
    return subprocess.run(
        [sys.executable, "-m", "promptise", "runtime", *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _popen_start(env, cwd, *args: str) -> subprocess.Popen:
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        [sys.executable, "-m", "promptise", "runtime", "start", *args],
        env=env,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )


def _wait_ready(state_dir: Path, name: str, proc: subprocess.Popen | None = None) -> dict:
    path = state_dir / f"{name}.json"
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            pytest.fail(f"runtime exited early ({proc.returncode}):\n{out}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("ready"):
                return data
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    pytest.fail(f"runtime {name!r} not ready within {READY_TIMEOUT}s")


def _finish(proc: subprocess.Popen, timeout: float = 60.0) -> tuple[int, str]:
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        pytest.fail(f"runtime did not exit:\n{out}")
    return proc.returncode, out


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _fake_state(name: str, pid: int | None = None, **extra) -> RuntimeState:
    """A state file for a runtime 'running' as this test process."""
    state = RuntimeState(
        name=name,
        pid=os.getpid() if pid is None else pid,
        host="127.0.0.1",
        port=9,  # discard port: nothing listens
        token="t",
        started_at=RuntimeState.now(),
        ready=True,
        **extra,
    )
    _cli_state.write_state(state, exclusive=False)
    return state


def _journal_line(i: int, entry_type: str = "state_transition") -> str:
    return json.dumps(
        {
            "entry_id": f"e{i}",
            "process_id": "worker",
            "timestamp": f"2026-10-19T10:00:{i:02d}+00:00",
            "entry_type": entry_type,
            "data": {"n": i},
        }
    )


runner = CliRunner()


# ---------------------------------------------------------------------------
# State files
# ---------------------------------------------------------------------------


class TestStateFiles:
    @pytest.mark.skipif(not POSIX, reason="POSIX file modes")
    def test_state_file_is_private(self, state_dir):
        _fake_state("alpha")
        assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE((state_dir / "alpha.json").stat().st_mode) == 0o600

    @pytest.mark.skipif(not POSIX, reason="POSIX file modes")
    def test_open_state_file_is_refused(self, state_dir):
        _fake_state("alpha")
        os.chmod(state_dir / "alpha.json", 0o644)
        result = runner.invoke(runtime_app, ["status"])
        assert result.exit_code == 1
        assert "other users can access it" in result.output

    @pytest.mark.skipif(not POSIX, reason="POSIX file modes")
    def test_open_state_dir_of_ours_is_tightened(self, state_dir):
        state_dir.mkdir(mode=0o755)
        os.chmod(state_dir, 0o755)
        _cli_state.state_dir()
        assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700

    @pytest.mark.skipif(not POSIX or os.geteuid() == 0, reason="needs a non-root POSIX user")
    def test_foreign_state_dir_is_refused(self, monkeypatch, state_dir):
        state_dir.mkdir(mode=0o700)
        real_uid = os.geteuid()
        monkeypatch.setattr(os, "geteuid", lambda: real_uid + 1)
        with pytest.raises(StateError, match="owned by uid"):
            _cli_state.state_dir()

    def test_stale_state_file_is_removed(self, state_dir):
        _fake_state("ghost", pid=_dead_pid())
        result = runner.invoke(runtime_app, ["status"])
        assert result.exit_code == 1
        assert "No runtime is running" in result.output
        assert not (state_dir / "ghost.json").exists()

    def test_malformed_state_file_is_removed(self, state_dir):
        _fake_state("ok")
        (state_dir / "broken.json").write_text("{not json")
        if POSIX:
            os.chmod(state_dir / "broken.json", 0o600)
        live, stale = _cli_state.list_states()
        assert [s.name for s in live] == ["ok"]
        assert stale == ["broken"]
        assert not (state_dir / "broken.json").exists()

    def test_exclusive_claim(self, state_dir):
        _fake_state("taken")
        with pytest.raises(StateError, match="already running"):
            _cli_state.write_state(
                RuntimeState(name="taken", pid=os.getpid(), host="h", port=1, token="t"),
                exclusive=True,
            )
        # A stale holder is replaced.
        _fake_state("old", pid=_dead_pid())
        mine = RuntimeState(name="old", pid=os.getpid(), host="h", port=1, token="t")
        _cli_state.write_state(mine, exclusive=True)
        assert _cli_state.find_state("old").pid == os.getpid()

    def test_remove_state_leaves_another_runtimes_file(self, state_dir):
        _fake_state("shared")
        other = RuntimeState(name="shared", pid=_dead_pid(), host="h", port=1, token="t")
        _cli_state.remove_state(other)
        assert (state_dir / "shared.json").exists()

    def test_invalid_runtime_name(self):
        with pytest.raises(StateError, match="Invalid runtime name"):
            _cli_state.validate_runtime_name("../etc")
        assert _cli_state.safe_runtime_name("my agents!") == "my-agents"

    def test_pid_alive(self):
        assert _cli_state.pid_alive(os.getpid())
        assert not _cli_state.pid_alive(_dead_pid())
        assert not _cli_state.pid_alive(0)


# ---------------------------------------------------------------------------
# Command errors (no runtime needed)
# ---------------------------------------------------------------------------


class TestCommandErrors:
    @pytest.mark.parametrize("command", ["status", "stop", "restart"])
    def test_no_runtime_running(self, state_dir, command):
        result = runner.invoke(runtime_app, [command])
        assert result.exit_code == 1
        assert "No runtime is running" in result.output
        assert "requires distributed mode" not in result.output

    def test_several_runtimes_need_a_name(self, state_dir):
        _fake_state("one")
        _fake_state("two")
        result = runner.invoke(runtime_app, ["stop"])
        assert result.exit_code == 1
        assert "Several runtimes are running" in result.output
        assert "one" in result.output and "two" in result.output

    def test_unknown_runtime_name(self, state_dir):
        _fake_state("one")
        result = runner.invoke(runtime_app, ["restart", "nope"])
        assert result.exit_code == 1
        assert "No runtime named 'nope'" in result.output

    def test_unreachable_runtime(self, state_dir):
        _fake_state("one", processes={"worker": {"journal": None}})
        result = runner.invoke(runtime_app, ["status", "worker"])
        assert result.exit_code == 1
        assert "Cannot reach runtime 'one'" in result.output

    def test_token_flag_needs_url(self, state_dir):
        result = runner.invoke(runtime_app, ["status", "--token", "abc"])
        assert result.exit_code == 1
        assert "--token only applies with --url" in result.output

    def test_token_env_without_url_is_fine(self, state_dir, monkeypatch):
        monkeypatch.setenv(_cli_state.TOKEN_ENV, "abc")
        result = runner.invoke(runtime_app, ["status"])
        assert "No runtime is running" in result.output

    def test_bad_url(self, state_dir):
        result = runner.invoke(runtime_app, ["status", "--url", "localhost:9100"])
        assert result.exit_code == 1
        assert "must start with http://" in result.output

    def test_url_unreachable(self, state_dir):
        result = runner.invoke(runtime_app, ["status", "--url", "http://127.0.0.1:9"])
        assert result.exit_code == 1
        assert "Cannot reach" in result.output

    def test_name_with_directory_is_an_error(self, state_dir, workdir):
        result = runner.invoke(runtime_app, ["start", str(workdir), "--name", "x"])
        assert result.exit_code == 1
        assert "--runtime-name" in result.output

    def test_detach_with_dashboard_is_an_error(self, state_dir, workdir):
        result = runner.invoke(
            runtime_app, ["start", str(workdir / "hello.agent"), "--detach", "--dashboard"]
        )
        assert result.exit_code == 1
        assert "can't be combined" in result.output

    def test_start_refuses_a_running_name(self, state_dir, workdir):
        _fake_state("hello")
        result = runner.invoke(runtime_app, ["start", str(workdir / "hello.agent")])
        assert result.exit_code == 1
        assert "already running" in result.output

    def test_start_invalid_runtime_name(self, state_dir, workdir):
        result = runner.invoke(
            runtime_app, ["start", str(workdir / "hello.agent"), "--runtime-name", "a/b"]
        )
        assert result.exit_code == 1
        assert "Invalid runtime name" in result.output

    def test_validate_directory(self, tmp_path, workdir):
        result = runner.invoke(runtime_app, ["validate", str(workdir)])
        assert result.exit_code == 0, result.output
        assert "All 1 manifest(s) valid" in result.output
        (workdir / "bad.agent").write_text("version: '1.0'\nmodel: x\n")
        result = runner.invoke(runtime_app, ["validate", str(workdir)])
        assert result.exit_code == 1
        assert "bad.agent" in result.output

    def test_help_texts(self):
        for command in ("start", "stop", "status", "restart", "logs", "validate", "init"):
            result = runner.invoke(runtime_app, [command, "--help"])
            assert result.exit_code == 0
            assert "Usage" in result.output


class TestStopFallback:
    """A runtime whose API doesn't answer (hung, or not ours any more)."""

    @staticmethod
    def _sleeper() -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        # Reap it as soon as it dies, so its pid really disappears.
        threading.Thread(target=proc.wait, daemon=True).start()
        return proc

    @pytest.mark.skipif(not POSIX, reason="SIGTERM fallback is POSIX only")
    def test_sigterm_fallback(self, state_dir):
        proc = self._sleeper()
        try:
            _fake_state("hung", pid=proc.pid)
            result = runner.invoke(runtime_app, ["stop", "--timeout", "5"])
            assert result.exit_code == 0, result.output
            assert "Cannot reach runtime 'hung'" in result.output
            assert "Sending SIGTERM" in result.output
            assert "Stopped runtime hung" in result.output
            assert not (state_dir / "hung.json").exists()
        finally:
            proc.kill()

    def test_force_kills(self, state_dir, monkeypatch):
        proc = self._sleeper()
        try:
            _fake_state("hung", pid=proc.pid)
            # No graceful way out (as on Windows): only --force helps.
            monkeypatch.setattr("promptise.runtime.cli.terminate_process", lambda pid: False)
            result = runner.invoke(runtime_app, ["stop", "--timeout", "0.5"])
            assert result.exit_code == 1
            assert "did not stop" in result.output and "--force" in result.output
            result = runner.invoke(runtime_app, ["stop", "--timeout", "0.5", "--force"])
            assert result.exit_code == 0, result.output
            assert "Killed runtime hung" in result.output
            assert not (state_dir / "hung.json").exists()
        finally:
            proc.kill()


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


class TestLogs:
    def test_shows_the_newest_entries(self, tmp_path, state_dir):
        journal = tmp_path / "journal"
        journal.mkdir()
        (journal / "worker.jsonl").write_text(
            "\n".join(_journal_line(i) for i in range(30)) + "\nnot json\n"
        )
        result = runner.invoke(
            runtime_app, ["logs", "worker", "-n", "3", "--journal-path", str(journal)]
        )
        assert result.exit_code == 0, result.output
        assert '"n": 29' in result.output and '"n": 27' in result.output
        assert '"n": 26' not in result.output and '"n": 0}' not in result.output

    def test_finds_the_journal_from_the_state_file(self, tmp_path, state_dir, monkeypatch):
        journal = tmp_path / "elsewhere"
        journal.mkdir()
        (journal / "worker.jsonl").write_text(_journal_line(1, "checkpoint") + "\n")
        _fake_state(
            "rt",
            processes={"worker": {"journal": {"backend": "file", "path": str(journal)}}},
        )
        monkeypatch.chdir(tmp_path)  # no .promptise/journal here
        result = runner.invoke(runtime_app, ["logs", "worker"])
        assert result.exit_code == 0, result.output
        assert "checkpoint" in result.output
        assert not (tmp_path / ".promptise").exists()

    def test_journaling_off(self, state_dir):
        _fake_state("rt", processes={"worker": {"journal": None}})
        result = runner.invoke(runtime_app, ["logs", "worker"])
        assert result.exit_code == 1
        assert "journaling off" in result.output

    def test_lines_must_be_positive(self, tmp_path):
        result = runner.invoke(
            runtime_app, ["logs", "w", "-n", "0", "--journal-path", str(tmp_path)]
        )
        assert result.exit_code == 2

    def test_follow_prints_new_entries(self, tmp_path, env):
        journal = tmp_path / "journal"
        journal.mkdir()
        path = journal / "worker.jsonl"
        path.write_text(_journal_line(1) + "\n")
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "promptise",
                "runtime",
                "logs",
                "worker",
                "--follow",
                "--journal-path",
                str(journal),
            ],
            env=env,
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            deadline = time.monotonic() + 30
            seen = ""
            appended = False
            while time.monotonic() < deadline and '"n": 42' not in seen:
                line = proc.stdout.readline()
                if not line:
                    break
                seen += line
                if "Following" in line and not appended:
                    with path.open("a") as fh:
                        fh.write(_journal_line(42, "invocation_result") + "\n")
                    appended = True
            assert '"n": 42' in seen, seen
            assert "invocation_result" in seen
        finally:
            proc.kill()
            proc.communicate(timeout=10)


# ---------------------------------------------------------------------------
# A real runtime in a subprocess
# ---------------------------------------------------------------------------


class TestRealRuntime:
    def test_start_status_restart_stop(self, env, workdir, state_dir, monkeypatch):
        proc = _popen_start(env, workdir, "hello.agent")
        try:
            data = _wait_ready(state_dir, "hello", proc)
            assert data["pid"] == proc.pid
            assert data["processes"]["hello"]["journal"]["path"] == str(
                (workdir / "journal").resolve()
            )
            if POSIX:
                assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
                assert stat.S_IMODE((state_dir / "hello.json").stat().st_mode) == 0o600

            status = _cli("status", env=env, cwd=workdir)
            assert status.returncode == 0, status.stderr
            assert "hello" in status.stdout and "RUNNING" in status.stdout

            as_json = _cli("status", "hello", "--json", env=env, cwd=workdir)
            info = json.loads(as_json.stdout)
            assert info["runtime"]["pid"] == proc.pid
            assert "token" not in info["runtime"]
            assert info["status"]["processes"]["hello"]["state"] == "running"

            detail = _cli("status", "--process", "hello", "--json", env=env, cwd=workdir)
            assert json.loads(detail.stdout)["process"]["name"] == "hello"

            missing = _cli("stop", "--process", "nope", env=env, cwd=workdir)
            assert missing.returncode == 1
            assert "No process named 'nope'" in missing.stderr

            # --url / token: the same API, reached as a remote one.
            remote_env = dict(env, **{_cli_state.TOKEN_ENV: data["token"]})
            remote = _cli(
                "status", "--url", f"http://127.0.0.1:{data['port']}", "--json",
                env=remote_env, cwd=workdir,
            )  # fmt: skip
            assert remote.returncode == 0, remote.stderr
            assert "hello" in json.loads(remote.stdout)["status"]["processes"]
            wrong = _cli(
                "status", "--url", f"http://127.0.0.1:{data['port']}",
                env=dict(env, **{_cli_state.TOKEN_ENV: "wrong"}), cwd=workdir,
            )  # fmt: skip
            assert wrong.returncode == 1
            assert "401" in wrong.stderr

            restarted = _cli("restart", env=env, cwd=workdir)
            assert restarted.returncode == 0, restarted.stderr
            assert "Restarted every process" in restarted.stdout

            logs = _cli("logs", "hello", "-n", "50", env=env, cwd=workdir)
            assert logs.returncode == 0, logs.stderr
            assert "state_transition" in logs.stdout

            stopped = _cli("stop", env=env, cwd=workdir)
            assert stopped.returncode == 0, stopped.stderr
            assert "Stopped runtime hello" in stopped.stdout
            code, out = _finish(proc)
            assert code == 0, out
            assert "Shutting down (stop requested)" in out
            assert not (state_dir / "hello.json").exists()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

        # The journal recorded a clean stop.
        journal = (workdir / "journal" / "hello.jsonl").read_text(encoding="utf-8").splitlines()
        last = json.loads(journal[-1])
        assert last["entry_type"] == "state_transition"
        assert last["data"]["to_state"] == "stopped"

    def test_stop_one_process_keeps_the_runtime(self, env, workdir, state_dir):
        (workdir / "agents").mkdir()
        for name in ("a1", "a2"):
            (workdir / "agents" / f"{name}.agent").write_text(_MANIFEST.format(name=name))
        proc = _popen_start(env, workdir, "agents", "--runtime-name", "pair")
        try:
            _wait_ready(state_dir, "pair", proc)
            # A process name in the only runtime works as the target.
            one = _cli("stop", "a1", env=env, cwd=workdir)
            assert one.returncode == 0, one.stderr
            assert "Stopped process a1" in one.stdout
            info = json.loads(_cli("status", "--json", env=env, cwd=workdir).stdout)
            processes = info[0]["status"]["processes"]
            assert processes["a1"]["state"] == "stopped"
            assert processes["a2"]["state"] == "running"
            back = _cli("restart", "pair", "--process", "a1", env=env, cwd=workdir)
            assert back.returncode == 0, back.stderr
            assert _cli("stop", "pair", env=env, cwd=workdir).returncode == 0
            code, out = _finish(proc)
            assert code == 0, out
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    @pytest.mark.skipif(not POSIX, reason="SIGTERM can't be caught on Windows")
    def test_sigterm_shuts_down_cleanly(self, env, workdir, state_dir):
        proc = _popen_start(env, workdir, "hello.agent")
        try:
            _wait_ready(state_dir, "hello", proc)
            proc.send_signal(signal.SIGTERM)
            code, out = _finish(proc)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        assert code == 0, out
        assert "Shutting down (SIGTERM)" in out
        assert "All processes stopped" in out
        assert not (state_dir / "hello.json").exists()
        last = json.loads(
            (workdir / "journal" / "hello.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        )
        assert last["data"]["to_state"] == "stopped"

    @pytest.mark.skipif(not POSIX, reason="POSIX signals")
    def test_sigint_shuts_down_cleanly(self, env, workdir, state_dir):
        proc = _popen_start(env, workdir, "hello.agent")
        try:
            _wait_ready(state_dir, "hello", proc)
            proc.send_signal(signal.SIGINT)
            code, out = _finish(proc)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        assert code == 0, out
        assert "Shutting down (SIGINT)" in out
        assert "Traceback" not in out
        assert not (state_dir / "hello.json").exists()

    def test_logs_of_a_memory_journal_come_from_the_api(self, env, workdir, state_dir):
        (workdir / "mem.agent").write_text(
            _MANIFEST.format(name="mem").replace("backend: file", "backend: memory")
        )
        proc = _popen_start(env, workdir, "mem.agent")
        try:
            data = _wait_ready(state_dir, "mem", proc)
            assert data["processes"]["mem"]["journal"] == {"backend": "memory", "level": "full"}
            logs = _cli("logs", "mem", env=env, cwd=workdir)
            assert logs.returncode == 0, logs.stderr
            assert "state_transition" in logs.stdout
            assert _cli("stop", env=env, cwd=workdir).returncode == 0
            code, out = _finish(proc)
            assert code == 0, out
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_killed_runtime_leaves_a_stale_file_that_is_cleaned(self, env, workdir, state_dir):
        proc = _popen_start(env, workdir, "hello.agent")
        try:
            _wait_ready(state_dir, "hello", proc)
        finally:
            proc.kill()
            proc.communicate(timeout=30)
        assert (state_dir / "hello.json").exists()
        result = _cli("status", env=env, cwd=workdir)
        assert result.returncode == 1
        assert "No runtime is running" in result.stderr
        assert not (state_dir / "hello.json").exists()

    def test_api_port_in_use(self, env, workdir, state_dir):
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            port = sock.getsockname()[1]
            result = _cli("start", "hello.agent", "--api-port", str(port), env=env, cwd=workdir)
        assert result.returncode == 1
        assert "Cannot start the control API" in result.stderr
        assert not (state_dir / "hello.json").exists()

    def test_detach(self, env, workdir, state_dir):
        started = _cli("start", "hello.agent", "--detach", env=env, cwd=workdir, timeout=120)
        assert started.returncode == 0, started.stdout + started.stderr
        assert "Started runtime hello in the background" in started.stdout
        data = json.loads((state_dir / "hello.json").read_text(encoding="utf-8"))
        pid = data["pid"]
        try:
            assert str(pid) in started.stdout
            assert data["log_file"] == str(state_dir / "hello.log")
            assert (state_dir / "hello.log").exists()
            if POSIX:
                assert stat.S_IMODE((state_dir / "hello.log").stat().st_mode) == 0o600
            again = _cli("start", "hello.agent", "--detach", env=env, cwd=workdir)
            assert again.returncode == 1
            assert "already running" in again.stderr

            stopped = _cli("stop", "hello", env=env, cwd=workdir)
            assert stopped.returncode == 0, stopped.stderr
            assert _cli_state.wait_for_exit(pid, 30)
            assert not (state_dir / "hello.json").exists()
            assert "All processes stopped" in (state_dir / "hello.log").read_text(encoding="utf-8")
        finally:
            if _cli_state.pid_alive(pid):
                _cli_state.kill_process(pid)

    def test_detach_reports_a_failed_start(self, env, workdir, state_dir):
        (workdir / "broken.agent").write_text(_MANIFEST.format(name="broken"))
        env = dict(env, PROMPTISE_TEST_MODEL_FAILS="1")
        result = _cli("start", "broken.agent", "--detach", env=env, cwd=workdir, timeout=120)
        assert result.returncode == 1
        assert "exited during startup" in result.stderr
        assert "failed to start" in result.stderr
        assert "scripted model setup failure" in result.stderr
        assert not (state_dir / "broken.json").exists()
