"""FUN-03: ID проверяются до SQLite, ошибочный запрос не меняет данные."""

import html
import unittest

import test_bulk_http as fixture


MAX_ID = str((1 << 63) - 1)
INVALID_IDS = (str(1 << 63), "9" * 200, "9" * 5000, "-1", "0", "not-a-number")
BOUNDARIES = (MAX_ID, *INVALID_IDS)


class IdBoundaryTests(fixture.BulkHttpTests):
    def set_operator(self, enabled):
        self.conn.execute("UPDATE users SET is_operator=? WHERE id=?", (int(enabled), self.owner_id))
        self.conn.commit()

    def category(self, *, owner=None):
        cid = self.conn.execute(
            "INSERT INTO categories(owner_id,name,link_token,created_at) "
            "VALUES(?,'Проверка ID',lower(hex(randomblob(16))),?)",
            (owner or self.owner_id, fixture.STAMP),
        ).lastrowid
        self.conn.commit()
        return cid

    def post_data(self, **kwargs):
        return {"csrf": self.csrf, **kwargs}

    def assert_rejected_without_mutation(self, method, url, **kwargs):
        fixture.ratelimit._rates.clear()
        before = list(self.conn.iterdump())
        response = self.client.request(method, url, **kwargs)
        self.assertEqual(list(self.conn.iterdump()), before, (method, url))
        if response.status_code == 303:
            message = self.message(response)
            self.assertIn("⚠", message, (url, message))
            page = self.client.get(response.headers["location"])
            self.assertEqual(page.status_code, 200, page.text)
            self.assertIn(message, html.unescape(page.text))
        else:
            self.assertGreaterEqual(response.status_code, 400, response.text)
            self.assertLess(response.status_code, 500, response.text)
        return response

    def test_event_editor_get_rejects_boundary_ids_for_owner_and_operator(self):
        for operator in (0, 1):
            self.set_operator(operator)
            for raw_id in BOUNDARIES:
                with self.subTest(operator=operator, raw_id=raw_id):
                    self.assert_rejected_without_mutation("GET", f"/admin/dates/{raw_id}/edit")

    def test_event_editor_post_rejects_raw_path_ids_before_upload(self):
        for operator in (0, 1):
            self.set_operator(operator)
            for raw_id in BOUNDARIES:
                for multipart in (False, True):
                    with self.subTest(operator=operator, raw_id=raw_id, multipart=multipart):
                        # Multipart проходит через UploadRoute, где path_params
                        # ещё строки, до преобразования параметров FastAPI.
                        kwargs = ({"files": [("csrf", (None, self.csrf)),
                                             ("name", (None, "Не должно сохраниться"))]}
                                  if multipart else {"data": self.post_data(name="Не должно сохраниться")})
                        self.assert_rejected_without_mutation(
                            "POST", f"/admin/dates/{raw_id}/edit", **kwargs,
                            headers={"Referer": "http://testserver/admin/dates"},
                        )

    def test_bulk_all_actions_reject_mixed_valid_and_boundary_ids_atomically(self):
        for action in fixture.ACTIONS:
            for raw_id in BOUNDARIES:
                with self.subTest(action=action, raw_id=raw_id):
                    owned = self.date(action)
                    self.assert_rejected_without_mutation(
                        "POST", "/admin/dates/bulk",
                        data=self.post_data(action=action, date_ids=[str(owned), raw_id]),
                        headers={"Referer": "http://testserver/admin/dates"},
                    )

    def test_collection_detail_rejects_boundary_ids_for_owner_and_operator(self):
        for operator in (0, 1):
            self.set_operator(operator)
            for raw_id in BOUNDARIES:
                with self.subTest(operator=operator, raw_id=raw_id):
                    self.assert_rejected_without_mutation("GET", f"/admin/categories/{raw_id}")

    def test_collection_rename_rejects_raw_path_ids_before_upload(self):
        for operator in (0, 1):
            self.set_operator(operator)
            for raw_id in BOUNDARIES:
                with self.subTest(operator=operator, raw_id=raw_id):
                    self.assert_rejected_without_mutation(
                        "POST", f"/admin/categories/{raw_id}/rename",
                        files=[("csrf", (None, self.csrf)), ("name", (None, "Не сохранять"))],
                        headers={"Referer": "http://testserver/admin/categories"},
                    )

    def test_collection_attach_and_detach_reject_boundary_event_ids(self):
        for operator in (0, 1):
            self.set_operator(operator)
            # У оператора проверяем чужую подборку: owner helper обязан
            # использовать её владельца и ту же проверку диапазона ID.
            cid = self.category(owner=self.foreign_id if operator else self.owner_id)
            for action in ("attach", "detach"):
                for raw_id in BOUNDARIES:
                    with self.subTest(operator=operator, action=action, raw_id=raw_id):
                        self.assert_rejected_without_mutation(
                            "POST", f"/admin/categories/{cid}/{action}",
                            data=self.post_data(date_id=raw_id),
                            headers={"Referer": "http://testserver/admin/categories"},
                        )

    def test_collection_attach_and_detach_reject_boundary_category_ids(self):
        did = self.date("archive")
        for operator in (0, 1):
            self.set_operator(operator)
            for action in ("attach", "detach"):
                for raw_id in BOUNDARIES:
                    with self.subTest(operator=operator, action=action, raw_id=raw_id):
                        self.assert_rejected_without_mutation(
                            "POST", f"/admin/categories/{raw_id}/{action}",
                            data=self.post_data(date_id=str(did)),
                            headers={"Referer": "http://testserver/admin/categories"},
                        )

    def test_event_actions_reject_boundary_ids(self):
        for operator in (0, 1):
            self.set_operator(operator)
            for action in ("publish", "archive", "visibility", "delete", "clone"):
                for raw_id in BOUNDARIES:
                    with self.subTest(operator=operator, action=action, raw_id=raw_id):
                        self.assert_rejected_without_mutation(
                            "POST", f"/admin/dates/{raw_id}/{action}",
                            data=self.post_data(), headers={"Referer": "http://testserver/admin/dates"},
                        )

    def test_event_editor_invalid_category_ids_cannot_change_existing_event(self):
        did = self.date("archive")
        cid = self.category()
        self.conn.execute("INSERT INTO date_categories(date_id,category_id) VALUES(?,?)", (did, cid))
        self.conn.commit()
        for raw_id in INVALID_IDS:
            with self.subTest(raw_id=raw_id):
                self.assert_rejected_without_mutation(
                    "POST", f"/admin/dates/{did}/edit",
                    data=self.post_data(name="Не должно сохраниться", categories=[str(cid), raw_id]),
                    headers={"Referer": "http://testserver/admin/dates"},
                )

    def test_event_create_invalid_category_ids_cannot_publish_partial_event(self):
        cid = self.category()
        for raw_id in INVALID_IDS:
            with self.subTest(raw_id=raw_id):
                self.assert_rejected_without_mutation(
                    "POST", "/admin/dates/new",
                    data=self.post_data(name="Не создавать", categories=[str(cid), raw_id]),
                    headers={"Referer": "http://testserver/admin/dates"},
                )

    def test_child_media_invalid_ids_reject_instead_of_binding_or_successful_noop(self):
        did = self.date("archive")
        for suffix in ("videos/{}/delete", "images/{}/delete", "images/{}/focus"):
            for raw_id in INVALID_IDS:
                with self.subTest(suffix=suffix, raw_id=raw_id):
                    self.assert_rejected_without_mutation(
                        "POST", f"/admin/dates/{did}/" + suffix.format(raw_id),
                        data=self.post_data(focus="50% 50%"),
                        headers={"Referer": "http://testserver/admin/dates"},
                    )

    def test_max_int64_child_media_id_keeps_existing_not_found_or_noop_semantics(self):
        did = self.date("archive")
        before = list(self.conn.iterdump())
        for kind in ("videos", "images"):
            response = self.client.post(f"/admin/dates/{did}/{kind}/{MAX_ID}/delete",
                                        data=self.post_data())
            self.assertIn(response.status_code, (303, 404))
            self.assertEqual(list(self.conn.iterdump()), before)
        self.assert_rejected_without_mutation(
            "POST", f"/admin/dates/{did}/images/{MAX_ID}/focus",
            data=self.post_data(focus="50% 50%"), headers={"Referer": "http://testserver/admin/dates"},
        )

    def test_child_reorder_rejects_boundary_ids_atomically(self):
        did = self.date("archive")
        cid = self.category()
        self.conn.execute("INSERT INTO date_categories(date_id,category_id) VALUES(?,?)", (did, cid))
        self.conn.commit()
        for raw_id in BOUNDARIES:
            for url, order in ((f"/admin/categories/{cid}/dates_reorder", f"{did},{raw_id}"),
                               (f"/admin/dates/{did}/images/reorder", raw_id)):
                with self.subTest(url=url, raw_id=raw_id):
                    self.assert_rejected_without_mutation(
                        "POST", url, data=self.post_data(order=order),
                        headers={"X-Requested-With": "fetch"},
                    )

    def test_booking_and_question_ids_are_rejected_before_sql_binding(self):
        paths = ("bookings/{}/delete", "questions/{}/answer", "questions/{}/delete",
                 "questions/{}/accept_time", "questions/{}/decline_time", "questions/{}/toggle",
                 "questions/reviews/{}/dismiss")
        for path in paths:
            for raw_id in BOUNDARIES:
                with self.subTest(path=path, raw_id=raw_id):
                    self.assert_rejected_without_mutation(
                        "POST", "/admin/" + path.format(raw_id), data=self.post_data(text="Не сохранять"),
                        headers={"Referer": "http://testserver/admin/questions"},
                    )

    def test_community_widget_rejects_boundary_ids(self):
        for raw_id in BOUNDARIES:
            with self.subTest(raw_id=raw_id):
                self.assert_rejected_without_mutation("GET", f"/admin/community/date/{raw_id}")

    def test_public_profile_and_public_child_ids_reject_boundaries(self):
        cid = self.category()
        token = self.conn.execute("SELECT link_token FROM categories WHERE id=?", (cid,)).fetchone()[0]
        for path in ("/u/{}", f"/c/{token}/ics/{{}}", f"/c/{token}/participant-avatar/{{}}",
                     "/d/synthetic-share/review/{}"):
            for raw_id in BOUNDARIES:
                with self.subTest(path=path, raw_id=raw_id):
                    self.assert_rejected_without_mutation("GET", path.format(raw_id))

    def test_operator_user_and_category_paths_reject_boundaries(self):
        self.set_operator(True)
        for raw_id in BOUNDARIES:
            with self.subTest(path="user", raw_id=raw_id):
                self.assert_rejected_without_mutation("GET", f"/operator/users/{raw_id}")
            with self.subTest(path="category", raw_id=raw_id):
                self.assert_rejected_without_mutation(
                    "POST", f"/operator/categories/{raw_id}/toggle", data=self.post_data(),
                    headers={"X-Requested-With": "fetch"},
                )

    def test_shared_id_boundary_and_ownership_helpers_accept_only_positive_int64(self):
        from fastapi import HTTPException
        from object_ids import require_object_id
        from ownership import get_owned_category, get_owned_date

        for value in (1, "1", int(MAX_ID), MAX_ID):
            with self.subTest(valid=value):
                self.assertEqual(require_object_id(value), int(value))
        invalid_values = (*INVALID_IDS, int(MAX_ID) + 1, -1, 0, True, False, 1.0, None)
        for value in invalid_values:
            with self.subTest(invalid=value):
                with self.assertRaises(HTTPException) as failure:
                    require_object_id(value)
                self.assertEqual(failure.exception.status_code, 404)
        before = list(self.conn.iterdump())
        for helper in (get_owned_category, get_owned_date):
            for value in (MAX_ID, int(MAX_ID), *invalid_values):
                with self.subTest(helper=helper.__name__, value=value):
                    with self.assertRaises(HTTPException) as failure:
                        helper(self.conn, value, self.owner_id)
                    self.assertEqual(failure.exception.status_code, 404)
                    self.assertEqual(list(self.conn.iterdump()), before)

    def test_operator_voter_filter_rejects_invalid_ids(self):
        self.set_operator(True)
        for raw_id in INVALID_IDS:
            with self.subTest(raw_id=raw_id):
                self.assert_rejected_without_mutation(
                    "GET", "/operator/bookings", params={"voter_id": raw_id})
        self.assertEqual(self.client.get("/operator/bookings", params={"voter_id": MAX_ID}).status_code, 200)

    def test_category_query_ids_reject_invalid_values_and_preserve_valid_selection(self):
        for path, field in (("/admin/dates", "cat"), ("/admin/dates/new", "category")):
            for value in INVALID_IDS:
                with self.subTest(path=path, value=value):
                    self.assert_rejected_without_mutation("GET", path, params={field: value})
            cid = self.category()
            for value in ("", str(cid), MAX_ID):
                self.assertEqual(self.client.get(path, params={field: value}).status_code, 200)

    def test_invalid_category_query_cannot_break_editor_error_rendering(self):
        for value in INVALID_IDS:
            with self.subTest(value=value):
                self.assert_rejected_without_mutation(
                    "POST", "/admin/dates/new", params={"category": value}, data=self.post_data())
                before = list(self.conn.iterdump())
                response = self.client.get("/admin/dates/new", params={
                    "return_to": "/admin/dates?cat=" + value})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(list(self.conn.iterdump()), before)

    def test_other_form_ids_reject_before_partial_writes(self):
        cid = self.category(owner=self.foreign_id)
        did = self.date("archive", owner=self.foreign_id)
        self.conn.execute("UPDATE categories SET choice_mode='single',voting_status='open',voting_deadline='2031-01-01T10:00' WHERE id=?", (cid,))
        self.conn.execute("UPDATE dates SET origin='guest',guest_token=? WHERE id=?", (f"u{self.owner_id}", did))
        self.conn.execute("INSERT INTO date_categories(date_id,category_id) VALUES(?,?)", (did, cid))
        self.conn.commit()
        token = self.conn.execute("SELECT link_token FROM categories WHERE id=?", (cid,)).fetchone()[0]
        own_cid = self.category()
        for raw_id in INVALID_IDS:
            for url, data in (
                (f"/c/{token}/report", self.post_data(target_type="date", target_id=raw_id)),
                (f"/c/{token}/propose/{did}/edit", self.post_data(name="Не менять", remove_image=[raw_id])),
                (f"/c/{token}/propose/{did}/edit", self.post_data(name="Не менять", remove_video=[raw_id])),
                (f"/admin/categories/{own_cid}/voting/resolve", self.post_data(winner_date_id=raw_id)),
            ):
                with self.subTest(url=url, raw_id=raw_id, fields=list(data)):
                    self.assert_rejected_without_mutation("POST", url, data=data)

    def test_valid_ids_keep_owner_and_operator_editor_and_attach_behavior(self):
        for operator in (0, 1):
            self.set_operator(operator)
            owner = self.foreign_id if operator else self.owner_id
            did = self.date("archive", owner=owner)
            cid = self.category(owner=owner)
            for url in (f"/admin/dates/{did}/edit", f"/admin/categories/{cid}"):
                self.assertEqual(self.client.get(url).status_code, 200)
            for action, expected in (("attach", 1), ("detach", 0)):
                response = self.client.post(f"/admin/categories/{cid}/{action}",
                                            data=self.post_data(date_id=str(did)))
                self.assertEqual(response.status_code, 303, response.text)
                self.assertNotIn("⚠", self.message(response))
                self.assertEqual(self.conn.execute(
                    "SELECT COUNT(*) FROM date_categories WHERE date_id=? AND category_id=?", (did, cid),
                ).fetchone()[0], expected)


def load_tests(loader, tests, pattern):
    # Общая fixture не запускает унаследованные bulk-регрессии повторно.
    return unittest.TestSuite(IdBoundaryTests(name) for name in sorted(
        name for name in IdBoundaryTests.__dict__ if name.startswith("test_")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
