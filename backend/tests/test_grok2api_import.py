import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from backend.integrations.grok2api_client import Grok2APIImportError
from backend.web import application
from backend.web.application import (
    AccountIdsBody,
    _find_account_grok2api_files,
    _import_account_grok2api,
)


class FakeStore:
    def __init__(self, records):
        self.records = {int(row["id"]): dict(row) for row in records}
        self.status_updates = []
        self.grokiq_calls = []

    def get_results_by_ids(self, ids):
        return [dict(self.records[account_id]) for account_id in ids if account_id in self.records]

    def update_remote_import_status(self, account_id, kind, *, status, error=""):
        self.status_updates.append((int(account_id), kind, status, error))
        record = self.records.get(int(account_id))
        if record is not None:
            record["grok2api_remote_status"] = status
            record["grok2api_remote_error"] = error
        return True

    def grokiq_deliveries(self, ids):
        return {}


class FakeClient:
    def __init__(self, results=None, errors=None, login_error=""):
        self.results = dict(results or {})
        self.errors = dict(errors or {})
        self.login_error = login_error
        self.calls = []
        self.logged_in = 0

    def login(self):
        if self.login_error:
            raise Grok2APIImportError(self.login_error)
        self.logged_in += 1
        return "token"

    def import_auth_file(self, path, format_name="grok_build"):
        self.calls.append((str(path), format_name))
        if format_name in self.errors:
            raise Grok2APIImportError(self.errors[format_name])
        return dict(self.results.get(format_name) or {"created": 1, "updated": 0, "synced": 1})

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def _write_json(path: Path, payload=None) -> Path:
    path.write_text(json.dumps(payload or {"accounts": [{}]}), encoding="utf-8")
    return path


class FindAccountGrok2APIFilesTests(unittest.TestCase):
    def test_discovers_build_web_and_console_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            email = "user@outlook.com"
            build = _write_json(root / f"g2a-{email}.json")
            web = _write_json(root / f"grok-web-{email}.json")
            console = _write_json(root / f"grok-console-{email}.json")
            found = _find_account_grok2api_files(
                {"email": email},
                {"grok2api_auth_dir": str(root)},
            )
            self.assertEqual(found["grok_build"], build.resolve())
            self.assertEqual(found["grok_web"], web.resolve())
            self.assertEqual(found["grok_console"], console.resolve())


class ImportAccountGrok2APITests(unittest.TestCase):
    def test_imports_available_formats_and_notifies_grokiq(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            email = "ok@example.com"
            build = _write_json(root / f"g2a-{email}.json")
            web = _write_json(root / f"grok-web-{email}.json")
            record = {"id": 7, "email": email}
            store = FakeStore([record])
            client = FakeClient(
                results={
                    "grok_build": {"created": 1, "updated": 0, "synced": 1, "syncFailed": 0},
                    "grok_web": {"created": 0, "updated": 1, "synced": 1, "syncFailed": 0},
                }
            )
            with mock.patch.object(
                application.grokiq,
                "enqueue_imported_account",
                return_value={"event_id": "evt-1"},
            ) as enqueue:
                imported = _import_account_grok2api(
                    store,
                    {"grok2api_auth_dir": str(root)},
                    record,
                    client,
                )
            self.assertEqual(
                [(Path(path).name, format_name) for path, format_name in client.calls],
                [
                    ("g2a-ok@example.com.json", "grok_build"),
                    ("grok-web-ok@example.com.json", "grok_web"),
                ],
            )
            self.assertEqual(imported["result"]["created"], 1)
            self.assertEqual(imported["result"]["updated"], 1)
            self.assertEqual(imported["grokiqNotification"]["eventId"], "evt-1")
            self.assertEqual(store.status_updates[-1][2], "success")
            enqueue.assert_called_once()

    def test_missing_files_raise_without_status_update(self):
        store = FakeStore([{"id": 3, "email": "missing@example.com"}])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                _import_account_grok2api(
                    store,
                    {"grok2api_auth_dir": tmp},
                    {"id": 3, "email": "missing@example.com"},
                    FakeClient(),
                )
        self.assertEqual(store.status_updates, [])

    def test_all_format_failures_mark_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            email = "bad@example.com"
            _write_json(root / f"g2a-{email}.json")
            store = FakeStore([{"id": 4, "email": email}])
            client = FakeClient(errors={"grok_build": "remote rejected"})
            with self.assertRaisesRegex(Grok2APIImportError, "remote rejected"):
                _import_account_grok2api(
                    store,
                    {"grok2api_auth_dir": str(root)},
                    {"id": 4, "email": email},
                    client,
                )
            self.assertEqual(store.status_updates[-1][2], "failed")


class BatchGrok2APIImportEndpointTests(unittest.TestCase):
    def _endpoint(self):
        return next(
            route.endpoint
            for route in application.create_app().routes
            if getattr(route, "path", "") == "/api/accounts/grok2api/import"
        )

    def test_rejects_unconfigured_remote(self):
        gr = mock.Mock()
        gr.config = {}
        gr.get_registration_repository.return_value = FakeStore([])
        with mock.patch.object(application, "_gr", return_value=gr):
            with self.assertRaisesRegex(HTTPException, "完整配置 Grok2API"):
                self._endpoint()(AccountIdsBody(ids=[1]))

    def test_imports_available_accounts_and_skips_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ready = "ready@example.com"
            _write_json(root / f"g2a-{ready}.json")
            records = [
                {"id": 1, "email": ready},
                {"id": 2, "email": "empty@example.com"},
            ]
            store = FakeStore(records)
            gr = mock.Mock()
            gr.config = {
                "grok2api_auth_dir": str(root),
                "grok2api_remote_url": "https://example.test",
                "grok2api_remote_username": "admin",
                "grok2api_remote_password": "secret",
            }
            gr.get_registration_repository.return_value = store
            client = FakeClient()
            with (
                mock.patch.object(application, "_gr", return_value=gr),
                mock.patch(
                    "backend.integrations.grok2api_client.Grok2APIClient.from_config",
                    return_value=client,
                ),
                mock.patch.object(
                    application.grokiq,
                    "enqueue_imported_account",
                    return_value={"event_id": "evt-2"},
                ),
            ):
                result = self._endpoint()(AccountIdsBody(ids=[1, 2, 1, 9]))
            self.assertEqual(result["imported"], 1)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(result["missing"], 1)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(result["created"], 1)
            self.assertEqual(client.logged_in, 1)
            self.assertEqual(len(client.calls), 1)

    def test_continues_after_per_account_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = "one@example.com"
            second = "two@example.com"
            _write_json(root / f"g2a-{first}.json")
            _write_json(root / f"g2a-{second}.json")
            store = FakeStore(
                [
                    {"id": 11, "email": first},
                    {"id": 12, "email": second},
                ]
            )
            gr = mock.Mock()
            gr.config = {
                "grok2api_auth_dir": str(root),
                "grok2api_remote_url": "https://example.test",
                "grok2api_remote_username": "admin",
                "grok2api_remote_password": "secret",
            }
            gr.get_registration_repository.return_value = store
            client = FakeClient()

            def fail_first(path, format_name="grok_build"):
                client.calls.append((str(path), format_name))
                if "one@" in Path(path).name:
                    raise Grok2APIImportError("first failed")
                return {"created": 0, "updated": 1, "synced": 1, "syncFailed": 0}

            client.import_auth_file = fail_first
            with (
                mock.patch.object(application, "_gr", return_value=gr),
                mock.patch(
                    "backend.integrations.grok2api_client.Grok2APIClient.from_config",
                    return_value=client,
                ),
                mock.patch.object(
                    application.grokiq,
                    "enqueue_imported_account",
                    return_value=None,
                ),
            ):
                result = self._endpoint()(AccountIdsBody(ids=[11, 12]))
            self.assertEqual(result["imported"], 1)
            self.assertEqual(result["failed"], 1)
            self.assertEqual(result["updated"], 1)
            self.assertEqual(result["errors"][0]["id"], 11)


if __name__ == "__main__":
    unittest.main()
