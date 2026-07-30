"""Regression tests for browser session cleanup and screenshot recovery."""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestScreenshotPathRecovery:
    def test_extracts_standard_absolute_path(self):
        from tools.browser_tool import _extract_screenshot_path_from_text

        assert (
            _extract_screenshot_path_from_text("Screenshot saved to /tmp/foo.png")
            == "/tmp/foo.png"
        )

    def test_extracts_quoted_absolute_path(self):
        from tools.browser_tool import _extract_screenshot_path_from_text

        assert (
            _extract_screenshot_path_from_text(
                "Screenshot saved to '/Users/david/.hermes/browser_screenshots/shot.png'"
            )
            == "/Users/david/.hermes/browser_screenshots/shot.png"
        )


class TestBrowserCleanup:
    def setup_method(self):
        from tools import browser_tool

        self.browser_tool = browser_tool
        self.orig_active_sessions = browser_tool._active_sessions.copy()
        self.orig_session_last_activity = browser_tool._session_last_activity.copy()
        self.orig_recording_sessions = browser_tool._recording_sessions.copy()
        self.orig_cleanup_done = browser_tool._cleanup_done

    def teardown_method(self):
        self.browser_tool._active_sessions.clear()
        self.browser_tool._active_sessions.update(self.orig_active_sessions)
        self.browser_tool._session_last_activity.clear()
        self.browser_tool._session_last_activity.update(self.orig_session_last_activity)
        self.browser_tool._recording_sessions.clear()
        self.browser_tool._recording_sessions.update(self.orig_recording_sessions)
        self.browser_tool._cleanup_done = self.orig_cleanup_done

    def test_cleanup_browser_clears_tracking_state(self):
        browser_tool = self.browser_tool
        browser_tool._active_sessions["task-1"] = {
            "session_name": "sess-1",
            "bb_session_id": None,
        }
        browser_tool._session_last_activity["task-1"] = 123.0

        with (
            patch("tools.browser_tool._maybe_stop_recording") as mock_stop,
            patch(
                "tools.browser_tool._run_browser_command",
                return_value={"success": True},
            ) as mock_run,
            patch("tools.browser_tool.os.path.exists", return_value=False),
        ):
            browser_tool.cleanup_browser("task-1")

        assert "task-1" not in browser_tool._active_sessions
        assert "task-1" not in browser_tool._session_last_activity
        mock_stop.assert_called_once_with("task-1")
        mock_run.assert_called_once_with("task-1", "close", [], timeout=10)

    def test_cleanup_camofox_managed_persistence_skips_close(self):
        """When camofox mode + managed persistence, soft_cleanup fires instead of close."""
        browser_tool = self.browser_tool
        browser_tool._active_sessions["task-1"] = {
            "session_name": "sess-1",
            "bb_session_id": None,
        }
        browser_tool._session_last_activity["task-1"] = 123.0

        with (
            patch("tools.browser_tool._is_camofox_mode", return_value=True),
            patch("tools.browser_tool._maybe_stop_recording") as mock_stop,
            patch(
                "tools.browser_tool._run_browser_command",
                return_value={"success": True},
            ),
            patch("tools.browser_tool.os.path.exists", return_value=False),
            patch(
                "tools.browser_camofox.camofox_soft_cleanup",
                return_value=True,
            ) as mock_soft,
            patch("tools.browser_camofox.camofox_close") as mock_close,
        ):
            browser_tool.cleanup_browser("task-1")

        mock_soft.assert_called_once_with("task-1")
        mock_close.assert_not_called()

    def test_cleanup_camofox_no_persistence_calls_close(self):
        """When camofox mode but managed persistence is off, camofox_close fires."""
        browser_tool = self.browser_tool
        browser_tool._active_sessions["task-1"] = {
            "session_name": "sess-1",
            "bb_session_id": None,
        }
        browser_tool._session_last_activity["task-1"] = 123.0

        with (
            patch("tools.browser_tool._is_camofox_mode", return_value=True),
            patch("tools.browser_tool._maybe_stop_recording") as mock_stop,
            patch(
                "tools.browser_tool._run_browser_command",
                return_value={"success": True},
            ),
            patch("tools.browser_tool.os.path.exists", return_value=False),
            patch(
                "tools.browser_camofox.camofox_soft_cleanup",
                return_value=False,
            ) as mock_soft,
            patch("tools.browser_camofox.camofox_close") as mock_close,
        ):
            browser_tool.cleanup_browser("task-1")

        mock_soft.assert_called_once_with("task-1")
        mock_close.assert_called_once_with("task-1")

    def test_emergency_cleanup_clears_all_tracking_state(self):
        browser_tool = self.browser_tool
        browser_tool._cleanup_done = False
        browser_tool._active_sessions["task-1"] = {"session_name": "sess-1"}
        browser_tool._active_sessions["task-2"] = {"session_name": "sess-2"}
        browser_tool._session_last_activity["task-1"] = 1.0
        browser_tool._session_last_activity["task-2"] = 2.0
        browser_tool._recording_sessions.update({"task-1", "task-2"})

        with patch("tools.browser_tool.cleanup_all_browsers") as mock_cleanup_all:
            browser_tool._emergency_cleanup_all_sessions()

        mock_cleanup_all.assert_called_once_with()
        assert browser_tool._active_sessions == {}
        assert browser_tool._session_last_activity == {}
        assert browser_tool._recording_sessions == set()
        assert browser_tool._cleanup_done is True


def _fake_chrome_proc(pid, cmdline, age_s, cpu_time_s, children=None):
    """Build a MagicMock standing in for a psutil.Process for the sweep test."""
    proc = MagicMock()
    proc.pid = pid
    proc.info = {"cmdline": cmdline, "create_time": time.time() - age_s}
    proc.cpu_times.return_value = SimpleNamespace(user=cpu_time_s, system=0.0)
    proc.children.return_value = children or []
    return proc


class TestStaleChromeSweep:
    """Covers the daemon-died-before-closing-Chrome orphan case.

    ``_reap_orphaned_browser_sessions`` can only reap what hermes tracked a
    PID for (the agent-browser daemon). If that daemon itself is already
    dead, hermes never learns the Chrome PID it spawned, so that Chrome
    process survives forever unless something else independently notices
    it. ``_reap_stale_agent_browser_chrome_processes`` is that something
    else: it looks at OS-level process state directly instead of relying on
    hermes's own bookkeeping.
    """

    def test_reaps_old_idle_chrome_with_no_owning_daemon(self):
        from tools import browser_tool

        stale = _fake_chrome_proc(
            pid=4242,
            cmdline=[
                "/snap/chromium/x/chrome",
                "--user-data-dir=/tmp/agent-browser-chrome-abc123",
                "--headless=new",
            ],
            age_s=browser_tool.STALE_CHROME_AGE_S + 60,
            cpu_time_s=1.0,  # essentially never did any work
        )

        with patch("psutil.process_iter", return_value=[stale]):
            browser_tool._reap_stale_agent_browser_chrome_processes()

        stale.kill.assert_called_once_with()

    def test_leaves_recently_started_chrome_alone(self):
        """A brand-new session shouldn't be reaped just because it exists."""
        from tools import browser_tool

        fresh = _fake_chrome_proc(
            pid=4243,
            cmdline=[
                "/snap/chromium/x/chrome",
                "--user-data-dir=/tmp/agent-browser-chrome-fresh",
            ],
            age_s=60,  # 1 minute old
            cpu_time_s=0.5,
        )

        with patch("psutil.process_iter", return_value=[fresh]):
            browser_tool._reap_stale_agent_browser_chrome_processes()

        fresh.kill.assert_not_called()

    def test_leaves_old_but_actively_used_chrome_alone(self):
        """Old + real CPU time means a still-in-use session, not an orphan."""
        from tools import browser_tool

        busy = _fake_chrome_proc(
            pid=4244,
            cmdline=[
                "/snap/chromium/x/chrome",
                "--user-data-dir=/tmp/agent-browser-chrome-busy",
            ],
            age_s=browser_tool.STALE_CHROME_AGE_S + 60,
            cpu_time_s=browser_tool.STALE_CHROME_CPU_S + 30,
        )

        with patch("psutil.process_iter", return_value=[busy]):
            browser_tool._reap_stale_agent_browser_chrome_processes()

        busy.kill.assert_not_called()

    def test_ignores_chrome_child_processes(self):
        """Renderer/gpu/utility children match the same --user-data-dir but
        must be skipped directly — killing the top-level process (which the
        stale branch already does via proc.children()) is what tears them
        down, not a second, separate match against the same profile dir."""
        from tools import browser_tool

        renderer_child = _fake_chrome_proc(
            pid=4245,
            cmdline=[
                "/snap/chromium/x/chrome",
                "--type=renderer",
                "--user-data-dir=/tmp/agent-browser-chrome-abc123",
            ],
            age_s=browser_tool.STALE_CHROME_AGE_S + 60,
            cpu_time_s=1.0,
        )

        with patch("psutil.process_iter", return_value=[renderer_child]):
            browser_tool._reap_stale_agent_browser_chrome_processes()

        renderer_child.kill.assert_not_called()

    def test_ignores_unrelated_chrome_processes(self):
        """Chrome instances outside the agent-browser naming convention
        (e.g. a developer's own debugging browser) must never be touched."""
        from tools import browser_tool

        unrelated = _fake_chrome_proc(
            pid=4246,
            cmdline=[
                "/snap/chromium/x/chrome",
                "--user-data-dir=/home/ubuntu/.chrome-profile",
            ],
            age_s=browser_tool.STALE_CHROME_AGE_S + 60,
            cpu_time_s=1.0,
        )

        with patch("psutil.process_iter", return_value=[unrelated]):
            browser_tool._reap_stale_agent_browser_chrome_processes()

        unrelated.kill.assert_not_called()
