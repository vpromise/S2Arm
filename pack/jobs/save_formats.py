"""社区存档布局识别（JKSV / Checkpoint / yuzu / Ryujinx / 裸文件），启动器导入存档与 tools/save_import.py 共用。

只解析、不执行：zip 越界路径拒绝；不跟随软链。
  ryujinx     …/bis/user/save/<16 位十六进制>/0/，类型与程序号取同级 ExtraData0 开头 0x40 字节的存档属性
              （程序号 u64、用户号 16 字节、系统存档号 u64、类型 u8 在 0x20：1 账户 / 3 设备；同 saves.py 的 IMKV 键）
  yuzu        …/nand/user/save/0000000000000000/<32 位用户号>/<程序号>/（用户号全 0 = 设备存档）
  checkpoint  …/0x<程序号> <游戏名>/<时间戳 …>/（取最新一份）
  jksv        JKSV/<游戏名>/<用户名 - 时间戳>/ 或 zip（路径里没有程序号，调用方给 tid）；下钻到第一层有文件的目录，同层取最新
  nx2apple    <游戏号>/save（账户存档）、save_device（设备存档）；旁边的 .work / .new 是未提交的副本，不算存档
  raw         直接就是游戏存档文件；外面套的程序号 / 用户号目录剥掉，套着 JKSV 目录的按 JKSV；
              唯一子目录「0」有两种解释（Ryujinx 提交目录 / 游戏自己的槽位目录，如塞尔达 0/…5/），按调用方给的
              「这款游戏自己写出的存档顶层」（ref）比对决定剥不剥；没有参照或比不出 → 不剥、说明里报「结构存疑」
              有 edizon_save_metadata.json 的那层是 EdiZon 备份的存档根；只有 cheats/ 的是金手指，拒绝
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import struct
import tempfile
import zipfile

SKIP = {".DS_Store", "Thumbs.db", "desktop.ini", "edizon_save_metadata.json"}
RYU_META = {"ExtraData0", "ExtraData1"}
LAYOUTS = ("ryujinx", "yuzu", "checkpoint", "jksv", "nx2apple", "raw")
NX2APPLE_FOLDERS = {"save": "account", "save_device": "device"}
# 说明里带这个词 = 识别没定下来、按保守解释导入（zero_layer 的「结构存疑：…」）；
# 调用方（pack/jobs/saves.py 的 inspect）靠它把这类说明翻成给玩家的一句提示，不当成导入失败的证据
DOUBT = "结构存疑"
# 存档包上限（saves.py 我方 .s2asave 的同一套，审计 P1-47：社区 zip 原先 extractall 一次写完，高压缩比的包能写满私有目录）：
#   解压前按各条声明的未压缩大小加总、条数计数，超了就拒；zipfile 按声明大小读、超出会校验失败，所以声明值就是实际写入的上限
MAX_FILES = 20000
MAX_BYTES = 32 << 30


def files_under(d):
    """存档文件（相对 d 的路径排序），跳过系统杂项与 Ryujinx 的 ExtraData；遇到软链报错。"""
    out = []
    for base, dirs, names in os.walk(d):
        for x in dirs + names:
            if os.path.islink(os.path.join(base, x)):
                raise ValueError(f"存档里有链接：{x}")
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        out += [
            os.path.join(base, n)
            for n in sorted(names)
            if n not in SKIP and not n.startswith("._") and n not in RYU_META
        ]
    return out


def attr(blob):
    """存档属性（0x40）：程序号、用户号（32 位十六进制）、系统存档号、类型。"""
    program, high, low, static_id = struct.unpack_from("<4Q", blob)
    return program, f"{high:016x}{low:016x}", static_id, blob[0x20]


def own_top(d):
    """存档目录顶层名字（比对用）：跳过系统杂项、Ryujinx 元数据、点文件。"""
    return {x for x in os.listdir(d) if not x.startswith(".") and x not in SKIP and x not in RYU_META}


def zero_layer(d, ref):
    """d 下唯一子目录「0」该不该剥：剥 → 顶层是 0/ 里的东西，不剥 → 顶层是 0/ 本身；和 ref（游戏自己写出的存档顶层）比。
    返回 (剥不剥, 说明)；只有一种解释对得上才定，否则不剥并报结构存疑（10-27：塞尔达社区档 0/ 是槽位，被当提交目录剥掉、游戏开了新档）。"""
    inner = own_top(os.path.join(d, "0"))
    if ref:
        keep, strip = "0" in ref, bool(inner & ref)
        if strip != keep:
            return strip, ""
    return False, (DOUBT + "：唯一子目录 0/ 可能是 Ryujinx 提交目录也可能是游戏的存档槽位，"
                   + ("与这款游戏自己的存档比对不出" if ref else "没有这款游戏自己的存档可比") + "，按不剥导入")


def latest_child(d):
    subs = [os.path.join(d, x) for x in os.listdir(d) if os.path.isdir(os.path.join(d, x)) and not x.startswith(".")]
    return max(subs, key=lambda p: (os.path.getmtime(p), p)) if subs else d


def detect(src):
    """返回 [(布局, 存档目录, 程序号, 类型或 None, 说明)]，一个来源里可能有多份。"""
    found = []
    subs = {x for x in os.listdir(src) if os.path.isdir(os.path.join(src, x)) and not x.startswith(".")}
    folders = [x for x in NX2APPLE_FOLDERS if x in subs and files_under(os.path.join(src, x))]
    if folders and all(x in NX2APPLE_FOLDERS or x.endswith((".work", ".new")) for x in subs):
        # 外层文件夹就是 <游戏号> 时用它；否则由调用方给的程序号定（程序号留空）
        name = os.path.basename(src.rstrip("/\\")).lower()
        return [("nx2apple", os.path.join(src, folders[0]), name if re.fullmatch(r"[0-9a-f]{16}", name) else None, NX2APPLE_FOLDERS[folders[0]], "NX2Apple " + folders[0])]
    for base, dirs, _ in os.walk(src):
        parts = base[len(src):].strip("/").split("/") if base != src else []
        if len(parts) >= 2 and parts[-1] == "0" and re.fullmatch(r"[0-9a-fA-F]{16}", parts[-2]):
            meta = os.path.join(os.path.dirname(base), "ExtraData0")
            if os.path.isfile(meta) and os.path.getsize(meta) >= 0x40:
                with open(meta, "rb") as f:
                    program, user, static_id, kind = attr(f.read(0x40))
                if program and not static_id and kind in (1, 3):
                    found.append(("ryujinx", base, f"{program:016x}", "account" if kind == 1 else "device", f"ExtraData0 用户号 {user}"))
                    dirs[:] = []
                    continue
        if (
            len(parts) >= 3
            and re.fullmatch(r"[0-9a-fA-F]{16}", parts[-1])
            and re.fullmatch(r"[0-9a-fA-F]{32}", parts[-2])
            and parts[-3] == "0000000000000000"
        ):
            kind = "device" if int(parts[-2], 16) == 0 else "account"
            found.append(("yuzu", base, parts[-1].lower(), kind, f"yuzu 用户号 {parts[-2].lower()}"))
            dirs[:] = []
            continue
        m = re.fullmatch(r"0x([0-9a-fA-F]{16})(?: .*)?", parts[-1]) if parts else None
        if m:
            d = latest_child(base)
            found.append(("checkpoint", d, m.group(1).lower(), None, f"Checkpoint 取 {os.path.basename(d)}"))
            dirs[:] = []
    return found


@contextlib.contextmanager
def extracted(src, tid=None, layout=None, ref=None):
    """打开来源（目录或 zip），给出 (布局, 存档目录, 程序号, 类型或 None, 说明)；认不出 / 有歧义抛 ValueError（中文原因）。
    ref：这款游戏自己写出的存档顶层名字集合（同程序号已有存档 / 游戏首次启动建出的存档），只用来判唯一子目录「0」剥不剥。"""
    tmp = None
    try:
        src = os.fspath(src)
        if os.path.islink(src):
            raise ValueError("来源是链接")
        if os.path.isfile(src) and zipfile.is_zipfile(src):
            tmp = tempfile.mkdtemp(prefix="s2a_saveimp_")
            with zipfile.ZipFile(src) as z:
                for i in z.infolist():
                    parts = i.filename.replace("\\", "/").split("/")
                    if i.filename.startswith(("/", "\\")) or ".." in parts or (i.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError(f"zip 里有越界路径或链接：{i.filename}")
                n, size = len(z.infolist()), sum(i.file_size for i in z.infolist())
                if n > MAX_FILES or size > MAX_BYTES:
                    raise ValueError(f"zip 解压后太大：{n} 个文件、{size >> 20} MB（上限 {MAX_FILES} 个、{MAX_BYTES >> 30} GB）")
                z.extractall(tmp)
            src = tmp
        if not os.path.isdir(src):
            raise ValueError("来源不是目录或 zip")
        tid = tid.lower().removeprefix("0x") if tid else None
        hits = [] if layout in ("jksv", "raw") else detect(src)
        if tid:
            hits = [(h[0], h[1], h[2] or tid, h[3], h[4]) for h in hits]
            hits = [h for h in hits if h[2] == tid] or hits
        if layout and layout not in ("jksv", "raw"):
            hits = [h for h in hits if h[0] == layout]
        if len(hits) > 1:
            raise ValueError(f"认出 {len(hits)} 份存档（{', '.join(h[0] + ':' + h[2] for h in hits)}），须指明程序号")
        if hits:
            result = hits[0]
        else:
            if not tid:
                raise ValueError("认不出布局（JKSV / 裸文件路径里没有程序号），须指明程序号")
            d, why = src, ""
            lay = layout or ("jksv" if any(os.path.isdir(os.path.join(src, x)) for x in os.listdir(src)) else "raw")
            ez = [b for b, _, ns in os.walk(d) if "edizon_save_metadata.json" in ns] if lay == "raw" else []
            if len(ez) == 1:   # EdiZon 备份：带元数据的那层就是存档根
                d = ez[0]
            while lay == "raw":   # 剥包装层：程序号 / 用户号目录、Ryujinx 提交目录「0」；套着 JKSV 目录的转 JKSV
                subs = [x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x)) and not x.startswith(".")]
                if any(os.path.isfile(os.path.join(d, x)) and x not in SKIP for x in os.listdir(d)) or len(subs) != 1:
                    break
                if subs[0] == "JKSV":
                    lay = "jksv"
                elif subs[0] == "0":
                    strip, why = zero_layer(d, ref)
                    if not strip:
                        break
                elif not re.fullmatch(r"[0-9a-fA-F]{16}|[0-9a-fA-F]{32}", subs[0]):
                    break
                d = os.path.join(d, subs[0])
            if [x for x in os.listdir(d) if not x.startswith(".")] == ["cheats"]:
                raise ValueError("只有 cheats/（金手指），不是存档")
            if lay in ("jksv", "checkpoint"):   # 没有 0x<程序号> 那层的 Checkpoint 包同 JKSV：时间戳目录里取最新
                while True:
                    subs = [x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x)) and not x.startswith(".")]
                    if any(os.path.isfile(os.path.join(d, x)) and x not in SKIP for x in os.listdir(d)) or not subs:
                        break
                    if subs == ["0"]:   # 同 raw：0/ 可能是槽位
                        strip, why = zero_layer(d, ref)
                        if not strip:
                            break
                    d = latest_child(d)
            kind = "device" if os.path.basename(src.rstrip("/\\")) == "save_device" else None   # 直接拖 NX2Apple 的 save_device 文件夹：名字说明了类型
            result = (lay, d, tid, kind, why)
        if not files_under(result[1]):
            raise ValueError(f"{result[1]} 下没有存档文件")
        yield result
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
