"""C5：真实共享识别器的合成布局回归，不含游戏或玩家存档。

布局依据 save_formats.py 的现行接口：EdiZon 标记所在层、raw 包装层、
Checkpoint / JKSV 的目录修改时间及 Ryujinx 的 0x40 字节属性。
所有目录、字节、身份与预期均在测试中手写；不宣称真实游戏兼容性。
"""

import os
import stat
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

JOBS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JOBS))
import save_formats  # noqa: E402

TITLE_A = "0100aaaa00000001"
TITLE_B = "0100bbbb00000002"
USER = "0123456789abcdef0011223344556677"


class SaveFormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="s2arm synthetic layouts ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def tree(self, name, files):
        root = self.root / name
        root.mkdir(parents=True, exist_ok=True)
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return root

    def payload(self, directory):
        return {
            str(Path(path).relative_to(directory)): Path(path).read_bytes()
            for path in save_formats.files_under(directory)
        }

    def assert_selection(self, source, expected, files, **options):
        with save_formats.extracted(source, **options) as result:
            self.assertEqual(result[:4], expected)
            self.assertEqual(self.payload(result[1]), files)

    def archive(self, name, files):
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            for relative, content in files.items():
                archive.writestr(relative, content)
        return path

    def test_nx2apple_game_folder(self):
        tid = "0100000000010000"
        root = self.tree(tid, {"save/a.bin": b"a", "save.work/x": b"uncommitted", "save_device/b.bin": b"b"})
        with save_formats.extracted(root, tid=tid) as (layout, data, program, kind, _):
            self.assertEqual((layout, os.path.basename(data), program, kind), ("nx2apple", "save", tid, "account"))
        # 外层文件夹名不是游戏号：程序号用调用方给的
        renamed = self.tree("copied", {"save/a.bin": b"a"})
        with save_formats.extracted(renamed, tid=tid) as (_, _, program, kind, _):
            self.assertEqual((program, kind), (tid, "account"))
        only_device = self.tree("device", {"save_device/b.bin": b"b"})
        with save_formats.extracted(only_device, tid=tid) as (_, data, _, kind, _):
            self.assertEqual((os.path.basename(data), kind), ("save_device", "device"))

    def test_folder_with_other_contents_is_not_nx2apple(self):
        # 游戏自己的存档里恰好有 save 子目录，旁边还有别的：不能当成 NX2Apple
        root = self.tree("native", {"save/a.bin": b"a", "other/b.bin": b"b", "top.bin": b"c"})
        with save_formats.extracted(root, tid="0100000000010000") as (layout, data, _, kind, _):
            self.assertNotEqual(layout, "nx2apple"); self.assertIsNone(kind)

    def test_direct_save_device_folder_says_its_type(self):
        tid = "0100000000010000"
        with save_formats.extracted(self.tree("x/save_device", {"b.bin": b"b"}), tid=tid) as (_, _, _, kind, _):
            self.assertEqual(kind, "device")
        with save_formats.extracted(self.tree("y/save", {"a.bin": b"a"}), tid=tid) as (_, _, _, kind, _):
            self.assertIsNone(kind)

    def test_edizon_marker_locates_named_backup_among_unrelated_siblings(self):
        relative = "export/named backup"
        source = self.tree(
            "EdiZon",
            {
                f"{relative}/edizon_save_metadata.json": b"{}",
                f"{relative}/Slot0": b"synthetic slot",
                f"{relative}/sub/extra.bin": b"synthetic extra",
                "unrelated/newer.txt": b"not part of the backup",
            },
        )
        self.assert_selection(
            source,
            ("raw", str(source / relative), TITLE_A, None),
            {"Slot0": b"synthetic slot", "sub/extra.bin": b"synthetic extra"},
            tid="0X" + TITLE_A.upper(),
            layout="raw",
        )

    def test_edizon_metadata_without_payload_is_rejected(self):
        source = self.tree(
            "metadata only",
            {
                "named backup/edizon_save_metadata.json": b"{}",
                "named backup/.DS_Store": b"synthetic ignored",
                "named backup/._Slot0": b"synthetic ignored",
            },
        )
        with self.assertRaisesRegex(ValueError, "没有存档文件"):
            with save_formats.extracted(source, tid=TITLE_A, layout="raw"):
                self.fail("metadata alone must not produce a save")

    def test_edizon_zip_payload_and_temporary_directory_lifetime(self):
        source = self.archive(
            "edizon.zip",
            {
                "export/backup/edizon_save_metadata.json": b"{}",
                "export/backup/Slot0": b"synthetic zip slot",
                "export/backup/sub/part": b"synthetic zip part",
            },
        )
        before = source.read_bytes()
        with save_formats.extracted(source, tid=TITLE_A, layout="raw") as result:
            self.assertEqual((result[0], result[2], result[3]), ("raw", TITLE_A, None))
            directory = Path(result[1])
            extracted_root = directory.parents[1]
            self.assertTrue(extracted_root.is_dir())
            self.assertEqual(directory.relative_to(extracted_root), Path("export/backup"))
            self.assertEqual(
                self.payload(directory),
                {"Slot0": b"synthetic zip slot", "sub/part": b"synthetic zip part"},
            )
        self.assertFalse(extracted_root.exists())
        self.assertEqual(source.read_bytes(), before)

    def test_zip_temporary_directory_is_removed_when_consumer_raises(self):
        source = self.archive("consumer.zip", {"payload": b"synthetic"})
        with self.assertRaisesRegex(RuntimeError, "synthetic consumer failure"):
            with save_formats.extracted(source, tid=TITLE_A, layout="raw") as result:
                directory = Path(result[1])
                self.assertEqual(self.payload(directory), {"payload": b"synthetic"})
                raise RuntimeError("synthetic consumer failure")
        self.assertFalse(directory.exists())
        self.assertTrue(source.is_file())

    def test_zip_unpacked_size_and_count_are_bounded_before_extraction(self):
        # 审计 P1-47：3 条各 1 MiB 的零字节（压缩后很小），声明总量 3 MiB。上限 2 MiB → 解压前拒、临时目录不留；上限 4 MiB → 照常解出。
        # 条数上限 2 → 3 条也拒。期望按声明大小手算，不跑被测实现
        source = self.root / "bomb.zip"
        with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
            for i in range(3):
                archive.writestr(f"payload/{i}", bytes(1 << 20))
        self.assertLess(source.stat().st_size, 1 << 20)
        before = set(Path(tempfile.gettempdir()).glob("s2a_saveimp_*"))
        for name, value in (("MAX_BYTES", 2 << 20), ("MAX_FILES", 2)):
            with self.subTest(limit=name):
                keep = getattr(save_formats, name); setattr(save_formats, name, value)
                try:
                    with self.assertRaisesRegex(ValueError, "zip 解压后太大"):
                        with save_formats.extracted(source, tid=TITLE_A, layout="raw"):
                            self.fail("over-limit archive must not be extracted")
                finally:
                    setattr(save_formats, name, keep)
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("s2a_saveimp_*")), before)
        keep = save_formats.MAX_BYTES; save_formats.MAX_BYTES = 4 << 20
        try:
            with save_formats.extracted(source, tid=TITLE_A, layout="raw") as result:
                self.assertEqual(len(self.payload(result[1])), 3)
        finally:
            save_formats.MAX_BYTES = keep

    def test_files_are_ordered_per_directory_and_metadata_is_excluded(self):
        source = self.tree(
            "ordered",
            {
                "z/last": b"z",
                "b/nested/part": b"part",
                "b/file": b"b",
                "z.bin": b"last root",
                "a.bin": b"first root",
                ".hidden/payload": b"ignored directory",
                "b/.DS_Store": b"ignored",
                "b/Thumbs.db": b"ignored",
                "desktop.ini": b"ignored",
                "._a.bin": b"ignored",
                "edizon_save_metadata.json": b"{}",
                "b/ExtraData0": b"ignored",
                "ExtraData1": b"ignored",
            },
        )
        self.assertEqual(
            [str(Path(path).relative_to(source)) for path in save_formats.files_under(source)],
            ["a.bin", "z.bin", "b/file", "b/nested/part", "z/last"],
        )
        self.assertEqual(
            self.payload(source),
            {
                "a.bin": b"first root",
                "z.bin": b"last root",
                "b/file": b"b",
                "b/nested/part": b"part",
                "z/last": b"z",
            },
        )

    def test_symlink_entries_are_rejected_even_when_the_name_is_skipped(self):
        target = self.tree("target", {"payload": b"synthetic target"})
        for name, destination in (
            ("linked", target / "payload"),
            (".hidden", target),
            (".DS_Store", target / "payload"),
        ):
            with self.subTest(name=name):
                source = self.tree("link " + name, {"payload": b"synthetic source"})
                (source / name).symlink_to(destination)
                with self.assertRaisesRegex(ValueError, "存档里有链接"):
                    save_formats.files_under(source)
        self.assertEqual((target / "payload").read_bytes(), b"synthetic target")

    def test_source_symlink_is_rejected(self):
        target = self.tree("source target", {"payload": b"synthetic"})
        source = self.root / "source link"
        source.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "来源是链接"):
            with save_formats.extracted(source, tid=TITLE_A):
                self.fail("source links must not be followed")

    def test_zip_rejects_backslash_traversal_absolute_paths_and_links(self):
        for index, member in enumerate(("..\\escape", "/escape", "\\escape", "link")):
            with self.subTest(member=member):
                source = self.root / f"unsafe-{index}.zip"
                info = zipfile.ZipInfo(member)
                if member == "link":
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                with zipfile.ZipFile(source, "w") as archive:
                    archive.writestr(info, b"synthetic")
                with self.assertRaisesRegex(ValueError, "越界路径或链接"):
                    with save_formats.extracted(source, tid=TITLE_A):
                        self.fail("unsafe members must be rejected before extraction")

    def test_missing_path_and_non_zip_file_are_rejected(self):
        plain = self.root / "not-an-archive"
        plain.write_bytes(b"synthetic plain file")
        for source in (self.root / "missing", plain):
            with self.subTest(source=source.name):
                with self.assertRaisesRegex(ValueError, "来源不是目录或 zip"):
                    with save_formats.extracted(source, tid=TITLE_A):
                        self.fail("non-directories require a zip archive")

    def test_unidentified_files_without_program_id_are_rejected(self):
        source = self.tree("unknown", {"payload": b"synthetic"})
        self.assertEqual(save_formats.detect(str(source)), [])
        with self.assertRaisesRegex(ValueError, "须指明程序号"):
            with save_formats.extracted(source):
                self.fail("raw files cannot establish a program identity")

    def test_raw_program_user_and_commit_wrappers_preserve_inner_tree(self):
        relative = f"{TITLE_A.upper()}/{USER}/0"
        source = self.tree(
            "wrapped", {f"{relative}/payload": b"root", f"{relative}/sub/part": b"nested"}
        )
        self.assert_selection(
            source,
            ("raw", str(source / relative), TITLE_A, None),
            {"payload": b"root", "sub/part": b"nested"},
            tid=TITLE_A,
            layout="raw",
            ref={"payload", "sub"},   # 游戏自己的存档顶层是 payload / sub → 0/ 是提交目录
        )

    def test_zero_directory_is_compared_with_the_games_own_save(self):
        # 10-27：唯一子目录 0/ 两种解释，和这款游戏自己写出的存档顶层比（三种手写夹具，结构仿精选包里的三款，内容全是合成字节）
        cases = (
            # DDLC 型：0/ 里是存档文件、游戏自己的存档顶层就是这些文件 → 剥
            ("commit dir, several files", {"0/save_persistent.sav": b"p", "0/save_preferences.sav": b"q"},
             {"save_persistent.sav", "save_preferences.sav", "save_launcher.sav"}, "0", False),
            # 勇敢的心型：0/ 里只有一个槽位文件 → 剥
            ("commit dir, one slot file", {"0/slot-000.sav": b"s"}, {"slot-000.sav"}, "0", False),
            # 塞尔达型：游戏自己的存档顶层是 0/…5/ + option.sav → 0/ 是槽位，不剥
            ("slot dir", {"0/game_data.sav": b"g", "0/caption.sav": b"c"}, {"0", "1", "option.sav"}, "", False),
            # 没有参照：不剥，报结构存疑（不静默剥）
            ("no reference", {"0/game_data.sav": b"g"}, None, "", True),
            # 两种解释都对得上（参照里既有 0/ 又有同名文件）：不剥，报结构存疑
            ("both match", {"0/data.bin": b"d"}, {"0", "data.bin"}, "", True),
        )
        for name, files, ref, picked, doubtful in cases:
            with self.subTest(name=name):
                source = self.tree(name, files)
                for layout in ("raw", "jksv"):
                    with save_formats.extracted(source, tid=TITLE_A, layout=layout, ref=ref) as result:
                        self.assertEqual(result[1], str(source / picked) if picked else str(source))
                        self.assertEqual("结构存疑" in result[4], doubtful)
                        want = {k[len(picked) + 1:] if picked else k: v for k, v in files.items()}
                        self.assertEqual(self.payload(result[1]), want)

    def test_raw_named_or_branching_directories_are_not_flattened(self):
        cases = (
            {"named/payload": b"named"},
            {f"{TITLE_A}/payload": b"a", f"{TITLE_B}/payload": b"b"},
        )
        for index, files in enumerate(cases):
            with self.subTest(index=index):
                source = self.tree(f"preserve-{index}", files)
                self.assert_selection(
                    source, ("raw", str(source), TITLE_A, None), files, tid=TITLE_A, layout="raw"
                )

    def test_raw_root_payload_prevents_wrapper_descent(self):
        files = {"payload": b"root", f"{TITLE_A}/payload": b"nested"}
        source = self.tree("root payload", files)
        self.assert_selection(
            source, ("raw", str(source), TITLE_A, None), files, tid=TITLE_A, layout="raw"
        )

    def test_raw_jksv_wrapper_selects_newest_backup_by_mtime_not_name(self):
        source = self.tree(
            "raw JKSV",
            {
                "JKSV/Synthetic/zzz old/payload": b"old",
                "JKSV/Synthetic/aaa new/payload": b"new",
                "JKSV/Synthetic/.hidden/payload": b"hidden",
            },
        )
        base = source / "JKSV/Synthetic"
        for name, timestamp in (("zzz old", 1000), ("aaa new", 2000), (".hidden", 3000)):
            os.utime(base / name, (timestamp, timestamp))
        self.assert_selection(
            source,
            ("jksv", str(base / "aaa new"), TITLE_A, None),
            {"payload": b"new"},
            tid=TITLE_A,
            layout="raw",
        )

    def test_checkpoint_uses_mtime_and_normalizes_program_id(self):
        folder = "0x" + TITLE_A.upper() + " Synthetic"
        source = self.tree(
            "checkpoint",
            {
                f"{folder}/zzz old/payload": b"old",
                f"{folder}/aaa new/payload": b"new",
                f"{folder}/.hidden/payload": b"hidden",
            },
        )
        base = source / folder
        for name, timestamp in (("zzz old", 1000), ("aaa new", 2000), (".hidden", 3000)):
            os.utime(base / name, (timestamp, timestamp))
        self.assert_selection(
            source, ("checkpoint", str(base / "aaa new"), TITLE_A, None), {"payload": b"new"}
        )

    def test_explicit_checkpoint_without_program_folder_descends_to_payload(self):
        source = self.tree(
            "checkpoint without id", {"export/old/payload": b"old", "export/new/payload": b"new"}
        )
        for name, timestamp in (("old", 1000), ("new", 2000)):
            os.utime(source / "export" / name, (timestamp, timestamp))
        self.assert_selection(
            source,
            ("checkpoint", str(source / "export/new"), TITLE_A, None),
            {"payload": b"new"},
            tid=TITLE_A,
            layout="checkpoint",
        )

    def test_ryujinx_exact_size_attributes_define_identity_and_save_type(self):
        for kind, expected_type in ((1, "account"), (3, "device")):
            with self.subTest(kind=kind):
                metadata = (
                    struct.pack("<4Q", int(TITLE_B, 16), 0x0123456789ABCDEF, 0x0011223344556677, 0)
                    + bytes([kind])
                    + bytes(31)
                )
                # The slot number is deliberately different from the program ID.
                relative = "bis/user/save/0000000000000009/0"
                source = self.tree(
                    f"ryujinx-{kind}",
                    {
                        relative + "/payload": b"synthetic",
                        "bis/user/save/0000000000000009/ExtraData0": metadata,
                    },
                )
                self.assertEqual(len(metadata), 0x40)
                self.assert_selection(
                    source,
                    ("ryujinx", str(source / relative), TITLE_B, expected_type),
                    {"payload": b"synthetic"},
                )
                self.assertEqual(
                    save_formats.detect(str(source))[0][4], f"ExtraData0 用户号 {USER}"
                )

    def test_invalid_ryujinx_attributes_are_not_detected_as_account_saves(self):
        valid = struct.pack("<4Q", int(TITLE_A, 16), 0, 1, 0) + bytes([1]) + bytes(31)
        variants = {
            "truncated": valid[:0x3F],
            "zero program": bytes(8) + valid[8:],
            "system save": valid[:24] + struct.pack("<Q", 1) + valid[32:],
            "unsupported kind": valid[:32] + bytes([2]) + valid[33:],
        }
        for name, metadata in variants.items():
            with self.subTest(name=name):
                source = self.tree(
                    name,
                    {
                        "0000000000000009/0/payload": b"synthetic",
                        "0000000000000009/ExtraData0": metadata,
                    },
                )
                # Only detection is asserted: a caller-supplied raw fallback is a separate contract.
                self.assertEqual(save_formats.detect(str(source)), [])

    def test_yuzu_uppercase_identity_and_account_device_selection(self):
        source = self.tree(
            "yuzu",
            {
                f"nand/user/save/0000000000000000/{USER.upper()}/{TITLE_A.upper()}/payload": b"account",
                f"nand/user/save/0000000000000000/{'0' * 32}/{TITLE_B.upper()}/payload": b"device",
            },
        )
        expected = {(TITLE_A, "account"), (TITLE_B, "device")}
        self.assertEqual({(hit[2], hit[3]) for hit in save_formats.detect(str(source))}, expected)
        for title, kind in sorted(expected):
            with self.subTest(title=title):
                with save_formats.extracted(source, tid="0X" + title.upper()) as result:
                    self.assertEqual((result[0], result[2], result[3]), ("yuzu", title, kind))
                    self.assertEqual(self.payload(result[1]), {"payload": kind.encode()})

    def test_multiple_programs_require_selection(self):
        source = self.tree(
            "multiple titles", {f"0x{TITLE_A}/payload": b"a", f"0x{TITLE_B}/payload": b"b"}
        )
        with self.assertRaisesRegex(ValueError, "认出 2 份存档.*须指明程序号"):
            with save_formats.extracted(source):
                self.fail("multiple recognized titles cannot be selected implicitly")

    def test_same_program_in_two_users_remains_ambiguous_with_program_id(self):
        source = self.tree(
            "two users",
            {
                f"0000000000000000/{user}/{TITLE_A}/payload": b"synthetic"
                for user in ("1" * 32, "2" * 32)
            },
        )
        with self.assertRaisesRegex(ValueError, "认出 2 份存档"):
            with save_formats.extracted(source, tid=TITLE_A):
                self.fail("a program ID cannot distinguish two user saves")

    def test_recognized_empty_checkpoint_is_rejected(self):
        source = self.tree("empty checkpoint", {f"0x{TITLE_A}/.DS_Store": b"ignored"})
        with self.assertRaisesRegex(ValueError, "没有存档文件"):
            with save_formats.extracted(source):
                self.fail("recognition alone does not establish a nonempty save")


if __name__ == "__main__":
    unittest.main()
