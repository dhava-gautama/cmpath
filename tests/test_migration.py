import json
from pathlib import Path
import tempfile
import unittest

from cmpath import TaskMemory
from cmpath.migrate import migrate_v02


class MigrationTests(unittest.TestCase):
    def fixture(self):
        return {"version":2,"graph":{"active_id":"B1","paths":{
            "B1":{"waypoint":{"title":"Client invoice","key_facts":[]},"depends_on":["A1"],"segments":[{"start_ts":2,"end_ts":None}],"snapshot":{"next":"email"},"state":"full"}}},
            "log":[{"ts":3,"role":"user","content":"Draft invoice"}],
            "evicted_rows":[{"id":"A1","record_json":json.dumps({"id":"A1","waypoint":{"title":"Original report","key_facts":["Budget 3400"]},"depends_on":[],"snapshot":{"file":"report.md"},"state":"evicted"}),"messages_json":json.dumps([{"ts":1,"role":"assistant","content":"Budget 3400 confirmed."}])}]}

    def test_migration_restores_archive_and_dependencies(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)/"legacy.json"
            source.write_text(json.dumps(self.fixture()))
            dest = Path(temp)/"new.db"
            result = migrate_v02(source,dest)
            with TaskMemory(dest) as memory:
                parent = result["legacy_to_task_id"]["A1"]
                child = result["legacy_to_task_id"]["B1"]
                self.assertEqual(memory.task(child).parents,(parent,))
                self.assertEqual(memory.resolve("Return to A1").task_id,parent)
                self.assertEqual(memory.state()["active_task"],child)
                self.assertEqual(memory.task(child).snapshot,{"next":"email"})
                self.assertTrue(memory.search("3400"))
                self.assertFalse(memory.current_facts(parent))

    def test_failed_migration_leaves_no_partial_database(self):
        with tempfile.TemporaryDirectory() as temp:
            data = self.fixture()
            data["graph"]["paths"]["B1"]["depends_on"]=["MISSING"]
            source = Path(temp)/"legacy.json"
            source.write_text(json.dumps(data))
            dest = Path(temp)/"new.db"
            with self.assertRaises(ValueError):
                migrate_v02(source,dest)
            self.assertFalse(dest.exists())

    def test_existing_destination_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp)/"existing.db"
            dest.write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                migrate_v02(Path(temp)/"missing.json",dest)
            self.assertEqual(dest.read_bytes(),b"existing")
