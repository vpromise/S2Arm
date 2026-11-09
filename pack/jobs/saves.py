"""Stopped-game save backup/restore. Format and recovery rules: docs/存档备份_G05.md.

Never uses S2A_SAVE_DIR (the destructive test-seed interface). This module only
operates on the launcher's per-game Ryujinx root under library/saves/<gameId>.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import struct
import unicodedata
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from core import Failure, atomic, contained
from library import input_identity, selected_packages
import save_formats

OPERATIONS = {"saveStatus", "saveBackup", "saveInspect", "saveRestore"}
TREES = ("bis/user/save", "bis/system/save")
PROFILE = "system/Profiles.json"
SAVE_INDEX = "bis/system/save/8000000000000000/0/imkvdb.arc"
MAX_FILES = save_formats.MAX_FILES
MAX_BYTES = save_formats.MAX_BYTES
MAX_MANIFEST = 8 << 20
CHUNK = 1 << 20
# 社区存档转换后目录结构存疑时给玩家看的一句（10-27 塞尔达 0/…5/）：inspect 预览里显示，restore 不因此拒绝
STRUCTURE_NOTICE = "这份存档的目录结构不常见，导入时会按原样照搬。如果游戏读不到进度，请换一份存档。"


def report(c, message, code, severity="error"):
    cancelled = c.cancelled
    c.cancelled = False  # A rollback failure must still have a terminal diagnostic.
    try:
        c.diagnostic(message, code="save_" + code, severity=severity)
    finally:
        c.cancelled = cancelled


def fail(c, message, code):
    report(c, message, code)
    raise Failure(message, code="save_" + code)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_parents(path, root):
    while True:
        fsync_dir(path)
        if path == root:
            return
        path = path.parent


def path_name(name):
    if (
        not isinstance(name, str)
        or not name
        or len(name.encode("utf-8")) > 1024
        or "\\" in name
        or ":" in name
        or any(ord(ch) < 32 for ch in name)
        or name != unicodedata.normalize("NFC", name)
    ):
        raise ValueError("invalid save path")
    parts = name.split("/")
    if len(parts) > 32 or any(p in ("", ".", "..") or p.endswith((" ", ".")) for p in parts):
        raise ValueError("invalid save path")
    return PurePosixPath(name)


def allowed(name, directory=False):
    return (not directory and name == PROFILE) or any(
        (directory and name == t) or name.startswith(t + "/") for t in TREES
    )


def safe_path(root, path):
    """Reject links in every existing component before containment resolution."""
    relative = path.relative_to(root)
    current = root
    if current.is_symlink():
        raise ValueError("save root is a link")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("save path is a link")
    return contained(root, path)


def locations(c, game):
    gid = game.get("id", "")
    if not re.fullmatch(r"[0-9a-fA-F]{16}", gid):
        fail(c, "游戏标识无效，无法管理存档。", "game_id")
    try:
        root = safe_path(c.library, c.library / "saves" / gid)
        transactions = safe_path(c.library, c.library / "save-transactions")
        backups = safe_path(c.library, c.library / "save-backups" / gid)
    except ValueError:
        fail(c, "存档目录包含链接，已停止操作。", "unsafe_path")
    return root, transactions, backups


def scan(c, root):
    """Stable, regular files only; no traversal of links or special files."""
    files, dirs, total, names = {}, [], 0, set()
    if not root.exists():
        return files, dirs
    if root.is_symlink() or not root.is_dir():
        raise ValueError("invalid save directory")
    for parent, children, leaves in os.walk(root, followlinks=False):
        for name in sorted(children + leaves):
            c.check()
            p = Path(parent) / name
            rel = p.relative_to(root).as_posix()
            path_name(rel)
            folded = rel.casefold()
            if folded in names:
                raise ValueError("case-colliding save paths")
            names.add(folded)
            st = p.lstat()
            if stat.S_ISDIR(st.st_mode):
                dirs.append(rel)
                if len(dirs) > MAX_FILES:
                    raise ValueError("save directory limit exceeded")
            elif stat.S_ISREG(st.st_mode):
                total += st.st_size
                if len(files) >= MAX_FILES or total > MAX_BYTES:
                    raise ValueError("save data limit exceeded")
                files[rel] = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            else:
                raise ValueError("links and special files are unsupported")
    return dict(sorted(files.items())), sorted(dirs)


def file_chunks(c, path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        before = os.fstat(f.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("not a regular save file")
        while chunk := f.read(CHUNK):
            c.check()
            yield chunk
        after = os.fstat(f.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("save file changed during copy")


def hash_file(c, path):
    h = hashlib.sha256()
    for chunk in file_chunks(c, path):
        h.update(chunk)
    return h.hexdigest()


def fingerprint(c, root):
    files, dirs = scan(c, root)
    items = [(name, info[2], hash_file(c, root / name)) for name, info in files.items()]
    return hashlib.sha256(
        json.dumps([root.exists(), dirs, items], ensure_ascii=False).encode()
    ).hexdigest()


def profile_names(data):
    """Profiles supply labels only; last_opened is not save ownership."""
    if not isinstance(data, dict):
        raise ValueError("invalid user metadata")
    profiles = data.get("profiles")
    if not isinstance(profiles, list) or len(profiles) > 100:
        raise ValueError("missing user profiles")
    names = {}
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ValueError("invalid user profile")
        uid, name = profile.get("user_id"), profile.get("name", "")
        if (
            not isinstance(uid, str)
            or not re.fullmatch(r"[0-9a-fA-F]{32}", uid)
            or int(uid, 16) == 0
            or uid.lower() in names
            or not isinstance(name, str)
        ):
            raise ValueError("invalid user identities")
        names[uid.lower()] = name
    return names


def save_entries(read, files, directories):
    """Read the committed LibHac 0.19 IMKV index, never its working /1 copy.

    Layout source and byte order are documented in docs/存档备份_G05.md.
    Unknown/truncated indices fail closed; no profile-based fallback.
    """
    if SAVE_INDEX not in files:
        raise ValueError("missing committed save index")
    data = read(SAVE_INDEX)
    if len(data) < 12 or len(data) > MAX_MANIFEST:
        raise ValueError("invalid save index size")
    magic, reserved, count = struct.unpack_from("<4sII", data)
    if magic != b"IMKV" or reserved or count > 0x1080:
        raise ValueError("unsupported save index")
    offset, ids, attributes, result = 12, set(), set(), []
    for _ in range(count):
        if offset + 12 + 0x80 > len(data):
            raise ValueError("truncated save index")
        magic, key_size, value_size = struct.unpack_from("<4sII", data, offset)
        if (magic, key_size, value_size) != (b"IMEN", 0x40, 0x40):
            raise ValueError("unsupported save index entry")
        offset += 12
        key, value = data[offset : offset + 0x40], data[offset + 0x40 : offset + 0x80]
        offset += 0x80
        program, high, low, static_id = struct.unpack_from("<4Q", key)
        kind, rank, slot = struct.unpack_from("<BBH", key, 0x20)
        save_id = struct.unpack_from("<Q", value)[0]
        space, state = value[0x18:0x1A]
        if key in attributes or save_id in ids or not save_id:
            raise ValueError("duplicate or invalid save index entry")
        attributes.add(key)
        ids.add(save_id)
        if kind not in (1, 3) or space != 1:
            continue
        if (
            not program
            or static_id
            or rank
            or slot
            or state
            or (kind == 1 and not (high or low))
            or (kind == 3 and (high or low))
        ):
            raise ValueError("unsupported account/device save state")
        sid = f"{save_id:016x}"
        if f"bis/user/save/{sid}/0" not in directories:
            raise ValueError("missing committed save directory")
        result.append(
            dict(
                saveId=sid,
                programId=f"{program:016x}",
                type="account" if kind == 1 else "device",
                userId=f"{high:016x}{low:016x}",
            )
        )
    if offset != len(data):
        raise ValueError("trailing save index data")
    present = {n.split("/")[3] for n in (*files, *directories) if n.startswith("bis/user/save/")}
    if any(not re.fullmatch(r"[0-9a-f]{16}", sid) or int(sid, 16) not in ids for sid in present):
        raise ValueError("unindexed save directory")
    if not result:
        raise ValueError("no indexed account/device saves")
    return sorted(result, key=lambda entry: entry["saveId"])


def save_users(entries):
    ids = sorted({entry["userId"] for entry in entries if entry["type"] == "account"})
    # Retain the v1 UI shape. active is a display representative, not last_opened.
    return dict(ids=ids, active=ids[0] if ids else "")


def ownership(entries):
    groups = {}
    for entry in entries:
        groups.setdefault((entry["programId"], entry["type"]), set()).add(entry["userId"])
    return groups


def json_object(data):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError("duplicate JSON field")
            result[k] = v
        return result

    return json.loads(data, object_pairs_hook=unique)


def identity(game):
    packages = selected_packages(game)
    versions = [p["version"] for p in packages if p["type"] in ("base", "update")]
    return dict(
        id=game["id"],
        name=game.get("name", ""),
        version=max(versions, default=0),
        contentIdentity=input_identity(game),
    )


def save_manifest(c, game, root):
    files, dirs = scan(c, root)
    selected = {n: info for n, info in files.items() if allowed(n)}
    if not any(n.startswith("bis/user/save/") for n in selected):
        fail(c, "此游戏尚无可备份的存档，请先在游戏中保存。", "empty")
    profile = root / PROFILE
    if PROFILE not in selected or profile.stat().st_size > MAX_MANIFEST:
        fail(c, "存档用户资料缺失或无效，已停止操作。", "user")
    profile_names(json_object(profile.read_bytes()))
    if SAVE_INDEX not in selected or selected[SAVE_INDEX][2] > MAX_MANIFEST:
        raise ValueError("missing or oversized save index")
    entries = save_entries(lambda name: (root / name).read_bytes(), selected, dirs)
    manifest = dict(
        format="S2ArmSave",
        schema=1,
        layout="ryujinx-data-v1",
        platform="macos",
        createdAt=datetime.datetime.now(datetime.UTC).isoformat(),
        game=identity(game),
        users=save_users(entries),
        saveEntries=entries,
        deviceSettings={},
        directories=[d for d in dirs if allowed(d, directory=True)],
        files=[],
    )
    return manifest, selected


def write_archive(c, game, root, destination):
    manifest, selected = save_manifest(c, game, root)
    # Publish exclusively: never replace an existing backup, even after a race.
    if destination.exists() or destination.is_symlink():
        fail(c, "同名备份已存在，请选择新的文件名。", "exists")
    total = sum(v[2] for v in selected.values())
    if shutil.disk_usage(destination.parent).free < total + (64 << 20):
        fail(c, "备份目标磁盘空间不足，请清理空间后重试。", "space")
    temporary = destination.with_name("." + destination.name + "." + uuid.uuid4().hex + ".partial")
    done = 0
    try:
        with temporary.open("xb") as f:
            os.chmod(temporary, 0o600)
            with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as z:
                for name, info in selected.items():
                    h = hashlib.sha256()
                    with z.open("payload/" + name, "w", force_zip64=True) as target:
                        for chunk in file_chunks(c, root / name):
                            target.write(chunk)
                            h.update(chunk)
                            done += len(chunk)
                            c.progress(done, total)
                    manifest["files"].append(dict(path=name, size=info[2], sha256=h.hexdigest()))
                z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        _, after = save_manifest(c, game, root)
        if after != selected:
            raise ValueError("save files changed during backup")
        with open_archive(c, temporary) as (_, checked):
            if checked != manifest:
                raise ValueError("backup validation failed")
        c.check()
        os.link(temporary, destination)
        fsync_dir(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return manifest


@contextlib.contextmanager
def bounded_zip(source):
    """打开 zip 前先核中央目录的大小、条目数、ZIP64 记录（不让 ZipFile 先把超大目录读进内存）；之后才交 ZipFile。"""
    if source.is_symlink() or not source.is_file():
        raise ValueError("not a regular backup")
    with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), "rb") as f:
        # Bound the central directory before ZipFile loads it into memory.
        size = os.fstat(f.fileno()).st_size
        if size > MAX_BYTES + (64 << 20):
            raise ValueError("oversized archive")
        f.seek(max(0, size - 65557))
        tail = f.read()
        end = tail.rfind(b"PK\x05\x06")
        if end < 0 or len(tail) - end < 22:
            raise ValueError("missing ZIP directory")
        _, disk, central_disk, on_disk, count, directory_size, offset, comment_size = struct.unpack_from(
            "<4s4H2IH", tail, end
        )
        if (
            disk
            or central_disk
            or len(tail) - end != 22 + comment_size
        ):
            raise ValueError("unsafe ZIP directory")
        end_offset = size - len(tail) + end
        directory_end = end_offset
        if end_offset >= 20:
            f.seek(end_offset - 20)
            locator = f.read(20)
            if locator.startswith(b"PK\x06\x07"):
                original = (on_disk, count, directory_size, offset)
                _, disk, record_offset, disks = struct.unpack("<4sIQI", locator)
                # Accept the standard adjacent record emitted by ZipFile, with
                # no prepended data or extensible record. ZIP64 overrides the
                # bounded 32-bit EOCD fields, so check it before opening ZIP.
                if disk or disks != 1 or record_offset != end_offset - 76 or record_offset < 0:
                    raise ValueError("unsafe ZIP64 locator")
                f.seek(record_offset)
                record = struct.unpack("<4sQ2H2I4Q", f.read(56))
                sig, length, _, _, disk, central_disk, on_disk, count, directory_size, offset = (
                    record
                )
                if (
                    sig != b"PK\x06\x06"
                    or length != 44
                    or disk
                    or central_disk
                    or on_disk != count
                    or count > MAX_FILES + 1
                    or directory_size > 32 << 20
                    or offset + directory_size != record_offset
                    or any(old not in (sentinel, actual) for old, sentinel, actual in zip(
                        original, (0xffff, 0xffff, 0xffffffff, 0xffffffff),
                        (on_disk, count, directory_size, offset)
                    ))
                ):
                    raise ValueError("unsafe ZIP64 directory")
                directory_end = record_offset
        if (on_disk != count or count > MAX_FILES + 1 or directory_size > 32 << 20
                or offset + directory_size != directory_end):
            raise ValueError("unsafe ZIP directory bounds")
        f.seek(0)
        with zipfile.ZipFile(f) as z:
            if len(z.infolist()) != count:
                raise ValueError("ZIP entry count mismatch")
            yield z


@contextlib.contextmanager
def open_archive(c, source):
    with bounded_zip(source) as z:
        with validate_archive(c, z) as manifest:
            yield z, manifest


@contextlib.contextmanager
def validate_archive(c, z):
    entries = z.infolist()
    if len(entries) > MAX_FILES + 1:
        raise ValueError("too many backup entries")
    names = set()
    for item in entries:
        path_name(item.filename)
        folded = item.filename.casefold()
        mode = stat.S_IFMT(item.external_attr >> 16)
        if (
            folded in names
            or item.flag_bits & 1
            or mode not in (0, stat.S_IFREG)
            or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
        ):
            raise ValueError("unsafe ZIP entry")
        names.add(folded)
    meta = z.getinfo("manifest.json")
    if meta.file_size > MAX_MANIFEST:
        raise ValueError("oversized manifest")
    manifest = json_object(z.read(meta))
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != "S2ArmSave"
        or type(manifest.get("schema")) is not int
        or manifest["schema"] != 1
        or manifest.get("layout") != "ryujinx-data-v1"
    ):
        raise ValueError("unsupported save format")
    files, dirs = manifest.get("files"), manifest.get("directories")
    if (
        not isinstance(files, list)
        or not files
        or len(files) > MAX_FILES
        or not isinstance(dirs, list)
        or len(dirs) > MAX_FILES
    ):
        raise ValueError("invalid file list")
    paths, total, expected = set(), 0, {"manifest.json"}
    for name in dirs:
        path_name(name)
        if not allowed(name, directory=True) or name.casefold() in paths:
            raise ValueError("invalid directory list")
        paths.add(name.casefold())
    file_names = set()
    for item in files:
        name, size, sha = item["path"], item["size"], item["sha256"]
        path_name(name)
        if (
            not allowed(name)
            or name.casefold() in paths
            or type(size) is not int
            or size < 0
            or not isinstance(sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha)
        ):
            raise ValueError("invalid file manifest")
        paths.add(name.casefold())
        file_names.add(name.casefold())
        expected.add("payload/" + name)
        total += size
        if total > MAX_BYTES or z.getinfo("payload/" + name).file_size != size:
            raise ValueError("invalid backup size")
    for name in paths:
        if any(
            parent.as_posix().casefold() in file_names
            for parent in PurePosixPath(name).parents
            if parent.as_posix() != "."
        ):
            raise ValueError("file/directory collision")
    if (
        set(z.namelist()) != expected
        or PROFILE.casefold() not in file_names
        or not any(n.startswith("bis/user/save/") for n in file_names)
    ):
        raise ValueError("unexpected or missing backup entries")
    for item in files:
        h, count = hashlib.sha256(), 0
        with z.open("payload/" + item["path"]) as stream:
            while chunk := stream.read(CHUNK):
                c.check()
                count += len(chunk)
                if count > item["size"]:
                    raise ValueError("oversized backup entry")
                h.update(chunk)
        if count != item["size"] or h.hexdigest() != item["sha256"]:
            raise ValueError("backup checksum mismatch")
    if z.getinfo("payload/" + PROFILE).file_size > MAX_MANIFEST:
        raise ValueError("oversized user profile")
    profile_names(json_object(z.read("payload/" + PROFILE)))
    if (
        SAVE_INDEX not in {item["path"] for item in files}
        or z.getinfo("payload/" + SAVE_INDEX).file_size > MAX_MANIFEST
    ):
        raise ValueError("missing or oversized save index")
    indexed = save_entries(
        lambda name: z.read("payload/" + name), {item["path"] for item in files}, dirs
    )
    if "saveEntries" in manifest:
        if manifest["saveEntries"] != indexed or manifest.get("users") != save_users(indexed):
            raise ValueError("save ownership metadata mismatch")
    settings = manifest.get("deviceSettings", {})
    if not isinstance(settings, dict) or any(
        key not in ("system_time", "language", "region", "timezone")
        or not isinstance(value, str)
        or len(value) > 256
        for key, value in settings.items()
    ):
        raise ValueError("invalid device settings")
    # Older v1 manifests lack these optional fields. Derive ownership from their
    # existing payload too; never trust their profile-based users/active fields.
    manifest = manifest | dict(
        saveEntries=indexed, users=save_users(indexed), deviceSettings=settings
    )
    yield manifest


def journal_paths(c, game, record):
    root, transactions, backups = locations(c, game)
    token = record.get("transaction", "")
    if (
        record.get("schema") != 1
        or record.get("gameId") != game["id"]
        or not re.fullmatch(r"[0-9a-f]{32}", token)
        or type(record.get("hadOriginal")) is not bool
        or record.get("phase") not in ("prepared", "committed")
    ):
        raise ValueError("invalid recovery record")
    transaction = safe_path(c.library, transactions / token)
    previous = safe_path(c.library, backups / token)
    owner = safe_path(c.library, transaction / "owner.json")
    if json_object(owner.read_bytes()) != dict(schema=1, gameId=game["id"], transaction=token):
        raise ValueError("transaction owner mismatch")
    return root, transaction, previous


class RecoveryFailed(Exception):
    """自动回退失败：事务记录与原副本原样留着，下次再试（与 iPhone ios/save_gate.h 同一条规则）。"""

    def __init__(self, journal, transaction):
        super().__init__(str(journal))
        self.journal, self.transaction = journal, transaction


def recovery_message(error):
    # 与 iPhone 存档页同一口径：说清记录在哪、可以先备份、单独确认后仍可启动、记录不删（审计 18:00 P0-3）
    return (
        f"存档恢复事务尚未完成，自动回退失败。事务记录在 {error.journal}"
        + (f"，上次的数据副本在 {error.transaction}" if error.transaction else "")
        + "。可以先「备份存档」把当前存档拷走；确认可能丢掉这次没做完的导入 / 恢复后，可选择「仍要启动」。"
        "记录不会删除，下次启动会再试恢复。"
    )


def recover_once(c, game):
    """Caller MUST hold the library execution lock, including build.launch.
    返回 True = 回退了一次没做完的事务；False = 没有事务或已提交；失败抛 RecoveryFailed（不删记录）。"""
    root, transactions, _ = locations(c, game)
    journal = safe_path(c.library, transactions / (game["id"] + ".json"))
    if not journal.exists():
        return False
    seen = ""   # 认出的事务目录（失败时写进说明）
    try:
        if journal.stat().st_size > MAX_MANIFEST:
            raise ValueError("invalid recovery record")
        record = json_object(journal.read_bytes())
        root, transaction, previous = journal_paths(c, game, record)
        seen = str(transaction)
        old, new = previous / "data", transaction / "new"
        safe_path(c.library, old)
        safe_path(c.library, new)
        if old.exists() and not old.is_dir():
            raise ValueError("invalid original save copy")
        if record["phase"] == "committed":
            if not root.is_dir():
                raise ValueError("committed save root missing")
        elif old.exists():
            if root.exists():
                if new.exists():
                    raise ValueError("ambiguous recovery state")
                os.rename(root, new)
            root.parent.mkdir(parents=True, exist_ok=True)
            os.rename(old, root)
            fsync_dir(root.parent)
            fsync_dir(previous)
            fsync_dir(transaction)
        elif record["hadOriginal"]:
            if not root.is_dir() or not new.is_dir():
                raise ValueError("original save copy is missing")
        elif root.exists():
            if new.exists():
                raise ValueError("unexpected save root appeared")
            os.rename(root, new)
            fsync_dir(root.parent)
        journal.unlink()
        fsync_dir(transactions)
        if transaction.exists():
            shutil.rmtree(transaction)
        return record["phase"] != "committed"
    except (OSError, ValueError, KeyError, TypeError):
        raise RecoveryFailed(journal, seen) from None


def recover(c, game, override=False):
    """启动 / 恢复前调：回退失败时 override = 玩家单独确认过「仍要启动」→ 记一条警告、记录留着、放行；否则报失败（code save_recovery）。"""
    try:
        return recover_once(c, game)
    except RecoveryFailed as error:
        if not override:
            fail(c, recovery_message(error), "recovery")
        report(c, recovery_message(error) + "（玩家已确认仍要启动）", "recovery_overridden", "warning")
        return False


@contextlib.contextmanager
def stopped(c, tolerate=False):
    """tolerate = 只读或把存档拷出去的操作（检查 / 备份 / 校验备份）：回退失败也照做，第三项给出失败的事务记录路径；
    恢复存档不 tolerate（不在说不清的状态上再覆盖）。"""
    with c.lock("execution"), c.lock():
        game = c.game()
        recovered, failed = False, ""
        try:
            recovered = recover_once(c, game)
        except RecoveryFailed as error:
            if not tolerate:
                fail(c, recovery_message(error), "recovery")
            report(c, recovery_message(error), "recovery", "warning")
            failed = str(error.journal)
        yield game, recovered, failed


def status(c):
    c.begin("检查存档状态")
    with stopped(c, tolerate=True) as (game, recovered, failed):
        root, _, backups = locations(c, game)
        files, _ = scan(c, root)
        if any(n.startswith("bis/user/save/") for n in files):
            manifest, files = save_manifest(c, game, root)
            result = dict(
                hasSave=True,
                users=manifest["users"],
                fileCount=len(files),
                totalBytes=sum(v[2] for v in files.values()),
            )
        else:
            result = dict(hasSave=False, fileCount=0, totalBytes=0)
        copies = (
            sorted(
                backups.glob("*/before-restore.s2asave"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if backups.is_dir()
            else []
        )
        return result | dict(recovered=recovered, previousBackup=str(copies[0]) if copies else "", recoveryFailed=failed)


def backup(c):
    c.begin("备份游戏存档")
    with stopped(c, tolerate=True) as (game, _, _):
        root, _, _ = locations(c, game)
        destination = Path(c.r["path"]).expanduser()
        destination = destination.parent.resolve() / destination.name
        if (
            not destination.parent.is_dir()
            or destination == c.library
            or c.library in destination.parents
            or destination == c.private
            or c.private in destination.parents
        ):
            fail(c, "请选择游戏库和应用数据目录以外的备份位置。", "destination")
        try:
            manifest = write_archive(c, game, root, destination)
        except (ValueError, OSError, zipfile.BadZipFile, KeyError, TypeError):
            fail(c, "备份失败，原存档未修改。请检查目录、权限和存档完整性。", "backup")
        return dict(outputPath=str(destination), fileCount=len(manifest["files"]))


def own_backup(source):
    """我方备份走原流程（含全部安全校验）：.s2asave 一律算；别的 zip 先过 bounded_zip 边界检查、再看有没有我方 manifest。
    目录与没有我方 manifest 的 zip 按社区存档布局转换。边界检查不过照样抛错（不降级成社区存档绕过安全校验）。"""
    if source.suffix.lower() == ".s2asave" or source.is_symlink():
        return True
    if source.is_dir():
        return False
    if not zipfile.is_zipfile(source):
        return True   # 既不是目录也不是 zip：交原流程报「格式不支持」
    with bounded_zip(source) as z:
        if "manifest.json" not in z.namelist():
            return False
        if z.getinfo("manifest.json").file_size > MAX_MANIFEST:
            return True   # 超大 manifest 不读进内存（审计 P1-47）：交原流程按 MAX_MANIFEST 拒绝
        try:
            return json.loads(z.read("manifest.json")).get("format") == "S2ArmSave"
        except ValueError:
            return True   # 坏 manifest：交原流程拒绝


def source_digest(c, source):
    if source.is_file():
        return hash_file(c, source)
    h = hashlib.sha256()
    for f in save_formats.files_under(str(source)):
        h.update(os.path.relpath(f, source).encode() + b"\0" + hash_file(c, Path(f)).encode() + b"\n")
    return h.hexdigest()


def import_notice_path(archive):
    """缓存旁的提示小文件：命中缓存时不重跑识别，靠它把当时的说明还给界面。"""
    return archive.with_name(archive.stem + ".notice.json")


def read_import_notice(path):
    """取回缓存时的说明；没有小文件 / 损坏 / 是链接都返回 None（调用方据此补算一次）。"""
    if path.is_symlink():
        return None
    try:
        record = json_object(path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema") != 1:
        return None
    notice = record.get("notice")
    return notice if isinstance(notice, str) else None


def save_top(read, files, dirs, game):
    """这款游戏自己写出的存档顶层（同程序号条目的槽 0 里第一层名字），给 save_formats 判唯一子目录 0/ 剥不剥。"""
    names = set()
    if SAVE_INDEX in files:
        for e in save_entries(read, files, dirs):
            if e["programId"] != game["id"].lower():
                continue
            pre = "bis/user/save/%s/0/" % e["saveId"]
            for name in (*files, *dirs):
                if name.startswith(pre):
                    names.add(name.removeprefix(pre).split("/")[0])
    return {
        x
        for x in names
        if not x.startswith(".") and x not in save_formats.SKIP and x not in save_formats.RYU_META
    }


def import_notice(c, game, root, source):
    """只跑识别、不写归档：取这个来源该不该提示「目录结构存疑」。早于提示小文件的缓存补算这一句用。
    补算失败只少一句提示，绝不挡导入。"""
    try:
        files, dirs = scan(c, root) if root.exists() else ({}, set())

        def read(name):
            return (root / name).read_bytes()

        with save_formats.extracted(
            source, tid=game["id"], ref=save_top(read, files, dirs, game)
        ) as hit:
            return STRUCTURE_NOTICE if save_formats.DOUBT in hit[4] else ""
    except (ValueError, OSError, zipfile.BadZipFile):
        return ""


def external_archive(c, game, root, source):
    """社区存档（JKSV / Checkpoint / yuzu / Ryujinx / 裸文件）→ 本游戏的 .s2asave。

    只替换本游戏已有存档条目（同程序号、同类型）提交槽 0 / 1 里的文件，其余存档树、索引、用户资料原样；
    转换结果按「来源内容 + 当前存档指纹」缓存在 save-imports/<游戏>/，检查与恢复用同一份（原流程比对归档 sha）。
    返回 (归档, 提示)；提示是给玩家看的一句（目录结构存疑时为 STRUCTURE_NOTICE，否则空串），
    与归档同时写进缓存旁的小文件，缓存命中时不必重跑识别也能取回。
    """
    try:
        key = hashlib.sha256((source_digest(c, source) + fingerprint(c, root)).encode()).hexdigest()[:32]
        cache = safe_path(c.library, c.library / "save-imports" / game["id"])
    except ValueError:
        fail(c, "存档目录包含链接，已停止操作。", "unsafe_path")
    destination = cache / (key + ".s2asave")
    notice_path = import_notice_path(destination)
    if destination.is_file():
        notice = read_import_notice(notice_path)
        if notice is None:
            # 这次改动之前留下的缓存（或小文件坏了）：只补算这一句，不重转归档
            notice = import_notice(c, game, root, source)
            with contextlib.suppress(OSError):
                atomic(notice_path, dict(schema=1, notice=notice))
        return destination, notice
    notice = ""
    try:
        files, dirs = scan(c, root) if root.exists() else ({}, set())

        def read(name):
            return (root / name).read_bytes()

        # 这款游戏自己写出的存档顶层（槽 0）：社区存档唯一子目录「0」是提交目录还是游戏的槽位，靠它判（10-27 塞尔达 0/…5/）
        with save_formats.extracted(
            source, tid=game["id"], ref=save_top(read, files, dirs, game)
        ) as (layout, data, program, kind, why):
            if program != game["id"].lower():
                fail(c, "这份存档属于另一款游戏（程序号 %s），已拒绝导入。" % program, "wrong_game")
            # 识别的说明（唯一子目录 0/ 比不出剥不剥时 zero_layer 报 DOUBT）只提示、不拒绝；
            # 布局命中自带的其他说明（如「ExtraData0 用户号 …」）不是存疑，按 DOUBT 的字样区分
            if save_formats.DOUBT in why:
                notice = STRUCTURE_NOTICE
            if SAVE_INDEX not in files:
                fail(c, "这款游戏还没有存档位置。请先运行一次游戏并存档，再导入。", "no_target")
            entries = [
                e
                for e in save_entries(read, files, dirs)
                if e["programId"] == game["id"].lower() and (kind is None or e["type"] == kind)
            ]
            if kind is None and len(entries) > 1:   # 同时有账户存档和设备存档的游戏：没说明类型的来源按账户存档
                entries = [e for e in entries if e["type"] == "account"] or entries
            if len(entries) != 1:
                fail(
                    c,
                    "这款游戏还没有对应的存档位置。请先运行一次游戏并存档，再导入。"
                    if not entries
                    else "这款游戏有多个用户的存档，暂不支持选择导入哪一个。",
                    "no_target",
                )
            sid = entries[0]["saveId"]
            cache.mkdir(parents=True, exist_ok=True, mode=0o700)
            stage = Path(tempfile.mkdtemp(prefix=".import.", dir=cache))
            try:
                tree = stage / "root"
                copy_tree(c, root, tree)
                incoming = save_formats.files_under(data)
                for slot in ("0", "1"):
                    target = tree / "bis/user/save" / sid / slot
                    if slot == "1" and not target.exists():
                        continue
                    shutil.rmtree(target, ignore_errors=True)
                    target.mkdir(parents=True)
                    for f in incoming:
                        out = target / os.path.relpath(f, data)
                        out.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(f, out)
                write_archive(c, game, tree, destination)
            finally:
                shutil.rmtree(stage, ignore_errors=True)
    except ValueError as e:
        fail(c, "认不出这份存档的格式：%s" % e, "invalid")
    # 提示与归档一起落盘：下次命中缓存时不必重跑识别；写不进去就只丢提示，不挡导入
    try:
        atomic(notice_path, dict(schema=1, notice=notice))
    except OSError:
        pass
    return destination, notice


def checked_archive(c, game, root):
    """校验来源（我方备份或社区存档）→ (归档路径, 清单, 归档 sha, 提示)；提示只给界面显示，不参与任何判定。"""
    source = Path(c.r["path"]).expanduser()
    notice = ""
    try:
        external = (source.exists() or source.is_symlink()) and not own_backup(source)
    except (ValueError, OSError, zipfile.BadZipFile):
        fail(c, "备份损坏、格式不支持或包含不安全路径，已拒绝恢复。", "invalid")
    if external:
        source, notice = external_archive(c, game, root, source)
    try:
        if source.stat().st_size > MAX_BYTES + (64 << 20) or root in source.resolve().parents:
            raise ValueError("invalid backup location or size")
        before = hash_file(c, source)
        with open_archive(c, source) as (_, manifest):
            wanted = identity(game)
            if manifest["game"]["id"] != wanted["id"]:
                fail(c, "备份属于另一款游戏，已拒绝恢复。", "wrong_game")
            if (
                manifest["game"].get("version") != wanted["version"]
                or manifest["game"].get("contentIdentity") != wanted["contentIdentity"]
            ):
                fail(c, "备份与当前游戏版本或更新、DLC 组合不符，已拒绝恢复。", "version")
            if root.exists():
                files, dirs = scan(c, root)
                if any(n.startswith("bis/user/save/") for n in files):
                    if SAVE_INDEX not in files or files[SAVE_INDEX][2] > MAX_MANIFEST:
                        raise ValueError("missing or oversized target index")
                    target = ownership(
                        save_entries(lambda name: (root / name).read_bytes(), files, dirs)
                    )
                    incoming = ownership(manifest["saveEntries"])
                    if any(target[key] != incoming[key] for key in target.keys() & incoming.keys()):
                        fail(c, "备份用户与当前存档用户不符，已拒绝恢复。", "wrong_user")
        if before != hash_file(c, source):
            fail(c, "备份已变化，请重新检查。", "changed")
        return source, manifest, before, notice
    except (ValueError, OSError, zipfile.BadZipFile, KeyError, TypeError, RuntimeError):
        fail(c, "备份损坏、格式不支持或包含不安全路径，已拒绝恢复。", "invalid")


SEED_NOTICE = "这款游戏在这台电脑上还没玩过：存档会在下次启动游戏时放进去。"
SEED_DONE_LINE = "S2A 带存档起局："


def pending_seed(c, gid):
    """没玩过的游戏没有存档位置：社区存档先放这里，下次启动时由运行时建好存档再灌入（S2A_SAVE_DIR，同内部带存档起局）"""
    return safe_path(c.library, c.library / "save-imports" / gid / "pending-seed")


def has_save_target(c, game, root):
    if not root.exists():
        return False
    files, dirs = scan(c, root)
    if SAVE_INDEX not in files:
        return False
    return any(e["programId"] == game["id"].lower() for e in save_entries(lambda name: (root / name).read_bytes(), files, dirs))


def seed_source(c, game, root):
    """这次该走「下次启动时放进去」的来源；不是（有存档位置，或来源是我方备份）返回 None"""
    source = Path(c.r["path"]).expanduser()
    try:
        if not source.exists() or own_backup(source) or has_save_target(c, game, root):
            return None
    except (ValueError, OSError, zipfile.BadZipFile):
        return None
    return source


def seed_kind(c, game, kind):
    """来源说明了类型就用；否则看游戏的 control.nacp：只有设备存档的按设备，其余按账户"""
    if kind:
        return kind
    nacp = next((c.library / "games" / game["id"] / "current").rglob("control.nacp"), None)
    with contextlib.suppress(OSError, struct.error, AttributeError):
        data = nacp.read_bytes()[:0x30A0]
        account, device = struct.unpack_from("<q", data, 0x3080)[0], struct.unpack_from("<q", data, 0x3090)[0]
        if account <= 0 < device:
            return "device"
    return "account"


def seed_target_mark(c, root):
    return fingerprint(c, root) if root.exists() else "no-save-yet"


@contextlib.contextmanager
def opened_seed(c, game, source):
    try:
        with save_formats.extracted(source, tid=game["id"]) as (_, data, program, kind, why):
            if program != game["id"].lower():
                fail(c, "这份存档属于另一款游戏（程序号 %s），已拒绝导入。" % program, "wrong_game")
            yield data, kind
    except (ValueError, OSError, zipfile.BadZipFile):
        fail(c, "备份损坏、格式不支持或包含不安全路径，已拒绝恢复。", "invalid")


def seed_preview(c, game, root, source):
    with opened_seed(c, game, source) as (data, _):
        files = [os.path.join(data, f) for f in save_formats.files_under(data)]
        total = sum(os.path.getsize(f) for f in files)
    return dict(
        manifest=dict(game=identity(game) | dict(name=game.get("name") or game["id"]), files=[]),
        archiveSha256=source_digest(c, source),
        targetFingerprint=seed_target_mark(c, root),
        previewLibraryId=c.r["libraryId"],
        fileCount=len(files),
        totalBytes=total,
        notice=SEED_NOTICE,
    )


def seed_restore(c, game, root, source):
    if (
        c.r.get("archiveSha256") != source_digest(c, source)
        or c.r.get("targetFingerprint") != seed_target_mark(c, root)
        or c.r.get("previewLibraryId") != c.r["libraryId"]
    ):
        fail(c, "备份或当前存档已变化，请重新检查备份后再确认恢复。", "changed")
    pending = pending_seed(c, game["id"])
    stage = pending.with_name(".pending-seed.new")
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, mode=0o700)
    with opened_seed(c, game, source) as (data, kind):
        copy_tree(c, Path(data), stage / seed_kind(c, game, kind))
    shutil.rmtree(pending, ignore_errors=True)
    os.replace(stage, pending)
    return dict(committed=True, hadOriginal=False, outputPath=str(pending), previousBackup="", cleanupWarning="")


def claim_seed(c, gid):
    """启动时调：有待放入的存档就返回目录，给 S2A_SAVE_DIR"""
    pending = pending_seed(c, gid)
    return pending if pending.is_dir() and any(pending.iterdir()) else None


def finish_seed(c, gid, log):
    """一局结束后：运行时报了灌入成功才删；没成功留着，下次启动再放"""
    pending = pending_seed(c, gid)
    with contextlib.suppress(OSError):
        if SEED_DONE_LINE in Path(log).read_text(encoding="utf-8", errors="replace"):
            shutil.rmtree(pending, ignore_errors=True)


def inspect(c):
    c.begin("校验存档备份")
    with stopped(c, tolerate=True) as (game, _, _):
        root, _, _ = locations(c, game)
        seed = seed_source(c, game, root)
        if seed is not None:
            return seed_preview(c, game, root, seed)
        _, manifest, sha, notice = checked_archive(c, game, root)
        return dict(
            manifest=manifest,
            archiveSha256=sha,
            targetFingerprint=fingerprint(c, root),
            previewLibraryId=c.r["libraryId"],
            fileCount=len(manifest["files"]),
            totalBytes=sum(v["size"] for v in manifest["files"]),
            # 目录结构存疑时给玩家的一句话（空的就正常）；恢复不因它拒绝，界面只在确认前提示
            notice=notice,
        )


def copy_tree(c, source, destination):
    files, dirs = scan(c, source)
    destination.mkdir(parents=True, exist_ok=True)
    for name in dirs:
        (destination / name).mkdir(parents=True, exist_ok=True)
    for name in files:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as f:
            os.chmod(target, 0o600)
            for chunk in file_chunks(c, source / name):
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())


def restore(c):
    c.begin("验证恢复条件", 1, 3)
    with stopped(c) as (game, _, _):
        root, transactions, backups = locations(c, game)
        seed = seed_source(c, game, root)
        if seed is not None:
            return seed_restore(c, game, root, seed)
        source, manifest, sha, _ = checked_archive(c, game, root)
        target_hash = fingerprint(c, root)
        if (
            c.r.get("archiveSha256") != sha
            or c.r.get("targetFingerprint") != target_hash
            or c.r.get("previewLibraryId") != c.r["libraryId"]
        ):
            fail(c, "备份或当前存档已变化，请重新检查备份后再确认恢复。", "changed")
        token = uuid.uuid4().hex
        transactions.mkdir(parents=True, exist_ok=True, mode=0o700)
        transaction = transactions / token
        transaction.mkdir(mode=0o700)
        atomic(transaction / "owner.json", dict(schema=1, gameId=game["id"], transaction=token))
        previous = backups / token
        previous.mkdir(parents=True, mode=0o700)
        sync_parents(transaction, c.library)
        sync_parents(previous, c.library)
        new, old = transaction / "new", previous / "data"
        journal = transactions / (game["id"] + ".json")
        had_original = root.exists()
        committed = False
        try:
            total = sum(info[2] for info in scan(c, root)[0].values()) + sum(
                f["size"] for f in manifest["files"]
            )
            if shutil.disk_usage(c.library).free < 2 * total + (64 << 20):
                fail(c, "游戏库磁盘空间不足，无法暂存并保留恢复前副本。", "space")
            if had_original and any(n.startswith("bis/user/save/") for n in scan(c, root)[0]):
                c.begin("保留恢复前副本", 2, 3)
                # Exportable rollback plus the untouched original directory, kept
                # even after a successful restore. Nothing deletes old saves.
                write_archive(c, game, root, previous / "before-restore.s2asave")
            copy_tree(c, root, new)
            for name in (*TREES, PROFILE):
                p = new / name
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink(missing_ok=True)
            with open_archive(c, source) as (z, checked):
                if checked != manifest or hash_file(c, source) != sha:
                    fail(c, "备份已变化，请重新检查。", "changed")
                for name in manifest["directories"]:
                    (new / name).mkdir(parents=True, exist_ok=True)
                done, total = 0, sum(f["size"] for f in manifest["files"])
                for item in manifest["files"]:
                    path = new / item["path"]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    h = hashlib.sha256()
                    with z.open("payload/" + item["path"]) as src, path.open("xb") as dst:
                        os.chmod(path, 0o600)
                        while chunk := src.read(CHUNK):
                            c.check()
                            dst.write(chunk)
                            h.update(chunk)
                            done += len(chunk)
                            c.progress(done, total)
                        dst.flush()
                        os.fsync(dst.fileno())
                    if h.hexdigest() != item["sha256"]:
                        raise ValueError("backup changed while extracting")
            if fingerprint(c, root) != target_hash:
                fail(c, "当前存档已变化，请停止游戏并重新检查备份。", "changed")
            for parent, _dirs, _ in os.walk(new, topdown=False):
                fsync_dir(parent)
            record = dict(
                schema=1,
                gameId=game["id"],
                transaction=token,
                hadOriginal=had_original,
                phase="prepared",
            )
            atomic(journal, record)
            fsync_dir(transactions)
            root.parent.mkdir(parents=True, exist_ok=True)
            sync_parents(root.parent, c.library)
            c.begin("提交存档恢复", 3, 3)
            if had_original:
                os.rename(root, old)
                fsync_dir(previous)
                fsync_dir(root.parent)
            c.check()
            os.rename(new, root)
            fsync_dir(root.parent)
            fsync_dir(transaction)
            c.check()
            atomic(journal, record | dict(phase="committed"))
            committed = True
            # 事务记录删不掉时这句也放进结果（审计 P2-10：原先只进任务诊断，面板照写「存档恢复已完成」，下次启动又恢复一遍玩家不知道为什么）
            cleanup_warning = ""
            try:
                fsync_dir(transactions)
                journal.unlink()
                fsync_dir(transactions)
                shutil.rmtree(transaction)
            except OSError:
                cleanup_warning = "存档已恢复，事务清理尚未完成。原副本已保留，请重新检查。"
                report(c, cleanup_warning, "cleanup", "warning")
            prior = previous / "before-restore.s2asave"
            return dict(
                committed=True,
                hadOriginal=had_original,
                outputPath=str(root),
                previousBackup=str(prior) if prior.is_file() else "",
                cleanupWarning=cleanup_warning,
            )
        except BaseException:
            if journal.exists():
                recover(c, game)
            raise
        finally:
            if not committed and transaction.exists() and not journal.exists():
                shutil.rmtree(transaction)
            # The previous archive and original directory are intentionally kept.
            if not committed and not any(previous.iterdir()):
                previous.rmdir()
