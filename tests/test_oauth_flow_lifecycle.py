"""SEC-02: срок жизни, одноразовость и изоляция OAuth-flow на HTTP-границе."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
import threading
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import test_oauth_flow_binding as binding


class OAuthLifecycleTests(binding.OAuthBindingTests):
    def test_login_creates_account_and_registry_session(self):
        state = self.start(link=False)
        self.assertEqual(self.callback(state).status_code, 303)
        uid = self.payload()["user_id"]
        self.assertNotIn(uid, (self.a, self.b))
        self.assertEqual(self.links(), [("google", "external-test", uid)])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM auth_sessions WHERE user_id=?", (uid,)).fetchone()[0], 1)
        self.provider.assert_called_once_with("google", "mock-code")

    def test_wrong_provider_rejects_and_consumes_flow(self):
        self.authenticate(self.a)
        state = self.start("google")
        saved = self.payload()
        response = self.callback(state, "discord")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.links(), [])
        self.provider.assert_not_called()
        self.set_payload(saved)
        self.assert_rejected(state)

    def test_flow_succeeds_just_before_ttl(self):
        self.authenticate(self.a)
        start_time = int(binding.auth_routes.time.time())
        with patch.object(binding.auth_routes.time, "time", return_value=start_time) as clock:
            state = self.start()
            clock.return_value = start_time + 599
            response = self.callback(state)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.links(), [("google", "external-test", self.a)])

    def test_flow_expires_at_ttl_boundary(self):
        self.authenticate(self.a)
        start_time = int(binding.auth_routes.time.time())
        with patch.object(binding.auth_routes.time, "time", return_value=start_time) as clock:
            state = self.start()
            clock.return_value = start_time + 600
            self.assert_rejected(state)

    def test_expiry_during_exchange_prevents_mutation(self):
        self.authenticate(self.a)
        start_time = int(binding.auth_routes.time.time())
        identity = self.provider.return_value
        with patch.object(binding.auth_routes.time, "time", return_value=start_time) as clock:
            state = self.start()
            def delayed_exchange(*args):
                clock.return_value = start_time + 600
                return identity
            self.provider.side_effect = delayed_exchange
            response = self.callback(state)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.links(), [])
        self.provider.assert_called_once()

    def test_disabled_provider_terminally_consumes_flow(self):
        self.authenticate(self.a)
        state = self.start()
        original_cookie = self.payload()
        with patch.dict(binding.auth_routes.OAUTH_PROVIDERS, {"google": ("", "")}):
            self.assertEqual(self.callback(state).status_code, 503)
        self.set_payload(original_cookie)
        self.assert_rejected(state)

    def test_successful_callback_replay_rejected(self):
        self.authenticate(self.a)
        state = self.start()
        self.assertEqual(self.callback(state).status_code, 303)
        self.provider.reset_mock()
        self.assert_rejected(state)

    def test_restored_cookie_cannot_replay_completed_flow(self):
        self.authenticate(self.a)
        state = self.start()
        saved = self.payload()
        self.assertEqual(self.callback(state).status_code, 303)
        self.set_payload(saved)
        self.provider.reset_mock()
        self.assert_rejected(state)

    def test_cancellation_consumes_flow_even_with_restored_cookie(self):
        self.authenticate(self.a)
        state = self.start()
        saved = self.payload()
        response = self.callback(state, error="access_denied")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.links(), [])
        self.provider.assert_not_called()
        self.set_payload(saved)
        self.assert_rejected(state)

    def test_missing_code_consumes_flow_even_with_restored_cookie(self):
        self.authenticate(self.a)
        state = self.start()
        saved = self.payload()
        response = self.client.get("/auth/google/callback", params={"state": state})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.links(), [])
        self.provider.assert_not_called()
        self.set_payload(saved)
        self.assert_rejected(state)

    def test_invalid_code_consumes_flow_even_with_restored_cookie(self):
        self.authenticate(self.a)
        state = self.start()
        saved = self.payload()
        self.provider.side_effect = HTTPException(502, "Тестовый код отклонён")
        self.assertEqual(self.callback(state).status_code, 502)
        self.assertEqual(self.links(), [])
        self.provider.assert_called_once()
        self.provider.side_effect = None
        self.provider.reset_mock()
        self.set_payload(saved)
        self.assert_rejected(state)

    def _change_cookie_mode(self, state, mode):
        saved = self.payload()
        # Старый формат нужен, чтобы этот же HTTP-regression был red на baseline.
        if "oauth_flows" in saved:
            key = hashlib.sha256(state.encode("ascii")).hexdigest()
            saved["oauth_flows"][key]["mode"] = mode
        else:
            saved["oauth_link"] = mode == "link"
        self.set_payload(saved)

    def test_login_cannot_be_converted_to_link_by_cookie_payload(self):
        self.authenticate(self.a)
        state = self.start(link=False)
        self._change_cookie_mode(state, "link")
        self.assert_rejected(state)

    def test_link_cannot_be_converted_to_login_by_cookie_payload(self):
        self.authenticate(self.a)
        state = self.start()
        self._change_cookie_mode(state, "login")
        self.assert_rejected(state)
        self.assertEqual(self.payload()["user_id"], self.a)

    def test_anonymous_login_rejects_changed_session(self):
        state = self.start(link=False)
        self.authenticate(self.a)
        self.assert_rejected(state)
        self.assertEqual(self.payload()["user_id"], self.a)

    def test_independent_login_clients_keep_separate_sessions(self):
        other = self.new_client()
        first_state = self.start(link=False)
        second_state = self.start(link=False, client=other)
        self.assertNotEqual(first_state, second_state)
        self.assertEqual(self.callback(first_state).status_code, 303)
        self.assertEqual(self.callback(second_state, client=other).status_code, 303)
        first, second = self.payload(), self.payload(other)
        self.assertEqual(first["user_id"], second["user_id"])
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertEqual(self.provider.call_count, 2)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM auth_sessions WHERE user_id=?", (first["user_id"],)
        ).fetchone()[0], 2)

    def test_parallel_link_flows_in_same_session_are_independent(self):
        self.authenticate(self.a)
        google = self.start("google")
        discord = self.start("discord")
        self.assertEqual(self.callback(google, "google").status_code, 303)
        self.assertEqual(self.callback(discord, "discord").status_code, 303)
        self.assertEqual(self.links(), [("discord", "external-test", self.a),
                                       ("google", "external-test", self.a)])
        self.assertEqual(self.provider.call_count, 2)

    def test_same_provider_link_flows_in_same_session_are_independent(self):
        self.authenticate(self.a)
        first, second = self.start(), self.start()
        self.assertNotEqual(first, second)
        self.assertEqual(self.callback(first).status_code, 303)
        self.assertEqual(self.callback(second).status_code, 303)
        self.assertEqual(self.links(), [("google", "external-test", self.a)])
        self.assertEqual(self.provider.call_count, 2)

    def test_callback_without_browser_proof_does_not_consume_owner_flow(self):
        self.authenticate(self.a)
        state = self.start()
        stranger = self.new_client()
        self.assert_rejected(state, stranger)
        self.assertEqual(self.callback(state).status_code, 303)
        self.assertEqual(self.links(), [("google", "external-test", self.a)])

    def test_concurrent_callbacks_exchange_and_mutate_once(self):
        self.authenticate(self.a)
        state = self.start()
        original_cookie = self.payload()
        clients = [self.new_client(), self.new_client()]
        for client in clients:
            self.set_payload(original_cookie, client)
        start = threading.Barrier(3)
        entered_exchange, release_exchange = threading.Event(), threading.Event()
        identity = self.provider.return_value
        def exchange(*args):
            entered_exchange.set()
            if not release_exchange.wait(5):
                raise AssertionError("Тест не освободил mock обмена code")
            return identity
        def callback(client):
            start.wait(timeout=5)
            return self.callback(state, client=client)
        self.provider.side_effect = exchange
        with patch.object(binding.users, "link_oauth_account",
                          wraps=binding.users.link_oauth_account) as mutation:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(callback, client) for client in clients]
                try:
                    start.wait(timeout=5)
                    self.assertTrue(entered_exchange.wait(5))
                    # При атомарном consume второй запрос уже завершится с 403,
                    # пока первый ещё ждёт внешний обмен.
                    wait(futures, timeout=1, return_when=FIRST_COMPLETED)
                finally:
                    release_exchange.set()
                responses = [future.result(timeout=5) for future in futures]
            self.assertEqual(sorted(response.status_code for response in responses), [303, 403])
            mutation.assert_called_once()
        self.provider.assert_called_once()
        self.assertEqual(self.links(), [("google", "external-test", self.a)])

    def test_login_return_target_is_consumed(self):
        self.set_payload({"login_next": "/admin/profile"})
        state = self.start(link=False)
        response = self.callback(state)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/admin/profile")
        self.assertNotIn("login_next", self.payload())

    def test_explicit_login_target_does_not_leave_stale_return_cookie(self):
        self.set_payload({"login_next": "/admin/profile"})
        response = self.client.get("/auth/google", params={"next": "/admin/profile"})
        self.assertEqual(response.status_code, 303)
        state = binding.parse_qs(binding.urlsplit(response.headers["location"]).query)["state"][0]
        response = self.callback(state)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/admin/profile")
        self.assertNotIn("login_next", self.payload())


def load_tests(loader, tests, pattern):
    # Переиспользуем fixture SEC-01, не запускаем унаследованные тесты повторно.
    return unittest.TestSuite(OAuthLifecycleTests(name) for name in sorted(
        name for name in OAuthLifecycleTests.__dict__ if name.startswith("test_")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
