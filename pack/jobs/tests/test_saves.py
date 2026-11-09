"""G05 real filesystem/CLI transactions; synthetic bytes, no player saves."""

import contextlib
import copy
import io
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

JOBS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JOBS))
# Standalone unittest discovery uses this checkout's job modules.
import library  # noqa: E402
import saves  # noqa: E402
from core import Cancelled, Context, Failure, atomic  # noqa: E402

import build  # noqa: E402

GID = "0100000000000000"
USER = "00000000000000010000000000000000"
PROGRESS = "bis/user/save/0000000000000001/0/progress.bin"


def synthetic_index(entries):
    """LibHac IMKV wire format; only synthetic identifiers and bytes."""
    data = struct.pack("<4sII", b"IMKV", 0, len(entries))
    for sid, program, kind, user in entries:
        key, value = bytearray(0x40), bytearray(0x40)
        struct.pack_into("<3Q", key, 0, int(program, 16), int(user[:16], 16), int(user[16:], 16))
        key[0x20] = kind
        struct.pack_into("<Q", value, 0, sid)
        value[0x18] = 1
        data += struct.pack("<4sII", b"IMEN", len(key), len(value)) + key + value
    return data


class SaveTests(unittest.TestCase):
    def test_own_backup_does_not_read_oversized_manifest_into_memory(self):
        # 审计 P1-47：manifest.json 声明大小超过 MAX_MANIFEST（8 MiB）时不整份读进内存，直接交原流程拒绝（返回 True）；
        #   小 manifest 照常读。期望手算：9 MiB > 8 MiB → 不调 ZipFile.read；1 KiB → 调一次
        with tempfile.TemporaryDirectory() as d:
            big, small = Path(d) / "big.zip", Path(d) / "small.zip"
            with zipfile.ZipFile(big, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("manifest.json", b" " * (9 << 20))
            with zipfile.ZipFile(small, "w") as z:
                z.writestr("manifest.json", json.dumps({"format": "S2ArmSave"}))
            with patch.object(zipfile.ZipFile, "read", side_effect=AssertionError("oversized manifest was read")) as r:
                self.assertTrue(saves.own_backup(big))
                self.assertEqual(r.call_count, 0)
            self.assertTrue(saves.own_backup(small))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="s2arm saves ")
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name).resolve()
        self.events = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.events)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.request = dict(
            schema=1,
            operation="setup",
            privateRoot=str(self.directory / "private"),
            engineDir="/nonexistent-engine",
            library=str(self.directory / "library"),
            libraryId="",
        )
        self.request["libraryId"] = library.setup(self.context())["libraryId"]
        self.request["gameId"] = GID
        self.game = dict(
            id=GID,
            name="Synthetic game",
            packages=[
                dict(
                    type="base",
                    titleId=GID,
                    baseId=GID,
                    version=1,
                    sha256="a" * 64,
                    path="sources/fixture.nsp",
                    display="1.0",
                )
            ],
            hidden=False,
        )
        atomic(self.directory / "library/catalog.json", dict(schema=1, games=[self.game]))
        self.root = self.directory / "library/saves" / GID
        self.put(PROGRESS, b"checkpoint-one")
        self.put("bis/user/save/0000000000000001/ExtraData0", b"extra-data")
        self.put(saves.SAVE_INDEX, synthetic_index([(1, GID, 1, USER)]))
        self.put(
            "system/Profiles.json",
            json.dumps(
                dict(last_opened=USER, profiles=[dict(user_id=USER, name="Fixture")])
            ).encode(),
        )
        self.put("system/prod.keys", b"test-only-not-a-real-key")
        (self.root / "bis/user/save/0000000000000001/0/empty").mkdir()
        self.archive = self.directory / "progress.s2asave"

    def context(self, **extra):
        return Context(self.request | dict(taskId=uuid.uuid4().hex) | extra)

    def put(self, path, content):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)

    def backup(self):
        return saves.backup(self.context(operation="saveBackup", path=str(self.archive)))

    def preview(self):
        return saves.inspect(self.context(operation="saveInspect", path=str(self.archive)))

    def prepare_restore(self):
        self.backup()
        self.put(PROGRESS, b"checkpoint-two")
        return self.context(operation="saveRestore", path=str(self.archive), **self.preview())

    def rewrite_archive(self, mutate=None, extra=None):
        with zipfile.ZipFile(self.archive) as z:
            data = {i.filename: z.read(i) for i in z.infolist()}
        if mutate:
            manifest = json.loads(data["manifest.json"])
            mutate(manifest)
            data["manifest.json"] = json.dumps(manifest).encode()
        with zipfile.ZipFile(self.archive, "w") as z:
            for name, content in data.items():
                z.writestr(name, content)
            for name, content in (extra or {}).items():
                z.writestr(name, content)

    def test_round_trip_preserves_indices_profiles_empty_dirs_and_other_data(self):
        c = self.prepare_restore()
        self.put("system/prod.keys", b"unchanged-after-restore")
        # Recheck after changing unrelated runtime data too.
        c.r.update(self.preview())
        result = saves.restore(c)
        self.assertTrue(result["committed"])
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-one")
        self.assertEqual((self.root / "system/prod.keys").read_bytes(), b"unchanged-after-restore")
        self.assertEqual(
            (self.root / saves.SAVE_INDEX).read_bytes(),
            synthetic_index([(1, GID, 1, USER)]),
        )
        self.assertTrue((self.root / "bis/user/save/0000000000000001/0/empty").is_dir())
        self.assertEqual(json.loads((self.root / saves.PROFILE).read_text())["last_opened"], USER)
        prior = Path(result["previousBackup"])
        self.assertTrue(prior.is_file())
        with saves.open_archive(self.context(), prior) as (z, _):
            self.assertEqual(z.read("payload/" + PROGRESS), b"checkpoint-two")
            self.assertNotIn("payload/system/prod.keys", z.namelist())
        self.assertFalse(list((self.directory / "library/save-transactions").glob("*.json")))

    def test_export_manifest_lists_exact_files_without_keys(self):
        self.backup()
        result = self.preview()
        m = result["manifest"]
        self.assertEqual(m["game"]["id"], GID)
        self.assertEqual(m["game"]["version"], 1)
        self.assertEqual(m["users"], dict(ids=[USER], active=USER))
        self.assertTrue(any(f["path"].startswith("bis/system/save/") for f in m["files"]))
        self.assertFalse(any("prod.keys" in f["path"] for f in m["files"]))
        self.assertEqual(self.archive.stat().st_mode & 0o777, 0o600)

    def test_zip64_directory_bounds_are_checked_before_zipfile_loads_it(self):
        self.backup()
        original = self.archive.read_bytes()
        offset = original.rfind(b"PK\x05\x06")
        eocd = struct.unpack("<4s4H2IH", original[offset:])
        count, size, start = eocd[4:7]

        def zip64(entries, directory_size):
            record = struct.pack(
                "<4sQ2H2I4Q",
                b"PK\x06\x06",
                44,
                45,
                45,
                0,
                0,
                entries,
                entries,
                directory_size,
                start,
            )
            locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, offset, 1)
            return original[:offset] + record + locator + original[offset:]

        self.archive.write_bytes(zip64(count, size))
        self.assertEqual(self.preview()["manifest"]["game"]["id"], GID)
        for entries, directory_size in ((saves.MAX_FILES + 2, size), (count, 1 << 40)):
            self.archive.write_bytes(zip64(entries, directory_size))
            with patch("saves.zipfile.ZipFile", side_effect=AssertionError("must bound first")):
                with self.assertRaises(Failure):
                    self.preview()

    def test_restore_result_carries_cleanup_warning_when_transaction_cannot_be_removed(self):
        # 审计 P2-10：换上新档后删事务目录失败 → 结果里带 cleanupWarning 原句（Mac 面板用它，不再写「已完成」）；正常删掉时为空串
        real = saves.shutil.rmtree
        def failing(path, *a, **k):
            if Path(path).parent.name == "save-transactions":   # 只让删事务目录本身失败（事务内暂存子目录照常删）
                raise OSError("synthetic cleanup failure")
            return real(path, *a, **k)
        c = self.prepare_restore()
        with patch.object(saves.shutil, "rmtree", side_effect=failing):
            result = saves.restore(c)
        self.assertTrue(result["committed"])
        self.assertIn("事务清理尚未完成", result["cleanupWarning"])
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-one")
        self.put(PROGRESS, b"checkpoint-two")   # 第二次正常恢复：备份已在，只改进度再恢复
        c = self.context(operation="saveRestore", path=str(self.archive), **self.preview())
        self.assertEqual(saves.restore(c)["cleanupWarning"], "")

    def test_can_restore_into_new_game_save_root(self):
        self.backup()
        import shutil

        shutil.rmtree(self.root)
        c = self.context(operation="saveRestore", path=str(self.archive), **self.preview())
        self.assertTrue(saves.restore(c)["committed"])
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-one")

    def test_empty_status_is_normal_and_backup_does_not_create_save_data(self):
        import shutil

        shutil.rmtree(self.root)
        c = self.context(operation="saveStatus")
        self.assertFalse(saves.status(c)["hasSave"])
        self.assertEqual(c.job["diagnostics"], [])
        with self.assertRaises(Failure):
            self.backup()
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.root.exists())

    def test_wrong_game_version_and_user_are_rejected(self):
        self.backup()
        original = self.archive.read_bytes()
        for field, value in (
            ("id", "0100000000000001"),
            ("version", 2),
            ("contentIdentity", "b" * 64),
        ):
            with self.subTest(field=field):
                self.archive.write_bytes(original)
                self.rewrite_archive(
                    lambda m, field=field, value=value: m["game"].update({field: value})
                )
                with self.assertRaises(Failure):
                    self.preview()
                self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-one")
        self.archive.write_bytes(original)
        other = "00000000000000020000000000000000"
        self.put(
            saves.PROFILE,
            json.dumps(dict(last_opened=other, profiles=[dict(user_id=other)])).encode(),
        )
        self.put(saves.SAVE_INDEX, synthetic_index([(1, GID, 1, other)]))
        with self.assertRaises(Failure):
            self.preview()

    def test_index_owners_override_current_profile_and_profiles_are_labels_only(self):
        owner = "78626c000000000153574e4154495645"
        self.put(saves.SAVE_INDEX, synthetic_index([(1, GID, 1, owner)]))
        # Current profile differs, and the owner does not even need a display profile.
        self.backup()
        m = self.preview()["manifest"]
        self.assertEqual(m["users"], dict(ids=[owner], active=owner))
        self.assertEqual(
            m["saveEntries"],
            [dict(saveId="0000000000000001", programId=GID, type="account", userId=owner)],
        )
        self.put(saves.PROFILE, json.dumps(dict(last_opened="not-an-owner", profiles=[])).encode())
        self.assertEqual(self.preview()["manifest"]["users"]["active"], owner)

    def test_extra_profiles_and_different_current_user_do_not_block_restore(self):
        self.backup()
        other = "00000000000000020000000000000000"
        self.put(
            saves.PROFILE,
            json.dumps(
                dict(
                    last_opened=other,
                    profiles=[dict(user_id=USER), dict(user_id=other, name="Other")],
                )
            ).encode(),
        )
        self.put(PROGRESS, b"checkpoint-two")
        c = self.context(operation="saveRestore", path=str(self.archive), **self.preview())
        self.assertTrue(saves.restore(c)["committed"])
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-one")

    def test_old_manifest_without_optional_fields_uses_payload_index(self):
        self.backup()

        def legacy(m):
            m.pop("saveEntries")
            m.pop("deviceSettings")
            m["users"] = dict(ids=["f" * 32], active="f" * 32)

        self.rewrite_archive(legacy)
        c = self.context(operation="saveRestore", path=str(self.archive), **self.preview())
        self.assertTrue(saves.restore(c)["committed"])
        self.assertEqual(self.preview()["manifest"]["deviceSettings"], {})
        self.assertEqual(self.preview()["manifest"]["users"]["ids"], [USER])

    def test_device_and_each_account_save_have_independent_owners(self):
        owner = "78626c000000000153574e4154495645"
        self.put("bis/user/save/0000000000000002/0/progress.bin", b"device")
        self.put("bis/user/save/0000000000000003/0/progress.bin", b"second-account")
        self.put(
            saves.SAVE_INDEX,
            synthetic_index([(1, GID, 1, USER), (2, GID, 3, "0" * 32), (3, GID, 1, owner)]),
        )
        self.backup()
        m = self.preview()["manifest"]
        self.assertEqual([e["type"] for e in m["saveEntries"]], ["account", "device", "account"])
        self.assertEqual([e["userId"] for e in m["saveEntries"]], [USER, "0" * 32, owner])
        self.assertEqual(m["deviceSettings"], {})

    def test_device_settings_are_optional_preserved_metadata_not_applied(self):
        self.backup()
        settings = dict(
            system_time="2000-01-01T00:00:00Z", language="en-US", region="USA", timezone="UTC"
        )
        self.rewrite_archive(lambda m: m.update(deviceSettings=settings))
        self.assertEqual(self.preview()["manifest"]["deviceSettings"], settings)

    def test_forged_manifest_ownership_is_rejected(self):
        self.backup()
        self.rewrite_archive(lambda m: m["saveEntries"][0].update(userId="f" * 32))
        with self.assertRaises(Failure):
            self.preview()

    def test_bad_indices_fail_without_profile_or_working_copy_fallback(self):
        valid = (self.root / saves.SAVE_INDEX).read_bytes()
        for data in (
            b"IMKV",
            valid[:-1],
            valid + b"extra",
            valid.replace(b"IMEN", b"BAD!"),
            valid[:12] + valid[12:] * 2,
        ):
            with self.subTest(length=len(data)):
                self.put(saves.SAVE_INDEX, data)
                with self.assertRaises(Failure):
                    self.backup()
                self.assertFalse(self.archive.exists())
        self.put(saves.SAVE_INDEX.replace("/0/", "/1/"), valid)
        (self.root / saves.SAVE_INDEX).unlink()
        with self.assertRaises(Failure):
            self.backup()

    def test_unindexed_data_is_rejected(self):
        self.put("bis/user/save/0000000000000002/0/progress.bin", b"unindexed")
        with self.assertRaises(Failure):
            self.backup()

    def test_changed_archive_or_target_requires_a_new_preview(self):
        c = self.prepare_restore()
        self.put(PROGRESS, b"checkpoint-three")
        with self.assertRaises(Failure):
            saves.restore(c)
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-three")
        c.r.update(self.preview())
        self.rewrite_archive(lambda m: m.update(createdAt="changed"))
        with self.assertRaises(Failure):
            saves.restore(c)
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-three")

    def test_unsafe_and_corrupt_archives_are_rejected_without_writes(self):
        self.backup()
        original = self.archive.read_bytes()
        mutations = [
            lambda m: m.update(schema=2),
            lambda m: m["files"][0].update(path="../escape"),
            lambda m: m["files"][0].update(sha256="0" * 64),
            lambda m: m["files"][0].update(size=1 << 40),
            lambda m: m["files"].append(copy.deepcopy(m["files"][0])),
            lambda m: m["users"].update(active="0" * 32),
            lambda m: m.update(layout="unknown"),
        ]
        for mutate in mutations:
            self.archive.write_bytes(original)
            self.rewrite_archive(mutate)
            with self.assertRaises(Failure):
                self.preview()
        for name in ("../outside", "/absolute", "payload/bis/user/save/UPPER", "unexpected"):
            self.archive.write_bytes(original)
            self.rewrite_archive(extra={name: b"bad"})
            with self.assertRaises(Failure):
                self.preview()
        self.archive.write_bytes(b"not a zip")
        with self.assertRaises(Failure):
            self.preview()
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-one")

    def test_zip_symlink_and_duplicate_names_are_rejected(self):
        self.backup()
        original = self.archive.read_bytes()
        info = zipfile.ZipInfo("payload/bis/user/save/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(self.archive, "a") as z:
            z.writestr(info, "/outside")
        with self.assertRaises(Failure):
            self.preview()
        self.archive.write_bytes(original)
        with zipfile.ZipFile(self.archive, "a") as z:
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                z.writestr("manifest.json", b"{}")
        with self.assertRaises(Failure):
            self.preview()

    def test_source_symlinks_and_case_collisions_are_rejected(self):
        link = self.root / "bis/user/save/link"
        link.symlink_to(self.directory / "outside")
        with self.assertRaises(Failure):
            self.backup()
        link.unlink()
        # Case-collision archive paths are rejected even on case-sensitive hosts.
        self.backup()
        with zipfile.ZipFile(self.archive, "a") as z:
            z.writestr("MANIFEST.json", b"{}")
        with self.assertRaises(Failure):
            self.preview()

    def test_existing_backup_is_never_overwritten(self):
        self.archive.write_bytes(b"existing backup")
        with self.assertRaises(Failure):
            self.backup()
        self.assertEqual(self.archive.read_bytes(), b"existing backup")

    def test_disk_shortage_and_cancellation_leave_original_intact(self):
        c = self.prepare_restore()
        from collections import namedtuple

        usage = namedtuple("usage", "total used free")(100, 99, 1)
        with patch.object(saves.shutil, "disk_usage", return_value=usage):
            with self.assertRaises(Failure):
                saves.restore(c)
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-two")
        original = saves.copy_tree

        def cancel(*args):
            original(*args)
            c.stop()

        with patch.object(saves, "copy_tree", side_effect=cancel):
            with self.assertRaises(Cancelled):
                saves.restore(c)
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-two")

    def test_cancel_between_renames_rolls_back_original(self):
        c = self.prepare_restore()
        rename = os.rename

        def cancel(source, destination):
            rename(source, destination)
            if Path(source) == self.root and Path(destination).name == "data":
                c.stop()

        with patch.object(saves.os, "rename", side_effect=cancel):
            with self.assertRaises(Cancelled):
                saves.restore(c)
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-two")
        self.assertFalse(list((self.directory / "library/save-transactions").glob("*.json")))

    def test_failure_after_publish_rolls_back_original(self):
        c = self.prepare_restore()
        rename = os.rename

        def broken(source, destination):
            rename(source, destination)
            if Path(source).name == "new" and Path(destination) == self.root:
                raise OSError("synthetic failure")

        with patch.object(saves.os, "rename", side_effect=broken):
            with self.assertRaises(OSError):
                saves.restore(c)
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-two")

    def test_failed_rollback_keeps_journal_and_original_for_next_attempt(self):
        c = self.prepare_restore()
        rename = os.rename

        def fail_rollback(source, destination):
            if Path(source).name == "data" and Path(destination) == self.root:
                raise OSError("synthetic rollback failure")
            rename(source, destination)
            if Path(source) == self.root:
                c.stop()

        with patch.object(saves.os, "rename", side_effect=fail_rollback):
            with self.assertRaises(Failure):
                saves.restore(c)
        self.assertEqual(c.job["diagnostics"][-1]["code"], "save_recovery")
        self.assertTrue(list((self.directory / "library/save-transactions").glob("*.json")))
        self.assertTrue(saves.status(self.context(operation="saveStatus"))["recovered"])
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-two")

    def broken_journal(self):
        # 事务记录损坏（回退无从做起）：与 iPhone 恢复失败同一种状态（审计 18:00 P0-3）
        j = self.directory / "library/save-transactions" / (GID + ".json")
        j.parent.mkdir(parents=True, exist_ok=True)
        j.write_text("{}")
        return j

    def test_recovery_failure_still_allows_status_backup_and_inspect(self):
        self.backup()
        j = self.broken_journal()
        state = saves.status(self.context(operation="saveStatus"))
        self.assertEqual(state["recoveryFailed"], str(j))
        self.assertTrue(state["hasSave"])
        self.archive.unlink()
        self.assertGreater(self.backup()["fileCount"], 0)   # 当前存档照样能备份出去
        self.preview()
        self.assertTrue(j.exists())   # 确认前不删事务记录

    def test_recovery_failure_blocks_launch_until_confirmed(self):
        j = self.broken_journal()
        c = self.context(operation="launch")
        with self.assertRaises(Failure) as raised:
            saves.recover(c, self.game)
        self.assertEqual(raised.exception.code, "save_recovery")
        self.assertIn(str(j), str(raised.exception))
        self.assertIn("仍要启动", str(raised.exception))
        c = self.context(operation="launch", overrideRecovery=True)
        self.assertFalse(saves.recover(c, self.game, override=True))
        self.assertEqual(c.job["diagnostics"][-1]["code"], "save_recovery_overridden")
        self.assertTrue(j.exists())   # 放行也不删记录：下次启动再试恢复

    def test_recovery_failure_still_blocks_restore(self):
        c = self.prepare_restore()
        self.broken_journal()
        with self.assertRaises(Failure) as raised:
            saves.restore(c)
        self.assertEqual(raised.exception.code, "save_recovery")
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-two")

    def test_cancel_after_commit_is_a_successful_cli_restore(self):
        c = self.prepare_restore()
        request = self.directory / "late-cancel.json"
        request.write_text(json.dumps(c.r))
        request.chmod(0o600)
        code = """import sys
sys.path.insert(0,sys.argv[1])
import main,saves
original=saves.restore
def late_cancel(c):
    result=original(c)
    c.stop()
    return result
saves.restore=late_cancel
sys.argv=["main.py","--request",sys.argv[2]]
sys.exit(main.main())
"""
        p = subprocess.run(
            [sys.executable, "-B", "-c", code, str(JOBS), str(request)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout.splitlines()[-1])["state"], "succeeded")
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-one")

    def crash_restore(self, when):
        c = self.prepare_restore()
        request = self.directory / "crash-request.json"
        request.write_text(json.dumps(c.r))
        request.chmod(0o600)
        code = """import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import saves
from core import Context
c=Context(json.loads(Path(sys.argv[2]).read_text()))
root=Path(c.library)/"saves"/c.r["gameId"]
rename=os.rename
def crash(src,dst):
    rename(src,dst)
    if (sys.argv[3]=="old" and Path(src)==root) or (sys.argv[3]=="new" and Path(dst)==root): os._exit(70)
if sys.argv[3]=="commit":
    original=saves.atomic
    def crash_commit(path,value):
        original(path,value)
        if value.get("phase")=="committed": os._exit(70)
    saves.atomic=crash_commit
else: saves.os.rename=crash
saves.restore(c)
"""
        # The patch is confined to this test child; production has no fault hook.
        p = subprocess.run(
            [sys.executable, "-B", "-c", code, str(JOBS), str(request), when],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(p.returncode, 70, p.stderr)

    def test_process_death_after_old_rename_recovers_before_launch(self):
        self.crash_restore("old")
        self.assertFalse(self.root.exists())
        # Prove launch calls recovery before attempting to validate/start a host.
        with patch.object(
            build, "validate_runtime", side_effect=Failure("stop before game launch")
        ):
            with self.assertRaises(Failure):
                build.launch(self.context(operation="launch"))
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-two")

    def test_process_death_after_new_rename_recovers_original(self):
        self.crash_restore("new")
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-one")
        self.assertTrue(saves.status(self.context(operation="saveStatus"))["recovered"])
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-two")

    def test_process_death_after_commit_keeps_restored_data(self):
        self.crash_restore("commit")
        self.assertFalse(saves.status(self.context(operation="saveStatus"))["recovered"])
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-one")

    def test_live_execution_lock_blocks_backup(self):
        import filelock

        with (self.directory / "library/.execution.lock").open("a") as f:
            filelock.try_lock(f)
            with self.assertRaises(Failure):
                self.backup()
        self.assertFalse(self.archive.exists())

    def test_cli_saves_work_without_engine_sdk_or_player_keys(self):
        c = self.context(
            operation="saveBackup", path=str(self.archive), engineBundle="/missing-engine"
        )
        request = self.directory / "request.json"
        request.write_text(json.dumps(c.r))
        request.chmod(0o600)
        p = subprocess.run(
            [sys.executable, "-B", "-I", str(JOBS / "main.py"), "--request", str(request)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        events = [json.loads(line) for line in p.stdout.splitlines()]
        self.assertEqual(events[-1]["state"], "succeeded")
        self.assertEqual(events[-1]["result"]["outputPath"], str(self.archive))
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        self.assertFalse((c.private / "engines").exists())

    def test_cli_preserves_save_error_codes_at_terminal_failure(self):
        self.backup()
        self.rewrite_archive(lambda m: m["game"].update(id="0100000000000001"))
        for expected in ("save_wrong_game", "save_invalid"):
            c = self.context(operation="saveInspect", path=str(self.archive))
            request = self.directory / "rejected-request.json"
            request.write_text(json.dumps(c.r))
            request.chmod(0o600)
            p = subprocess.run(
                [sys.executable, "-B", "-I", str(JOBS / "main.py"), "--request", str(request)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(p.returncode, 1, p.stderr)
            events = [json.loads(line) for line in p.stdout.splitlines()]
            self.assertEqual(events[-1]["state"], "failed")
            self.assertEqual(events[-1]["diagnostics"][-1]["code"], expected)
            self.assertNotIn("job_failed", [d["code"] for d in events[-1]["diagnostics"]])
            self.archive.write_bytes(b"synthetic corrupt archive")


class ExternalImportTests(unittest.TestCase):
    """社区存档布局导入（JKSV / Checkpoint / Ryujinx …）：转成本游戏 .s2asave 后走原恢复流程。"""

    setUp = SaveTests.setUp
    context = SaveTests.context
    put = SaveTests.put

    def community(self, name, files):
        d = self.directory / name
        for rel, data in files.items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_bytes(data)
        return d

    def import_(self, source):
        preview = saves.inspect(self.context(operation="saveInspect", path=str(source)))
        return saves.restore(self.context(operation="saveRestore", path=str(source), **preview)), preview

    def test_jksv_directory_replaces_only_this_games_slot(self):
        src = self.community("JKSV", {"Synthetic game/Player - 2026-10-20/progress.bin": b"community",
                                       "Synthetic game/Player - 2026-10-20/sub/extra.bin": b"more"})
        index = (self.root / saves.SAVE_INDEX).read_bytes()
        with patch.object(saves.save_formats, "detect", return_value=[]):
            result, preview = self.import_(src)
        self.assertTrue(result["committed"])
        slot = self.root / "bis/user/save/0000000000000001/0"
        self.assertEqual((slot / "progress.bin").read_bytes(), b"community")
        self.assertEqual((slot / "sub/extra.bin").read_bytes(), b"more")
        self.assertFalse((slot / "empty").exists())
        self.assertEqual((self.root / "bis/user/save/0000000000000001/ExtraData0").read_bytes(), b"extra-data")
        self.assertEqual((self.root / saves.SAVE_INDEX).read_bytes(), index)
        self.assertTrue((self.root / "system/Profiles.json").is_file())
        self.assertTrue(Path(result["previousBackup"]).is_file())
        self.assertEqual(preview["fileCount"], len(preview["manifest"]["files"]))

    def test_game_with_account_and_device_saves_takes_each_folder_to_its_own_slot(self):
        # NX2Apple 的马车 8 这类游戏：save → 账户存档，save_device → 设备存档；互不影响
        self.put(saves.SAVE_INDEX, synthetic_index([(1, GID, 1, USER), (2, GID, 3, "0" * 32)]))
        self.put("bis/user/save/0000000000000002/0/device.bin", b"old-device")
        src = self.community("nx", {"save/progress.bin": b"account-new"})
        self.assertTrue(self.import_(src / "save")[0]["committed"])
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"account-new")
        self.assertEqual((self.root / "bis/user/save/0000000000000002/0/device.bin").read_bytes(), b"old-device")
        src = self.community("nx2", {"save_device/device.bin": b"device-new"})
        self.assertTrue(self.import_(src / "save_device")[0]["committed"])
        self.assertEqual((self.root / "bis/user/save/0000000000000002/0/device.bin").read_bytes(), b"device-new")
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"account-new")

    def test_zero_directory_judged_against_this_games_own_slot(self):
        # 10-27：社区存档唯一子目录 0/ —— 游戏自己的槽 0 顶层是 progress.bin → 0/ 是提交目录，剥掉；
        # 游戏自己的槽 0 顶层有 0/ 目录（塞尔达式槽位）→ 不剥，原样放进槽 0
        src = self.community("Ryujinx zero", {"0/progress.bin": b"stripped"})
        with patch.object(saves.save_formats, "detect", return_value=[]):
            self.assertTrue(self.import_(src)[0]["committed"])
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"stripped")
        slot = self.root / "bis/user/save/0000000000000001/0"
        (slot / "progress.bin").unlink()
        (slot / "0").mkdir()
        (slot / "0/progress.bin").write_bytes(b"own slot")
        src = self.community("Slot zero", {"0/progress.bin": b"kept"})
        with patch.object(saves.save_formats, "detect", return_value=[]):
            self.assertTrue(self.import_(src)[0]["committed"])
        self.assertEqual((slot / "0/progress.bin").read_bytes(), b"kept")
        self.assertFalse((slot / "progress.bin").exists())

    def test_checkpoint_directory_takes_program_from_folder_name(self):
        src = self.community("Checkpoint", {f"saves/0x{GID} Synthetic/20261020-120000 Player/progress.bin": b"ck"})
        self.assertTrue(self.import_(src)[0]["committed"])
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"ck")

    def test_structure_doubt_reaches_the_preview_and_survives_a_cache_hit(self):
        # 没有参照（这款游戏自己的槽 0 是空的）→ 唯一子目录 0/ 剥不剥比不出，inspect 要带提示；第二次命中缓存照样带
        slot = self.root / "bis/user/save/0000000000000001/0"
        (self.root / PROGRESS).unlink()
        (slot / "empty").rmdir()
        src = self.community("Doubt", {"0/progress.bin": b"doubt"})
        # 期望值手写：玩家看到的就是这一句（同一句在 Localizable.json 里有英文）
        want = "这份存档的目录结构不常见，导入时会按原样照搬。如果游戏读不到进度，请换一份存档。"
        with patch.object(saves.save_formats, "detect", return_value=[]):
            first = saves.inspect(self.context(operation="saveInspect", path=str(src)))
            second = saves.inspect(self.context(operation="saveInspect", path=str(src)))
        self.assertEqual(first["notice"], want)
        self.assertEqual(second["notice"], want)
        self.assertEqual(first["archiveSha256"], second["archiveSha256"])
        cache = self.directory / "library/save-imports" / GID
        self.assertEqual(len(list(cache.glob("*.s2asave"))), 1)
        # 上一版启动器留下的缓存没有提示小文件：只补算这一句，归档不重转
        next(cache.glob("*.notice.json")).unlink()
        with patch.object(saves.save_formats, "detect", return_value=[]):
            third = saves.inspect(self.context(operation="saveInspect", path=str(src)))
        self.assertEqual(third["notice"], want)
        self.assertEqual(third["archiveSha256"], first["archiveSha256"])
        self.assertEqual(len(list(cache.glob("*.s2asave"))), 1)
        # 有提示也不拒绝恢复：按不剥导入，0/ 原样放进槽 0
        result = saves.restore(self.context(operation="saveRestore", path=str(src), **third))
        self.assertTrue(result["committed"])
        self.assertEqual((slot / "0/progress.bin").read_bytes(), b"doubt")

    def test_structure_doubt_when_both_interpretations_match_reaches_the_preview(self):
        # 有参照但两种解释都不成立 / 都成立 → 都不下结论，同样按不剥导入并在预览里提示
        want = "这份存档的目录结构不常见，导入时会按原样照搬。如果游戏读不到进度，请换一份存档。"
        # 都不成立：游戏自己的槽 0 顶层是 progress.bin + empty，社区存档 0/ 里是 data.bin
        neither = self.community("Neither", {"0/data.bin": b"neither"})
        with patch.object(saves.save_formats, "detect", return_value=[]):
            preview = saves.inspect(self.context(operation="saveInspect", path=str(neither)))
        self.assertEqual(preview["notice"], want)
        # 都成立：游戏自己的槽 0 顶层既有 0/ 也有 progress.bin，社区存档 0/ 里正是 progress.bin
        slot = self.root / "bis/user/save/0000000000000001/0"
        (slot / "0").mkdir()
        both = self.community("Both", {"0/progress.bin": b"both"})
        with patch.object(saves.save_formats, "detect", return_value=[]):
            preview = saves.inspect(self.context(operation="saveInspect", path=str(both)))
        self.assertEqual(preview["notice"], want)

    def test_decided_structure_has_no_notice(self):
        # 游戏自己的槽 0 顶层是 progress.bin → 0/ 是提交目录，剥掉；有定论就不打扰玩家
        src = self.community("Decided", {"0/progress.bin": b"decided"})
        with patch.object(saves.save_formats, "detect", return_value=[]):
            preview = saves.inspect(self.context(operation="saveInspect", path=str(src)))
        self.assertEqual(preview["notice"], "")
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-one")
        result = saves.restore(self.context(operation="saveRestore", path=str(src), **preview))
        self.assertTrue(result["committed"])
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"decided")

    def test_ryujinx_zip_of_another_game_is_rejected_without_writes(self):
        zpath = self.directory / "other.zip"
        meta = struct.pack("<4Q", 0x0100BBBB00000000, 0, 1, 0) + bytes([1]) + bytes(0x3F)
        with zipfile.ZipFile(zpath, "w") as z:
            z.writestr("bis/user/save/0000000000000009/ExtraData0", meta)
            z.writestr("bis/user/save/0000000000000009/0/progress.bin", b"other")
        with self.assertRaises(Failure) as e:
            saves.inspect(self.context(operation="saveInspect", path=str(zpath)))
        self.assertEqual(e.exception.code, "save_wrong_game")
        self.assertEqual((self.root / PROGRESS).read_bytes(), b"checkpoint-one")

    def test_game_never_played_takes_the_save_on_next_launch(self):
        import shutil

        shutil.rmtree(self.root / "bis")
        src = self.community("raw", {"progress.bin": b"native-save", "sub/more.bin": b"more"})
        preview = saves.inspect(self.context(operation="saveInspect", path=str(src)))
        self.assertEqual(preview["notice"], saves.SEED_NOTICE)
        self.assertEqual((preview["fileCount"], preview["totalBytes"]), (2, 15))
        c = self.context(operation="saveRestore", path=str(src), **preview)
        self.assertTrue(saves.restore(c)["committed"])
        pending = saves.pending_seed(c, GID)
        self.assertEqual((pending / "account/progress.bin").read_bytes(), b"native-save")
        self.assertEqual((pending / "account/sub/more.bin").read_bytes(), b"more")
        self.assertEqual(saves.claim_seed(c, GID), pending)
        log = self.directory / "game.log"
        log.write_text("启动失败，没灌入", encoding="utf-8")
        saves.finish_seed(c, GID, log)
        self.assertEqual(saves.claim_seed(c, GID), pending)   # 没灌成功：留着，下次启动再放
        log.write_text("S2A 带存档起局：account ← x（2 个文件，15 字节）", encoding="utf-8")
        saves.finish_seed(c, GID, log)
        self.assertIsNone(saves.claim_seed(c, GID))

    def test_never_played_game_changed_since_preview_is_refused(self):
        import shutil

        shutil.rmtree(self.root / "bis")
        src = self.community("raw", {"progress.bin": b"one"})
        preview = saves.inspect(self.context(operation="saveInspect", path=str(src)))
        (src / "progress.bin").write_bytes(b"two")
        with self.assertRaises(Failure) as e:
            saves.restore(self.context(operation="saveRestore", path=str(src), **preview))
        self.assertEqual(e.exception.code, "save_changed")

    def test_device_only_game_seeds_the_device_slot(self):
        import shutil, struct

        shutil.rmtree(self.root / "bis")
        c = self.context(operation="saveInspect", path="x")
        nacp = c.library / "games" / GID / "current" / "ctl" / "control.nacp"
        nacp.parent.mkdir(parents=True, exist_ok=True)
        data = bytearray(0x30A0); struct.pack_into("<q", data, 0x3090, 1 << 20); nacp.write_bytes(data)
        src = self.community("raw", {"progress.bin": b"island"})
        preview = saves.inspect(self.context(operation="saveInspect", path=str(src)))
        c = self.context(operation="saveRestore", path=str(src), **preview)
        saves.restore(c)
        self.assertEqual((saves.pending_seed(c, GID) / "device/progress.bin").read_bytes(), b"island")

    def test_own_backup_never_takes_the_seed_path(self):
        import shutil

        backup = self.directory / "b.s2asave"
        saves.backup(self.context(operation="saveBackup", path=str(backup)))
        shutil.rmtree(self.root / "bis")
        c = self.context(operation="saveInspect", path=str(backup))
        self.assertNotEqual(saves.inspect(c).get("notice"), saves.SEED_NOTICE)

    def test_inspect_and_restore_use_the_same_cached_conversion(self):
        src = self.community("Checkpoint", {f"saves/0x{GID} Synthetic/20261020-120000 Player/progress.bin": b"same"})
        first = saves.inspect(self.context(operation="saveInspect", path=str(src)))
        second = saves.inspect(self.context(operation="saveInspect", path=str(src)))
        self.assertEqual(first["archiveSha256"], second["archiveSha256"])
        self.assertEqual(len(list((self.directory / "library/save-imports" / GID).glob("*.s2asave"))), 1)


if __name__ == "__main__":
    unittest.main()
