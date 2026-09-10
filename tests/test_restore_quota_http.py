"""FLOW-06: реальный restore → session/CSRF → атомарная квота SQLite."""

from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

import test_bulk_http as bulk


class RestoreQuotaHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bulk.db.init_db()

    def setUp(self):
        self.flow = bulk.BulkHttpTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.conn = self.flow.conn
        self.limit(1)

    def limit(self, value):
        self.conn.execute("UPDATE users SET date_limit=? WHERE id=?", (value, self.flow.owner_id))
        self.conn.commit()

    def event(self, *, archived=True, origin="admin", source=None, owner=None):
        did = self.flow.date("restore" if archived else "archive", owner=owner)
        self.conn.execute("UPDATE dates SET origin=?,source_date_id=? WHERE id=?", (origin, source, did))
        self.conn.commit()
        return did

    def single(self, did):
        return self.flow.client.post(
            f"/admin/dates/{did}/archive",
            data={"csrf": self.flow.csrf, "next": "/admin/dates?view=archived"},
            headers={"Referer": "http://testserver/admin/dates?view=archived"},
        )

    def snapshot(self):
        return [tuple(row) for row in self.conn.execute("SELECT * FROM dates ORDER BY id")]

    def assert_quota_error(self, response):
        message = self.flow.message(response)
        self.assertIn("лимит", message.lower())
        page = self.flow.client.get(response.headers["location"])
        self.assertEqual(page.status_code, 200)
        self.assertIn(message, page.text)

    def test_single_restore_at_limit_leaves_archived_event_unchanged(self):
        self.event(archived=False)
        archived = self.event()
        before = self.snapshot()
        self.assert_quota_error(self.single(archived))
        self.assertEqual(self.snapshot(), before)

    def test_bulk_restore_exceeding_remaining_slot_is_atomic(self):
        first, second = self.event(), self.event()
        before = self.snapshot()
        self.assert_quota_error(self.flow.post("restore", [first, second]))
        self.assertEqual(self.snapshot(), before)

    def test_single_and_bulk_restore_below_quota_then_noop_at_limit(self):
        self.limit(3)
        first, second, third = self.event(), self.event(), self.event()
        self.assertIn("Возвращено", self.flow.message(self.single(first)))
        self.assertIn(": 2 события", self.flow.message(self.flow.post("restore", [second, third, second])))
        for did in (first, second, third):
            self.assertIsNone(self.flow.state(did)["archived_at"])
        before = self.snapshot()
        self.assertIn(": 0 событий", self.flow.message(self.flow.post("restore", [first, second, third])))
        self.assertEqual(self.snapshot(), before)

    def test_bulk_restore_at_limit_rejects_without_changing_any_row(self):
        self.event(archived=False)
        first, second = self.event(), self.event()
        before = self.snapshot()
        self.assert_quota_error(self.flow.post("restore", [first, second]))
        self.assertEqual(self.snapshot(), before)

    def test_exempt_proposals_and_copies_restore_even_above_limit(self):
        source = self.event(archived=False)
        self.limit(0)
        for origin, source_id in (("guest", None), ("copy", None), ("admin", source)):
            with self.subTest(origin=origin, source=source_id):
                first = self.event(origin=origin, source=source_id)
                other_source = (self.event(owner=self.flow.foreign_id) if source_id else None)
                second = self.event(origin=origin, source=other_source)
                self.assertIn("Возвращено", self.flow.message(self.single(first)))
                self.assertIn(": 1 событие", self.flow.message(self.flow.post("restore", [second, source])))
                self.assertIsNone(self.flow.state(first)["archived_at"])
                self.assertIsNone(self.flow.state(second)["archived_at"])

    def test_mixed_exempt_and_counted_bulk_rejection_rolls_back_all(self):
        self.event(archived=False)
        exempt, counted = self.event(origin="guest"), self.event()
        before = self.snapshot()
        self.assert_quota_error(self.flow.post("restore", [exempt, counted]))
        self.assertEqual(self.snapshot(), before)

    def test_single_restore_by_operator_uses_resource_owner_quota(self):
        self.conn.execute("UPDATE users SET is_operator=1,date_limit=100 WHERE id=?", (self.flow.owner_id,))
        self.conn.execute("UPDATE users SET date_limit=0 WHERE id=?", (self.flow.foreign_id,))
        self.conn.commit()
        archived = self.event(owner=self.flow.foreign_id)
        before = self.snapshot()
        self.assert_quota_error(self.single(archived))
        self.assertEqual(self.snapshot(), before)

    def test_single_restore_preserves_ownership_and_csrf(self):
        own, foreign = self.event(), self.event(owner=self.flow.foreign_id)
        before = self.snapshot()
        self.assertIn("не найдено", self.flow.message(self.single(foreign)).lower())
        denied = self.flow.client.post(f"/admin/dates/{own}/archive", data={"csrf": "invalid"})
        self.assertEqual(denied.status_code, 303)
        self.assertEqual(self.snapshot(), before)

    def test_helper_cannot_bypass_quota(self):
        import admin_routes
        self.event(archived=False)
        archived = self.event()
        before = self.snapshot()
        row = self.conn.execute("SELECT * FROM dates WHERE id=?", (archived,)).fetchone()
        with self.assertRaises(admin_routes.DateQuotaExceeded):
            admin_routes._bulk_set_archived(self.conn, row, archived=False)
        self.conn.rollback()
        self.assertEqual(self.snapshot(), before)

    def test_create_clone_and_shared_copy_keep_product_quota_rules(self):
        created = self.flow.client.post(
            "/admin/dates/new", data={"name": "Личное событие", "csrf": self.flow.csrf},
            headers={"X-Requested-With": "fetch"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        own = self.conn.execute("SELECT id FROM dates WHERE owner_id=?", (self.flow.owner_id,)).fetchone()[0]
        before = self.snapshot()
        denied = self.flow.client.post(
            "/admin/dates/new", data={"name": "Лишнее", "csrf": self.flow.csrf},
            headers={"X-Requested-With": "fetch"},
        )
        self.assertEqual(denied.status_code, 400, denied.text)
        self.assertIn("лимит", denied.json()["detail"])
        self.assert_quota_error(self.flow.client.post(
            f"/admin/dates/{own}/clone", data={"csrf": self.flow.csrf},
        ))
        self.assertEqual(self.snapshot(), before)
        self.assertIn("Перенесено", self.flow.message(self.single(own)))
        cloned = self.flow.client.post(f"/admin/dates/{own}/clone", data={"csrf": self.flow.csrf})
        self.assertEqual(cloned.status_code, 303)
        clones = self.conn.execute(
            "SELECT * FROM dates WHERE owner_id=? AND archived_at IS NULL", (self.flow.owner_id,),
        ).fetchall()
        self.assertEqual(len(clones), 1)
        self.assertEqual(clones[0]["origin"], "admin")
        self.assertIsNone(clones[0]["source_date_id"])
        # Настоящее «Добавить себе» из чужой share-ссылки остаётся исключением.
        source = self.event(archived=False, owner=self.flow.foreign_id)
        token = self.flow.state(source)["share_token"]
        copied = self.flow.client.post(f"/d/{token}/add", data={"csrf": self.flow.csrf})
        self.assertEqual(copied.status_code, 303, copied.text)
        copy = self.conn.execute(
            "SELECT * FROM dates WHERE owner_id=? AND source_date_id=?", (self.flow.owner_id, source),
        ).fetchone()
        self.assertIsNotNone(copy)
        self.assertIsNone(copy["archived_at"])

    def test_competing_restore_create_and_clone_cannot_overfill_one_slot(self):
        for competing in ("restore", "create", "clone"):
            with self.subTest(competing=competing):
                self.conn.execute("DELETE FROM dates")
                self.conn.commit()
                bulk.ratelimit._rates.clear()
                first, second = self.event(), self.event()
                barrier = threading.Barrier(2)
                cookies = dict(self.flow.client.cookies)

                def submit(kind, did):
                    client = bulk.TestClient(bulk.main.app, follow_redirects=False,
                                             raise_server_exceptions=False, cookies=cookies)
                    try:
                        data = {"csrf": self.flow.csrf}
                        if kind == "restore":
                            path = "/admin/dates/bulk"
                            data.update(action="restore", date_ids=[did])
                        elif kind == "create":
                            path = "/admin/dates/new"
                            data["name"] = "Конкурирующее событие"
                        else:
                            path = f"/admin/dates/{did}/clone"
                        barrier.wait(timeout=5)
                        return client.post(path, data=data, headers={"X-Requested-With": "fetch"})
                    finally:
                        client.close()

                with ThreadPoolExecutor(max_workers=2) as pool:
                    a = pool.submit(submit, "restore", first)
                    b = pool.submit(submit, competing, second)
                    responses = [a.result(timeout=20), b.result(timeout=20)]
                self.assertTrue(all(r.status_code in (200, 303, 400) for r in responses),
                                [(r.status_code, r.text[:100]) for r in responses])
                active = self.conn.execute(
                    "SELECT id FROM dates WHERE owner_id=? AND archived_at IS NULL "
                    "AND origin='admin' AND source_date_id IS NULL", (self.flow.owner_id,),
                ).fetchall()
                self.assertEqual(len(active), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
