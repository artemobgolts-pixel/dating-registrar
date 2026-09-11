"""DEPLOY-05: стабильные группы 500 и correlation IDs, только локальная доставка."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from starlette.requests import Request

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_DATA = tempfile.TemporaryDirectory(prefix="date4you-alert-grouping-")
os.environ.update({
    "DATA_DIR": _DATA.name, "SECRET_KEY": "synthetic-alert-grouping",
    "COOKIE_SECURE": "false", "DOMAIN": "testserver", "LOG_LEVEL": "CRITICAL",
    "TG_BOT_TOKEN": "", "TG_CHAT_ID": "", "TG_BACKUP_CHAT_ID": "", "SENTRY_DSN": "",
})

import main
import notify


class AlertGroupingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        notify._alert_seen.clear()
        self.addCleanup(notify._alert_seen.clear)
        for name, value in (("TOKEN", "synthetic-token"), ("CHAT", "synthetic-chat")):
            patcher = mock.patch.object(notify, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(notify, "notify")
        self.deliver = patcher.start()
        self.addCleanup(patcher.stop)
        # Даже ошибка тестовой заглушки не должна выпускать настоящий HTTP.
        patcher = mock.patch.object(notify.httpx, "post", side_effect=AssertionError("network forbidden"))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def error(self, request_id, *, error_type=ValueError,
                    path="/c/private-token-1", route="/c/{token}", method="GET"):
        scope = {"type": "http", "method": method, "path": path,
                 "scheme": "http", "server": ("testserver", 80),
                 "query_string": b"", "headers": [],
                 "state": {"request_id": request_id}}
        if route is not None:
            scope["route"] = SimpleNamespace(path=route)
        tasks = []
        real_create_task = asyncio.create_task

        def capture(coroutine):
            task = real_create_task(coroutine)
            tasks.append(task)
            return task

        with mock.patch.object(main.asyncio, "create_task", side_effect=capture):
            response = await main.unhandled_error(Request(scope), error_type("private error text"))
        await asyncio.gather(*tasks)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers["X-Request-ID"], request_id)
        return response

    async def test_repeated_route_error_groups_and_keeps_sample_correlation(self):
        with mock.patch.object(main.log, "exception") as logs:
            await self.error("req-first")
            await self.error("req-second", path="/c/private-token-2")
        self.deliver.assert_called_once()
        message = self.deliver.call_args.args[0]
        self.assertIn("request_id=req-first", message)
        self.assertIn("/c/{token}", message)
        self.assertNotIn("private-token", message)
        self.assertNotIn("private error text", message)
        self.assertEqual([call.kwargs["extra"]["request_id"] for call in logs.call_args_list],
                         ["req-first", "req-second"])
        self.assertEqual(list(notify._alert_seen), [("GET", "/c/{token}", "builtins.ValueError")])

    async def test_distinct_types_routes_and_methods_remain_visible(self):
        await self.error("req-value")
        await self.error("req-runtime", error_type=RuntimeError)
        await self.error("req-route", route="/d/{token}")
        await self.error("req-method", method="POST")
        self.assertEqual(self.deliver.call_count, 4)
        self.assertEqual(len(notify._alert_seen), 4)

    async def test_unmatched_routes_and_unknown_methods_have_bounded_group(self):
        await self.error("req-first", route=None, path="/private-one", method="ARBITRARY1")
        await self.error("req-second", route=None, path="/private-two", method="ARBITRARY2")
        self.deliver.assert_called_once()
        self.assertEqual(list(notify._alert_seen), [("OTHER", "unmatched", "builtins.ValueError")])
        self.assertNotIn("private-one", self.deliver.call_args.args[0])

    async def test_same_group_can_alert_again_after_window(self):
        with mock.patch.object(notify.time, "monotonic", return_value=10):
            await self.error("req-first")
        with mock.patch.object(notify.time, "monotonic", return_value=10 + notify._ALERT_WINDOW):
            await self.error("req-after-window")
        self.assertEqual(self.deliver.call_count, 2)
        self.assertIn("req-after-window", self.deliver.call_args.args[0])

    async def test_concurrent_alerts_reserve_one_group(self):
        barrier = threading.Barrier(12)

        def issue(index):
            barrier.wait(timeout=5)
            notify.alert(f"request_id=req-{index}",
                         group_key=("GET", "/c/{token}", "builtins.ValueError"))

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(issue, range(12)))
        self.deliver.assert_called_once()
        self.assertIn("request_id=req-", self.deliver.call_args.args[0])

    async def test_legacy_alert_text_dedup_remains_compatible(self):
        notify.alert("сбой X")
        notify.alert("сбой X")
        notify.alert("сбой Y")
        self.assertEqual(self.deliver.call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
