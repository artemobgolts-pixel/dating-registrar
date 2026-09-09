"""CFG-01: Telegram-бэкап требует явного получателя; сеть заменена заглушкой."""

import gzip
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
_DATA = tempfile.TemporaryDirectory(prefix="date4you-backup-delivery-")
os.environ.update({"DATA_DIR": _DATA.name, "SECRET_KEY": "backup-delivery-test",
                   "TG_BOT_TOKEN": "", "TG_CHAT_ID": "", "TG_BACKUP_CHAT_ID": ""})

import config
import tasks


class BackupDeliveryTests(unittest.TestCase):
    def test_empty_recipient_skips_compression_and_network(self):
        self.assertEqual(config.TG_BACKUP_CHAT_ID, "")
        with patch.object(tasks, "TG_BACKUP_CHAT_ID", ""), \
                patch.object(tasks.gzip, "open") as compress, \
                patch.object(tasks.notify, "send_document") as send:
            self.assertFalse(tasks.ship_backup_to_tg(Path(_DATA.name) / "missing.db"))
        compress.assert_not_called()
        send.assert_not_called()

    def test_explicit_recipient_gets_snapshot_and_temp_gzip_is_removed(self):
        snapshot = Path(_DATA.name) / "synthetic.db"
        snapshot.write_bytes(b"synthetic backup fixture")
        seen = []

        def send(chat, path, **kwargs):
            seen.append((chat, gzip.decompress(path.read_bytes()), kwargs["filename"]))
            return True

        with patch.object(tasks, "TG_BACKUP_CHAT_ID", "-100123456"), \
                patch.object(tasks.tempfile, "gettempdir", return_value=_DATA.name), \
                patch.object(tasks.notify, "send_document", side_effect=send):
            self.assertTrue(tasks.ship_backup_to_tg(snapshot))
        self.assertEqual(seen, [("-100123456", b"synthetic backup fixture", "synthetic.db.gz")])
        self.assertFalse(snapshot.with_suffix(".db.gz").exists())
        self.assertTrue(snapshot.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
