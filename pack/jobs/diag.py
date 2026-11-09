"""拉回 iPhone / iPad 上的诊断（Mac 前端「拉回 iPhone 诊断…」，用户体验线 10-23）。

用系统已装 CoreDevice 框架里的 devicectl（不经 Xcode 的包装脚本，见 COREDEVICE）：列出已连接的 iOS 设备 → 找 S2Arm 打的 App（包名 io.s2arm.* / io.s2arm-exp.* / local.s2arm.*，
AltStore 重签后会在后面加团队号，按前缀认）→ 从 App 数据目录只拉诊断要的文件（Documents/diag 里 App 导出的诊断包、最近 3 次
会话的 host.log、frames.tsv、threads.txt、unpack.log、metrickit/），不拉存档和资源包 → 放进 <私有目录>/diagnostics/<时间-随机>/<设备>_<包名>-随机/。
请求里给 device（设备号或名字）就只拉那一台。
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from core import Failure

PREFIXES = ("io.s2arm.", "io.s2arm-exp.", "local.s2arm.", "s2arm.")
# 只拉这些（相对 App 数据目录；Documents/logs = os.c 每局一份的往局日志 host-<起局时刻>.log）；存档（bis/user/save）、资源包、密钥都不碰
ITEMS = ("Documents/diag", "Documents/host.log", "Documents/logs", "Documents/frames.tsv",
         "Documents/threads.txt", "Documents/unpack.log", "Documents/metrickit")


# 系统已装的 CoreDevice 框架（Xcode「附加组件」装进 /Library/Developer）。直接调它自带的 devicectl，不经 Xcode 的 usr/bin/devicectl：
#   那个是 zsh 包装脚本，所选 Xcode 期望的 CoreDevice 版本和已装的不同（10-26 Spare-host：启动器选 Xcode 27.1 要 651.13.5，系统装的是 Xcode 27.0 的 642.16）
#   就先跑 xcodebuild -runFirstLaunch 装组件 → 弹管理员密码框；启动器里每次探测都停在框上、60 秒超时（ssh 下没有图形界面、这步直接失败跳过，所以 1 秒）。
#   已装的 CoreDevice 本身能用；出包不替玩家装组件、不弹框。S2A_COREDEVICE 单测改位置
COREDEVICE = Path(os.environ.get("S2A_COREDEVICE", "/Library/Developer/PrivateFrameworks/CoreDevice.framework/Versions/A"))
NO_COREDEVICE = "这台 Mac 没装 Xcode 的设备组件（CoreDevice），读不到连着的设备：打开一次 Xcode、按提示装附加组件后再试；也可以在出包页直接选机型"


def devicectl(c, *args, timeout=120):
    exe = COREDEVICE / "Resources/bin/devicectl"
    if not (exe.is_file() and os.access(exe, os.X_OK)):
        return 127, {}, NO_COREDEVICE
    with tempfile.NamedTemporaryFile(suffix=".json", dir=c.private) as out:
        p = subprocess.run([str(exe), *args, "--json-output", out.name], env=c.env(), capture_output=True, text=True, timeout=timeout)
        try:
            data = json.loads(Path(out.name).read_text() or "{}")
        except ValueError:
            data = {}
    return p.returncode, data, (p.stderr or p.stdout).strip()


def reachable(d):
    """连着线或隧道已通。数据线连着、已配对时隧道空闲是 disconnected，devicectl 用到会自己拉起（10-26 测试设备2 插着线被判「没连 iPhone」）"""
    cp = d.get("connectionProperties", {})
    return cp.get("tunnelState") == "connected" or (cp.get("transportType") in ("wired", "localNetwork") and cp.get("pairingState") == "paired")


def iml_hint(c, probe, app):
    """App 首启复核写的 Documents/diag/iml_probe.json（ios/iml_probe.m，签名里真实生效的权限）：has_iml_ent 为 false 就打一行黄字提示重签；
    文件没有 / 读不了 / 不是 false（true、null = 读不到）都不提示、不报错"""
    try:
        rec = json.loads(probe.read_text())
    except (OSError, ValueError):
        return
    if isinstance(rec, dict) and rec.get("has_iml_ent") is False:
        c.diagnostic(f"{app}：这台机上装的包签名丢了提高内存上限权限，大游戏容易被系统杀；用 Xcode / AltStore Classic v2.3 重签后重装",
                     code="iml_missing", severity="warning")


def pull(c):
    c.begin("找已连接的 iPhone / iPad", 1, 3)
    rc, data, err = devicectl(c, "list", "devices")
    if rc:
        raise Failure("读不到设备列表：需要完整的 Xcode（devicectl）。" + (err.splitlines()[-1] if err else ""))
    want = (c.r.get("device") or "").strip()
    devices = [d for d in data.get("result", {}).get("devices", [])
               if d.get("hardwareProperties", {}).get("platform") in ("iOS", "iPadOS")
               and reachable(d)
               and (not want or want in (d.get("identifier"), d.get("hardwareProperties", {}).get("udid"), d.get("deviceProperties", {}).get("name")))]
    if not devices:
        raise Failure("没找到已连接的 iPhone / iPad：用数据线连上并解锁，在手机上点「信任这台电脑」，再试一次。")
    stamp = time.strftime("%m%d-%H%M%S")
    parent = c.private / "diagnostics"
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=stamp + "-", dir=parent))
    pulled = []
    c.begin("找 S2Arm 打的游戏 App", 2, 3)
    for d in devices:
        dev = d["identifier"]
        name = re.sub(r"[^\w.-]+", "_", d.get("hardwareProperties", {}).get("marketingName") or d.get("hardwareProperties", {}).get("productType") or "iPhone")
        rc, apps, _ = devicectl(c, "device", "info", "apps", "--device", dev)
        for app in apps.get("result", {}).get("apps", []):
            bid = app.get("bundleIdentifier", "")
            if not bid.startswith(PREFIXES):
                continue
            c.begin("拉回 " + (app.get("name") or bid), 3, 3)
            dest = Path(tempfile.mkdtemp(prefix=f"{name}_{bid}-", dir=root))
            got = 0
            for item in ITEMS:
                target = dest / Path(item).name
                rc, _, _ = devicectl(c, "device", "copy", "from", "--device", dev, "--domain-type", "appDataContainer",
                                     "--domain-identifier", bid, "--source", item, "--destination", str(target), timeout=300)
                if rc == 0:
                    got += 1
                elif target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)  # Only this failed copy in the new exclusive directory.
                else:
                    target.unlink(missing_ok=True)
            if got:
                pulled.append(dict(app=app.get("name") or bid, bundle=bid, files=got, path=str(dest)))
                iml_hint(c, dest / "diag" / "iml_probe.json", app.get("name") or bid)
    if not pulled:
        raise Failure("连着的设备上没找到 S2Arm 打的游戏 App，或者这些 App 还没运行过、没有日志。")
    return dict(outputPath=str(root), apps=pulled)
