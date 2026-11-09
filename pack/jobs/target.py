"""打 iPhone 包的目标设备：GPU 架构 + 系统大版本（维护者 10-23）。

包里的管线归档和函数体 GPU 动态库按「GPU 架构 × 系统大版本」出（graphics/tools/shader_pack.sh 的 S2A_PSO_ARCHS / S2A_PSO_OS），
对不上的机型 / 系统整份不命中、每条管线首遇现编（#60 iPad 用户「预编译后仍卡」）。所以出包前要知道目标设备：
- 连着线：devicectl 读机型号（productType，如 iPhone18,2）和系统版本；
- 没连：玩家在界面上选机型和系统大版本（前端记住上次的选择）。
机型号 → GPU 架构不手写表：查所用 Xcode 自带的 device_traits.db（Devices.ProductType → DeploymentVariant → DeviceTraits.GPUArchName；
iPad 机型号带 -A / -B 变体后缀，按前缀认）。正对照：iPhone18,2（iPhone 17 Pro Max，A19 Pro）→ applegpu_g18p，与 shader_pack 缺省一致。
命令行（tools/oneshot.sh 用）：python3 pack/jobs/target.py arch <机型号> [DEVELOPER_DIR]
命令行（pack/oneclick/oneclick.sh 用）：python3 pack/jobs/target.py device [--device <UDID|设备名>]
  按 --device 指的 UDID / 设备名，或连着的那一台（连着多台时用开发描述文件登记的 UDID 缩小）认目标设备，输出一行五段（制表符分隔：
  gpuArch / 系统大版本 / 机型号 / 机型名 / 依据）；定不下来退出码 3（调用方按 shader_pack 缺省继续，不当作失败）。
  一键出包原先不认目标机型，缺省架构（10-27 之前是单个 applegpu_g18p，A19 Pro）覆盖不到的机型装箱后才在 link_glue 报
  「no stub archive」（10-27 R13）。10-28 起出包缺省按三种模式（pack/jobs/archs.py）：显式 S2A_PSO_ARCHS >
  S2A_PSO_ARCHS_ALL=1 全机型 > 我方真机集合；oneclick 只在设备架构不在集合内时才 export 单架构。
"""
from __future__ import annotations
import glob
import json
import os
import plistlib
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

# GPU 要 Apple7 及以上（A14 / M1 起，架构号 g13 起）；系统 iOS / iPadOS 18 起（10-26 维护者，依据 10-24 定的直连下限 iOS 18 / macOS 15）：
#   26 起缺省 Metal 4 主路径，18–25 自动走 Metal 3 兼容路径——通用渲染器 graphics/metal/gal_core.m gal_be_select、直连 graphics/nvn/gfx/nvn/nvn_mtl.m nvn_be_pick 都按系统版本选；
#   着色器库全部按 -mios-version-min=18.0 编（tools/toolchain.env），管线归档按目标系统大版本出（ipa.py S2A_PSO_OS）。M7 证据：milestones/M7_Metal3_1025（Mac 强制 Metal 3 直连动森到 MARK）
MIN_GEN, MIN_OS = 13, 18
# 架构要经「部署变体」表取（按系统版本各一行，老系统行是通用 air64，只取 applegpu_*）：Xcode 27.1 的库里 Devices.DeviceTraitSet 是占位，
# 直接连会把 iPhone18,2 认成 g14g（10-23 实查；旧 Xcode 碰巧对）
JOIN = "from Devices d join DeploymentVariant v on v.DeviceTarget = d.Target join DeviceTraits t on t.DeviceTraitSetID = v.DeviceTraitSet "


class TargetError(Exception):
    def __init__(self, message, *, code="job_failed"):
        super().__init__(message)
        self.code = code


def traits_db(developer_dir):
    db = Path(developer_dir) / "Platforms/iPhoneOS.platform/usr/standalone/device_traits.db"
    if not db.is_file():
        raise TargetError("所用 Xcode 里没有设备特征库（device_traits.db），认不出机型的 GPU：请装完整的 Xcode。", code="xcode_unavailable")
    return db


def _query(developer_dir, sql, args=()):
    con = sqlite3.connect(f"file:{traits_db(developer_dir)}?mode=ro", uri=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def gen(arch):
    m = re.match(r"applegpu_g(\d+)", arch or "")
    return int(m.group(1)) if m else 0


def gpu_arch(product_type, developer_dir):
    rows = _query(developer_dir, "select distinct t.GPUArchName " + JOIN +
                  "where (d.ProductType = ? or d.ProductType like ? || '-%') and t.GPUArchName like 'applegpu_%'", (product_type, product_type))
    archs = sorted({r[0] for r in rows})
    if len(archs) != 1:
        raise TargetError(f"这个 Xcode 认不出机型 {product_type} 的 GPU（{'查到多个：' + '、'.join(archs) if archs else '没有记录'}）：请把 Xcode 更新到最新版。", code="xcode_unavailable")
    return archs[0]


def models(developer_dir):
    """可选机型（A14 / M1 及更新芯片的 iPhone / iPad），新的在前：[{name, productType, gpuArch}]"""
    rows = _query(developer_dir, "select distinct d.ProductType, d.ProductDescription, t.GPUArchName " + JOIN +
                  "where (d.ProductType like 'iPhone%' or d.ProductType like 'iPad%') and t.GPUArchName like 'applegpu_%'")
    seen, out = set(), []
    for pt, name, arch in rows:
        pt = re.sub(r"-[A-Z]$", "", pt)
        if gen(arch) < MIN_GEN or not name or name in seen:
            continue
        seen.add(name)
        out.append(dict(name=name, productType=pt, gpuArch=arch))
    key = lambda m: (0 if m["productType"].startswith("iPhone") else 1, -int(re.search(r"(\d+),", m["productType"]).group(1)), m["name"])
    return sorted(out, key=key)


def os_choices(env):
    """可选系统大版本：MIN_OS 到所用 iOS SDK 的大版本"""
    cmd = ["xcrun", "--sdk", "iphoneos", "--show-sdk-version"]

    def failure(reason, rc, stderr, code="xcode_unavailable"):
        # TimeoutExpired 即使 text=True 也可能带 bytes；没有退出码时不能编一个。
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        lines = (stderr or "").strip().splitlines()
        tail = lines[-1] if lines else "（空）"
        return TargetError(f"{reason}；命令：{' '.join(cmd)}；退出码：{rc}；stderr 末行：{tail}", code=code)

    try:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as e:
        raise failure("读取 iOS SDK 超时（60 秒）", "未取得（超时）", e.stderr) from e
    except OSError as e:
        raise failure(f"无法启动 iOS SDK 探测：{e}", "未启动", None) from e
    if p.returncode:
        raise failure("读取 iOS SDK 失败", p.returncode, p.stderr)
    version = p.stdout.strip()
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", version):
        raise failure(f"iOS SDK 版本输出为空或非法：{version!r}", p.returncode, p.stderr)
    try:
        top = int(version.split(".")[0])
    except ValueError as e:
        raise failure("iOS SDK 版本数值非法", p.returncode, p.stderr) from e
    if top < MIN_OS:
        raise failure(f"iOS SDK {version} 太旧，需要 iOS {MIN_OS} 或更新的 SDK", p.returncode, p.stderr,
                      code="xcode_sdk_too_old")
    return list(range(top, MIN_OS - 1, -1))


def _keep_device(d):
    """连着的 iOS / iPadOS 真机：physical + 隧道通或有线已配对（与 connected() 同一判据，10-28 按维护方 A3 第 5 条抽出来共用）"""
    from diag import reachable
    h = d.get("hardwareProperties", {})
    return h.get("reality") == "physical" and h.get("platform") in ("iOS", "iPadOS") and reachable(d)


def _row(d, dev_dir, with_link=False):
    """devicectl 的一条设备 → 本模块的行；GPU 架构查设备特征库（认不出留空，由调用方决定怎么报）"""
    h, p = d.get("hardwareProperties", {}), d.get("deviceProperties", {})
    cp = d.get("connectionProperties", {})
    pt = h.get("productType", "")
    try:
        arch = gpu_arch(pt, dev_dir) if pt else ""
    except TargetError:
        arch = ""
    row = dict(id=d.get("identifier", ""), udid=h.get("udid", ""), name=(p.get("name") or "").strip(),
               model=h.get("marketingName") or pt, productType=pt, gpuArch=arch,
               os=int((p.get("osVersionNumber") or "0").split(".")[0]))
    if with_link:
        row.update(tunnel=cp.get("tunnelState") == "connected",
                   wired=cp.get("transportType") == "wired" and cp.get("pairingState") == "paired")
    return row


def cable_first(rows):
    """装机对象：有数据线连着的就只算线连的；同一网络上配对过的别的手机不算「连了两台」（11-08 单壳线：Spare-host 上 Duo 插线，测试设备2 经网络配对可见）。没有线连的才看网络的"""
    return [r for r in rows if r.get("wired")] or rows


def device_list(c, timeout=60):
    """devicectl 的原始设备表、读不到的原因（读到时为空）、退出码与原文。「读不到」和「没连」要分开报，玩家插着手机时不能说没连"""
    from diag import devicectl, NO_COREDEVICE
    try:
        rc, data, raw = devicectl(c, "list", "devices", timeout=timeout)
    except subprocess.TimeoutExpired:
        return [], "读取设备列表超时：解锁手机，重新插一次数据线再试。", "timeout", f"超过 {timeout} 秒"
    except OSError as e:
        return [], "读不到设备列表：打开一次 Xcode，等它装完附加组件后再试。", "oserror", str(e)
    if rc:
        why = NO_COREDEVICE if rc == 127 else "读不到设备列表：打开一次 Xcode，等它装完附加组件后再试。"
        return [], why, rc, raw
    return data.get("result", {}).get("devices", []), "", 0, raw


def usb_idevices():
    """USB 上看得到的 iPhone / iPad 名称（devicectl 只列 Xcode 配对过的设备，没配对的要靠这里知道「其实插着」）"""
    try:
        out = subprocess.run(["ioreg", "-r", "-c", "IOUSBHostDevice", "-l", "-w0"], capture_output=True, text=True, timeout=10).stdout
    except (subprocess.TimeoutExpired, OSError):
        return []
    return re.findall(r'"USB Product Name" = "((?:iPhone|iPad)[^"]*)"', out)


def connected(c, with_link=False):
    """连着线、已信任的 iPhone / iPad：[{id, udid, name, model, productType, gpuArch, os}]；with_link 多带 tunnel / wired"""
    from diag import devicectl
    # 读不到设备列表（devicectl 退出非 0 / 超时 / 起不来）= 当没连设备：可选机型与系统照常给，玩家还能手选机型
    #   （10-26 Spare-host 实测：启动器里 devicectl list devices 次次 60 秒超时，异常冲掉整个探测，机型 0 种、出不了 iPhone 包）
    try:
        rc, data, _ = devicectl(c, "list", "devices", timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return []
    if rc:
        return []
    dev_dir = c.env()["DEVELOPER_DIR"]
    return [_row(d, dev_dir, with_link) for d in data.get("result", {}).get("devices", []) if _keep_device(d)]


def developer_dir():
    """查设备特征库用的 Xcode：直接复用出包链那一份（pack/jobs/archs.py 的 developer_dir，10-28 维护方 A3 第 5 条：不留第二套解析）"""
    from archs import developer_dir as _dd
    return _dd()


def _devicectl(*args, timeout=60):
    """直接调系统 CoreDevice 框架里的 devicectl（同 diag.py：Xcode 的 usr/bin/devicectl 是包装脚本，版本不符时会弹组件安装框）。
    读不到（没装组件 / 超时 / 输出不是 JSON）返回 None，调用方当「没有设备信息」"""
    from diag import COREDEVICE
    exe = COREDEVICE / "Resources/bin/devicectl"
    if not (exe.is_file() and os.access(exe, os.X_OK)):
        return None
    with tempfile.NamedTemporaryFile(suffix=".json") as out:
        try:
            subprocess.run([str(exe), *args, "--json-output", out.name], capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return None
        try:
            return json.loads(Path(out.name).read_text() or "{}")
        except (OSError, ValueError):
            return None


def _idevice_product_type(udid):
    """devicectl 列不出这台设备时的兜底：本机装了 libimobiledevice 就直接问设备要机型号（tools/oneshot.sh 同一路）"""
    import shutil
    exe = shutil.which("ideviceinfo")
    if not exe or not udid:
        return ""
    try:
        p = subprocess.run([exe, "-u", udid, "-k", "ProductType"], capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def devices(dd=None):
    """连着（physical + 隧道通 / 有线已配对）的 iPhone / iPad —— 与 connected() 同一筛选：
    [{id, udid, name, productType, model, os, gpuArch, tunnel, wired}]"""
    dd = dd or developer_dir()
    data = _devicectl("list", "devices") or {}
    return [_row(d, dd, with_link=True) for d in data.get("result", {}).get("devices", []) if _keep_device(d)]


# 开发描述文件放的两处（同 pack/dev_sign.py）
PROFILE_GLOBS = ("~/Library/MobileDevice/Provisioning Profiles/*.mobileprovision",
                 "~/Library/Developer/Xcode/UserData/Provisioning Profiles/*.mobileprovision")


def profile_udids(paths=()):
    """开发描述文件里登记的设备 UDID（大写）：连着多台时用它缩小到「这个包能装的机器」。没给路径就扫本机两处缺省目录"""
    out = set()
    files = [Path(p).expanduser() for p in paths if p] or [Path(p) for pat in PROFILE_GLOBS for p in glob.glob(os.path.expanduser(pat))]
    for f in files:
        try:
            r = subprocess.run(["security", "cms", "-D", "-i", str(f)], capture_output=True, timeout=60)
            prof = plistlib.loads(r.stdout)
        except (subprocess.TimeoutExpired, OSError, ValueError, plistlib.InvalidFileException):
            continue
        for u in prof.get("ProvisionedDevices") or ():
            out.add(str(u).upper())
    return out


def our_archs():
    """我方真机集合的 GPU 架构（pack/our_devices.tsv，与出包链 pack/jobs/archs.py 同一份）。
    出包缺省按这个集合出（10-27 维护者：g18p + g19p，Duo 也覆盖）；取不到给空"""
    try:
        from archs import our_devices_archs
        return our_devices_archs()
    except Exception:
        return []


def _answer(d, source):
    if not d.get("gpuArch"):
        raise TargetError(f"认不出 {d.get('model') or d.get('productType')} 的 GPU：请把 Xcode 更新到最新版。", code="xcode_unavailable")
    return dict(gpuArch=d["gpuArch"], os=d.get("os") or 0, productType=d.get("productType", ""),
                model=d.get("model") or d.get("productType", ""), source=source, ours=our_archs())


def pick(want="", profiles=(), dd=None):
    """命令行出包的目标设备（与 ipa.py 同口径）：--device 指的 UDID / 设备名 > 连着的那一台 > 描述文件登记的那一台。
    定不下来抛 TargetError(code="target_unknown")：这不是失败，调用方按缺省架构出包并打一行说明"""
    dd = dd or developer_dir()
    allv = devices(dd)
    want = (want or "").strip()
    if want:
        hits = [d for d in allv if want in (d["id"], d["udid"], d["name"])]
        if len(hits) > 1:
            raise TargetError(f"「{want}」对上 {len(hits)} 台设备：请给 UDID", code="target_unknown")
        if hits:
            return _answer(hits[0], f"--device {want}")
        pt = _idevice_product_type(want)   # devicectl 列不出来（隧道超时 / 走无线）：问设备要机型号
        if not pt:
            raise TargetError(f"devicectl 没列出设备「{want}」（没连着这条线，或没在这台电脑上点过信任）", code="target_unknown")
        return _answer(dict(gpuArch=gpu_arch(pt, dd), os=0, productType=pt, model=pt), f"--device {want}（ideviceinfo）")
    live = [d for d in allv if d.get("tunnel") or d.get("wired")]
    source = "连着的那一台"
    if len(live) != 1:
        uds = profile_udids(profiles)
        narrowed = [d for d in live if d["udid"].upper() in uds]
        if len(narrowed) == 1:
            live, source = narrowed, "开发描述文件登记的那一台"
    if len(live) == 1:
        return _answer(live[0], source)
    if not live:
        raise TargetError("没连着 iPhone / iPad（也没给 --device）", code="target_unknown")
    raise TargetError(f"连着 {len(live)} 台设备，定不了给哪台出包：用 --device <UDID> 指定", code="target_unknown")


def check(target, sdk_top=None):
    """出包前核目标：架构和系统都在渲染器支持范围内；目标系统大版本不能比所用 Xcode 的 iOS SDK 新（老 SDK 出不了新系统的归档，装上整份不命中）"""
    arch, osv = target.get("gpuArch") or "", int(target.get("os") or 0)
    label = target.get("model") or arch
    if not arch:
        raise TargetError(f"认不出 {label} 的 GPU：请把 Xcode 更新到最新版。", code="xcode_unavailable")
    if gen(arch) < MIN_GEN:
        raise TargetError(f"{label} 的芯片太老，S2Arm 暂时打不了这台设备的包（需要 A14 / M1 及更新的芯片）。", code="target_unsupported")
    if osv < MIN_OS:
        raise TargetError(f"{label} 的系统是 iOS {osv}，S2Arm 需要 iOS {MIN_OS} 或更新：请先升级系统。", code="target_unsupported")
    if sdk_top and osv > sdk_top:
        raise TargetError(f"{label} 的系统是 iOS {osv}，但所用 Xcode 只带 iOS {sdk_top} 的 SDK，出不了这个系统版本的着色器：请把 Xcode 更新到最新版。", code="xcode_sdk_too_old")
    return dict(gpuArch=arch, os=osv, model=label)


def probe(c):
    """前端「目标设备」：已连设备 + 可选机型 + 可选系统大版本。
    请求带 quick：只给可选机型与系统（静态数据：Xcode 设备特征库 + SDK 版本），不读连着的设备（devicectl 要 1 秒以上）——
    启动器先发 quick 让出包页立刻能选机型，再在后台发完整探测、探到设备再标出来（10-26 维护者：等机型表 61 秒）"""
    if os.name == "nt":   # Windows 只出直连包（一包通用，不选机型 / 系统）：只报设备服务里连着的手机（UDID、连接方式）
        from install import win_install_mod
        W = win_install_mod(c)
        try:
            devs = [] if c.r.get("quick") else W.mux_devices()
        except OSError:
            devs = []
        seen = {}
        for udid, how, _ in devs:
            if udid and (udid not in seen or how == "USB"):
                seen[udid] = how
        return dict(connected=[dict(id=u, udid=u, connection="wired" if h == "USB" else "network") for u, h in sorted(seen.items())],
                    models=[], osChoices=[])
    env = c.env()
    try:
        return dict(connected=[] if c.r.get("quick") else connected(c), models=models(env["DEVELOPER_DIR"]), osChoices=os_choices(env))
    except TargetError as e:
        from core import Failure
        raise Failure(str(e), code=e.code) from e


def _arg(args, name):
    if name in args:
        i = args.index(name)
        return args[i + 1] if i + 1 < len(args) else ""
    return next((a.split("=", 1)[1] for a in args if a.startswith(name + "=")), "")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "arch":
        try:
            print(gpu_arch(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else developer_dir()))
        except TargetError as e:
            print(e, file=sys.stderr)
            sys.exit(1)
    elif len(sys.argv) >= 2 and sys.argv[1] == "device":
        try:
            r = pick(_arg(sys.argv[2:], "--device"))
        except TargetError as e:
            print(e, file=sys.stderr)
            sys.exit(3)   # 3 = 定不下来（不是失败）：调用方按 shader_pack 缺省出包，打一行说明
        # 制表符分隔六段：机型名里有空格（iPhone 15 Pro），不能用空格分列。
        # os 读不到（ideviceinfo 兜底没有系统版本）输出 "-" 而不是 0：0 会被当成真实系统版本，直连出 ios0 归档、通用闸报「iOS 0」（维护方 A3 第 4 条）
        print("\t".join(str(x) for x in (r["gpuArch"], r["os"] or "-", r["productType"] or "-", r["model"] or "-",
                                          r["source"], " ".join(r.get("ours") or []) or "-")))
    else:
        print("用法：python3 pack/jobs/target.py arch <机型号> [DEVELOPER_DIR]\n"
              "      python3 pack/jobs/target.py device [--device <UDID|设备名>]", file=sys.stderr)
        sys.exit(2)
