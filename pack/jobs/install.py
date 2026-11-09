"""装到 iPhone（10-26 维护者 定：Xcode 做主路、AltStore 备选）：打 iPhone 包出的是自签 IPA，iPhone 不认；
用玩家在 Xcode 里登录的 Apple ID（免费的个人团队就行）重签后，用数据线直接装上，再把资源包拷进去、逐文件核对。

  ① 团队：优先读 Xcode 团队缓存；账号存在但缓存缺失时核本机开发描述文件与可用签名身份。请求给了 teamId 就用它，否则优先付费团队；找不到不当作未登录
  ② 设备：连着线、已信任的那台 iPhone / iPad（恰好一台；请求可给 device 指定）
  ③ 包名：io.s2arm.games.<团队号小写>（所有游戏进同一个容器 App，主页亮点「一个 App 装下整个游戏库」，维护者 11-08）——包名在 Apple 那边按团队注册，别的团队用过的同名包名注册不了
  ④ pack/ensure_profile.sh（xcodebuild -allowProvisioningUpdates，带设备号时顺带登记这台设备）拿开发描述文件 → pack/dev_sign.py 重签（核「提高内存上限」）
  ⑤ devicectl 装 App → tools/push_pack.py 按内容增量推资源包（同 dev_run、Windows 一键装机）→ 起 App 只解包，机上 .applied 对上本包索引才算装好
10-26 本机实测（免费个人团队、测试设备2）：描述文件 27 秒、签名 7 秒、安装 3 秒。免费账号签的 App 7 天过期，到期连上再装一次就行（资源包已在机上的不重拷）。"""
from __future__ import annotations

import argparse
import json
import datetime
import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from core import Failure, atomic, contained
from errors import recovery


def profile_teams(env=None):
    """从未过期开发描述文件及可用私钥身份发现团队；最终账号权限仍由 Xcode 签名验证。"""
    deadline = time.monotonic() + 8
    try:
        p = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"], capture_output=True, text=True, env=env, timeout=5)
        identities = set(re.findall(r"\b[0-9A-Fa-f]{40}\b", p.stdout.upper())) if p.returncode == 0 else set()
    except (OSError, subprocess.TimeoutExpired):
        return []
    if not identities:
        return []
    home = Path((env or os.environ).get("HOME", str(Path.home())))
    folders = (home / "Library/Developer/Xcode/UserData/Provisioning Profiles", home / "Library/MobileDevice/Provisioning Profiles")
    out, seen = [], set()
    now = datetime.datetime.now(datetime.timezone.utc)
    for folder in folders:
        for path in sorted(folder.glob("*.mobileprovision")):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return out
            try:
                p = subprocess.run(["security", "cms", "-D", "-i", str(path)], capture_output=True, env=env, timeout=min(2, remaining))
                if p.returncode:
                    continue
                profile = plistlib.loads(p.stdout)
                expiry = profile.get("ExpirationDate")
                if not isinstance(expiry, datetime.datetime) or expiry.replace(tzinfo=datetime.timezone.utc) <= now:
                    continue
                ent = profile.get("Entitlements", {})
                teams = profile.get("TeamIdentifier", [])
                if len(teams) != 1 or not isinstance(teams[0], str) or not re.fullmatch(r"[A-Z0-9]{10}", teams[0]):
                    continue
                tid = teams[0]
                if ent.get("get-task-allow") is not True or not profile.get("ProvisionedDevices") or ent.get("com.apple.developer.team-identifier") != tid:
                    continue
                if not any(hashlib.sha1(cert).hexdigest().upper() in identities for cert in profile.get("DeveloperCertificates", [])):
                    continue
                if tid not in seen:
                    seen.add(tid)
                    # 缺少个人团队标记时保守按免费处理，不据缺字段断言付费资格。
                    out.append(dict(id=tid, name=profile.get("TeamName") or "", free=profile.get("LocalProvision") is not False))
            except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError):
                continue
    return out


# 玩家话术（环境检查、装机清单、失败提示同一份）。Xcode 里加完 Apple ID，团队要等账户页把团队列表显示出来才写进缓存（11-08 MacBook Air 实走）：
#   判据是团队号真的出现，不是账号存在
ACCOUNT_NONE = ("还没有登录 Apple ID：打开 Xcode，点菜单 Xcode › Settings › Apple Accounts，点 + 添加你的 Apple ID（免费的就行），"
                "选中账号，等团队列表显示出来，再回来重新检查。装了多个 Xcode 也只要登一个，账号是共用的。")
TEAM_PENDING = ("Apple ID 已经登录，但这台 Mac 还没有签名团队：点「解决办法」打开现成的引导项目，在 Xcode 里选 Personal Team，"
                "连上手机点运行一次，回来会自动打勾。也可以先到 Xcode › Settings › Apple Accounts 看团队列表有没有显示。")


def _security_text(args, env):
    try:
        p = subprocess.run(["security", *args], capture_output=True, text=True, env=env, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout if p.returncode == 0 else ""


def development_identities(env=None):
    """钥匙串里带私钥、能用的开发证书的 SHA-1。"""
    text = _security_text(["find-identity", "-v", "-p", "codesigning"], env)
    return {h.upper() for h in re.findall(r'\b([0-9A-Fa-f]{40})\s+"(?:Apple Development|iPhone Developer):', text)}


def keychain_teams(env=None):
    """钥匙串开发证书的 OU 就是团队号，不依赖 Xcode 偏好缓存。证书看不出是否付费，按免费处理（只影响到期提示）；最终权限由 Xcode 签名验证。"""
    identities = development_identities(env)
    if not identities:
        return []
    text = _security_text(["find-certificate", "-a", "-Z", "-p"], env)
    out, seen = [], set()
    for sha, pem in re.findall(r"SHA-1 hash: ([0-9A-Fa-f]{40})\s+(-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----)", text, re.S):
        if sha.upper() not in identities:
            continue
        try:
            subject = subprocess.run(["/usr/bin/openssl", "x509", "-noout", "-subject"], input=pem, capture_output=True, text=True, env=env, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        match = re.search(r"\bOU\s*=\s*([A-Z0-9]{10})\b", subject)
        if match and match[1] not in seen:
            seen.add(match[1])
            out.append(dict(id=match[1], name="", free=True))
    return out


PREFS_UNREADABLE = "S2ArmPrefsUnreadable"


def _xcode_prefs(env):
    p = subprocess.run(["defaults", "export", "com.apple.dt.Xcode", "-"], capture_output=True, env=env, timeout=20)
    if p.returncode:
        return {}
    # 第三方 Xcode 扩展的快捷键键名可能带控制字符（如 ESC），defaults 原样导出，XML 不允许，解析会失败：先去掉（#144）
    text = re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", p.stdout)
    try:
        return plistlib.loads(text)
    except Exception:
        return {PREFS_UNREADABLE: True}


def signing_summary(env=None):
    """给诊断用的签名现状：只有版本和数量，不含账号和团队名。"""
    if os.name == "nt":
        return ""
    try:
        prefs = _xcode_prefs(env)
        version = subprocess.run(["xcodebuild", "-version"], capture_output=True, text=True, env=env, timeout=20).stdout.splitlines()[:1]
    except (OSError, subprocess.TimeoutExpired):
        prefs, version = {}, []
    accounts = prefs.get("DVTDeveloperAccountManagerAppleIDLists") or {}
    count = sum(len(xs) for xs in accounts.values() if isinstance(xs, list)) if isinstance(accounts, dict) else 0
    return (f"{version[0] if version else 'Xcode 未知'}；账号 {count} 个；团队缓存键{'读不了偏好' if PREFS_UNREADABLE in prefs else '有' if 'IDEProvisioningTeamByIdentifier' in prefs else '无'}；"
            f"可用团队 {len(xcode_account(env)[1])} 个；开发证书 {len(development_identities(env))} 张")


def xcode_account(env=None):
    """(有没有登录 Apple ID, 团队列表)：依次看 Xcode 团队缓存、本机描述文件、钥匙串开发证书，定位不到才算没有。"""
    if os.name == "nt":   # Windows 没有 Xcode 账号（也没有 defaults 命令）：签名走 Impactor
        return False, []
    prefs = _xcode_prefs(env)
    out, seen = [], set()
    for teams in (prefs.get("IDEProvisioningTeamByIdentifier") or {}).values():
        for t in teams or []:
            tid = t.get("teamID")
            if tid and tid not in seen:
                seen.add(tid)
                out.append(dict(id=tid, name=t.get("teamName") or "", free=bool(t.get("isFreeProvisioningTeam"))))
    accounts = prefs.get("DVTDeveloperAccountManagerAppleIDLists") or {}
    logged_in = isinstance(accounts, dict) and any(isinstance(xs, list) and any(isinstance(a, dict) and a.get("identifier") for a in xs) for xs in accounts.values())
    return logged_in, out or (profile_teams(env) if logged_in else []) or keychain_teams(env)


SIGNING_PBXPROJ = """// !$*UTF8*$!
{ archiveVersion = 1; classes = {}; objectVersion = 56; objects = {
A1000000000000000000000A = {isa = PBXBuildFile; fileRef = A1000000000000000000000B; };
A1000000000000000000000B = {isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = App.swift; sourceTree = "<group>"; };
A1000000000000000000000C = {isa = PBXFileReference; explicitFileType = wrapper.application; includeInIndex = 0; path = S2ArmSigning.app; sourceTree = BUILT_PRODUCTS_DIR; };
A1000000000000000000000D = {isa = PBXGroup; children = (A1000000000000000000000B, A1000000000000000000000E); sourceTree = "<group>"; };
A1000000000000000000000E = {isa = PBXGroup; children = (A1000000000000000000000C); name = Products; sourceTree = "<group>"; };
A1000000000000000000000F = {isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = (A1000000000000000000000A); runOnlyForDeploymentPostprocessing = 0; };
A10000000000000000000010 = {isa = PBXNativeTarget; buildConfigurationList = A10000000000000000000013; buildPhases = (A1000000000000000000000F); buildRules = (); dependencies = (); name = S2ArmSigning; productName = S2ArmSigning; productReference = A1000000000000000000000C; productType = "com.apple.product-type.application"; };
A10000000000000000000011 = {isa = PBXProject; buildConfigurationList = A10000000000000000000012; compatibilityVersion = "Xcode 14.0"; developmentRegion = en; hasScannedForEncodings = 0; knownRegions = (en, Base); mainGroup = A1000000000000000000000D; productRefGroup = A1000000000000000000000E; projectDirPath = ""; projectRoot = ""; targets = (A10000000000000000000010); };
A10000000000000000000012 = {isa = XCConfigurationList; buildConfigurations = (A10000000000000000000014); defaultConfigurationIsVisible = 0; defaultConfigurationName = Debug; };
A10000000000000000000013 = {isa = XCConfigurationList; buildConfigurations = (A10000000000000000000015); defaultConfigurationIsVisible = 0; defaultConfigurationName = Debug; };
A10000000000000000000014 = {isa = XCBuildConfiguration; buildSettings = {IPHONEOS_DEPLOYMENT_TARGET = 16.0; SDKROOT = iphoneos; SWIFT_VERSION = 5.0; ONLY_ACTIVE_ARCH = YES; }; name = Debug; };
A10000000000000000000015 = {isa = XCBuildConfiguration; buildSettings = {CODE_SIGN_STYLE = Automatic; GENERATE_INFOPLIST_FILE = YES; INFOPLIST_KEY_UILaunchScreen_Generation = YES; PRODUCT_BUNDLE_IDENTIFIER = "@BUNDLE@"; PRODUCT_NAME = "$(TARGET_NAME)"; TARGETED_DEVICE_FAMILY = "1,2"; }; name = Debug; };
}; rootObject = A10000000000000000000011; }
"""
SIGNING_APP = """import SwiftUI

@main struct S2ArmSigningApp: App {
    var body: some Scene { WindowGroup { Text("S2Arm 签名已就绪，可以回到电脑继续") } }
}
"""


def signing_project(c):
    """没有团队的玩家用的引导项目：放在私有目录，玩家在 Xcode 选 Personal Team、连手机运行一次，Xcode 就会生成证书和描述文件。"""
    root = c.private / "signing-helper"
    project = root / "S2ArmSigning.xcodeproj"
    project.mkdir(parents=True, exist_ok=True)
    bundle = "io.s2arm.signing.h" + hashlib.sha256(str(c.private).encode()).hexdigest()[:10]   # 免费账号的包名要全局唯一：按这台机的数据目录算，重开不变
    (project / "project.pbxproj").write_text(SIGNING_PBXPROJ.replace("@BUNDLE@", bundle), encoding="utf-8")
    (root / "App.swift").write_text(SIGNING_APP, encoding="utf-8")
    return dict(path=str(project))


def xcode_teams(env=None):
    return xcode_account(env)[1]


def no_team_failure(env=None):
    """没有可用团队时的失败：还没登录，和登录了但团队没同步，是两件事。"""
    return Failure(TEAM_PENDING, code="xcode_team_pending") if xcode_account(env)[0] else Failure(ACCOUNT_NONE, code="xcode_account_missing")


def pick_team(teams, want=None):
    if want:
        t = next((t for t in teams if t["id"] == want), None)
        if t:
            return t
        if not re.fullmatch(r"[A-Z0-9]{10}", want):
            raise Failure("Team ID 要是 10 位大写字母或数字，可以在 Xcode 的账户页或 Apple 开发者网站查到。", code="xcode_account_missing")
        return dict(id=want, name="", free=True)   # 玩家手填的：本机查不到也试，能不能用由 Xcode 签名给真实结果
    if not teams:
        raise no_team_failure()
    paid = [t for t in teams if not t["free"]]
    return (paid or teams)[0]


# 多款容器（启动器线 11-07，一个 App 装多款游戏；组装见 pack/ship_ext.py container、pack/jobs/ipa.py container）：
#   包名固定 io.s2arm.games.<团队号小写>：本来就按团队分（不会被别的团队占），不用 NX2Apple 那样「缺省包名被占 → 团队变体 → 落盘沿用」；
#     加款 / 换款都是同一个包名覆盖安装（不卸载，Documents 保留），一个团队只占一个 App ID（免费账号 7 天 10 个的额度）。换团队 = 换包名 = 新沙盒，旧存档要迁
#   设备侧每款文档根 Documents/games/<游戏号>（资源包 / 存档 / 直连见过的键都在它下面），起这一款的启动上下文 @bundle/Games/<游戏号>/launch.ctx——与 ship_ext 容器布局同口径
CONTAINER_DIR = "container"   # 游戏库 iPhone/container/：S2Arm.app（下次加款的底子）+ S2Arm.ipa


def container_bundle_id(team):
    return f"io.s2arm.games.{team.lower()}"


def game_docs(gid):
    return f"Documents/games/{gid.upper()}"


def game_ctx(gid):
    return f"@bundle/Games/{gid.upper()}/launch.ctx"


S_NO_USB = "这台 Mac 的 USB 上没看到 iPhone / iPad：换一根能传数据的线（充电线不行）、换个接口直连 Mac，并解锁手机。"
S_NOT_LISTED = "这台 Mac 看到手机插着，但设备服务还没列出它：解锁手机，在弹出的框里点「信任」；还不行就拔掉重插一次。"
S_MANY_WIRED = "连着不止一台 iPhone / iPad：只留要装的那一台。"
S_MANY_NETWORK = "同一网络上有多台配对过的 iPhone / iPad：用数据线连上要装的那一台。"
S_STALE_CHOICE = "之前选的设备「{0}」现在不在，可选：{1}。用数据线只连要装的那一台再试。"
S_TRUST = "在 iPhone 上点「信任」，再输入手机的锁屏密码。"
S_DEV_MODE = "在 iPhone 上打开「设置 → 隐私与安全性 → 开发者模式」，按提示重启手机；重启后解锁，这里会自动打勾。"
S_TUNNEL = "和手机建立开发连接失败：解锁手机后再试；走无线的话换成数据线。"
S_DDI = "手机的开发者服务没准备好：解锁手机后再试；还不行就重启手机。"
PREPARE_COOLDOWN = 30   # 清单几秒问一次：配对框、更新映像这类有动静的步骤隔一会儿才重试


def _link(d):
    return d.get("connectionProperties", {})


def _mask(value):
    return f"{value[:4]}…{value[-2:]}" if len(value or "") > 8 else "…"


def _clean(text):
    """诊断里的 devicectl 原文：设备序列号、UUID 不进诊断包"""
    return re.sub(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4,16}(?:-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})?\b", "…", str(text or ""))


def _with_raw(sentence, raw):
    """定句走翻译，原文另起一行照原样显示"""
    tail = _clean(raw).strip().splitlines()
    return sentence + ("\n" + tail[-1] if tail else "")


def _pick_device(devices, want):
    """要装的那台：返回 (设备, 连接环节的问题)。旧选择对不上时不算没连：只有一台就用它，多台列出来让玩家选"""
    phones = [d for d in devices if d.get("hardwareProperties", {}).get("reality") == "physical" and d.get("hardwareProperties", {}).get("platform") in ("iOS", "iPadOS")]
    live = [d for d in phones if _link(d).get("transportType") in ("wired", "localNetwork") or _link(d).get("tunnelState") == "connected"]
    if want:
        def named(d):
            return want in (d.get("identifier"), d.get("hardwareProperties", {}).get("udid"), (d.get("deviceProperties", {}).get("name") or "").strip())
        chosen = [d for d in live if named(d)]
        if chosen:
            live = chosen
        elif len(pool := [d for d in live if _link(d).get("transportType") == "wired"] or live) > 1:   # 有线优先，同网配对过的不列
            return None, S_STALE_CHOICE.replace("{0}", want).replace("{1}", "、".join(dict.fromkeys((d.get("deviceProperties", {}).get("name") or "").strip() for d in pool)))
    wired = [d for d in live if _link(d).get("transportType") == "wired"]
    if len(wired) > 1:
        return None, S_MANY_WIRED
    if not wired and len(live) > 1:
        return None, S_MANY_NETWORK
    if wired or live:
        return (wired or live)[0], ""
    return None, ""


def _cooled(c, device, step, polling):
    """清单轮询时同一步隔 PREPARE_COOLDOWN 秒才再做一次；真正装机时总是做"""
    if not polling:
        return True
    path = c.private / "device-prepare.json"
    try:
        marks = json.loads(path.read_text())
    except (OSError, ValueError):
        marks = {}
    key = f"{device.get('identifier')}:{step}"
    if time.time() - marks.get(key, 0) < PREPARE_COOLDOWN:
        return False
    marks[key] = time.time()
    atomic(path, marks)
    return True


def device_link(c, want="", polling=False):
    """读设备并把它准备到能装（玩家不用打开 Xcode）：配对 → 开发者模式 → 拉起隧道和开发者服务 → 必要时更新开发者磁盘映像。
    返回 {device, stage, problem, trace}：stage 是没过的环节（connected / trusted / developerMode / prepare，全过为空）；polling = 装机前清单轮询。"""
    import target
    from diag import devicectl
    trace, limits = [], (30, 20, 120) if polling else (120, 60, 240)
    def note(step, rc, raw): trace.append(dict(step=step, rc=rc, raw=_clean(raw)[-1500:]))
    def say(text):
        if not polling: c.emit("progress", text)
    def read():
        devices, why, rc, raw = target.device_list(c, timeout=30 if polling else 60)
        note("list devices", rc, raw)
        return devices, why
    def step(name, *args, timeout):
        try:
            rc, _, raw = devicectl(c, *args, timeout=timeout)
        except subprocess.TimeoutExpired:
            rc, raw = "timeout", f"超过 {timeout} 秒"
        except OSError as e:
            rc, raw = "oserror", str(e)
        note(name, rc, raw)
        return rc, raw
    def out(device, stage, problem, devices=()):
        return dict(device=device, stage=stage, problem=problem, trace=trace, devices=list(devices))
    def again(dev):
        devices, why = read()
        fresh = next((d for d in devices if d.get("identifier") == dev.get("identifier")), None)
        return devices, why, fresh
    devices, why = read()
    if why:
        return out(None, "connected", why)
    dev, problem = _pick_device(devices, want)
    if problem:
        return out(None, "connected", problem, devices)
    if dev is None:
        usb = target.usb_idevices()
        note("usb", 0, "、".join(usb) or "没有 iPhone / iPad")
        return out(None, "connected", S_NOT_LISTED if usb else S_NO_USB, devices)
    if _link(dev).get("pairingState") != "paired":
        raw = ""
        if _cooled(c, dev, "pair", polling):
            say("正在请手机信任这台 Mac")
            _, raw = step("manage pair", "manage", "pair", "--device", dev["identifier"], timeout=limits[0])
            devices, why, fresh = again(dev)
            dev = fresh or dev
        if _link(dev).get("pairingState") != "paired":
            return out(dev, "trusted", _with_raw(S_TRUST, raw), devices)
    if dev.get("deviceProperties", {}).get("developerModeStatus") not in ("enabled", None):
        return out(dev, "developerMode", S_DEV_MODE, devices)
    def services_ready(d):
        return d.get("deviceProperties", {}).get("ddiServicesAvailable") is not False
    if _link(dev).get("tunnelState") != "connected" or not services_ready(dev):
        say("正在和手机建立开发连接")
        rc, raw = step("device info details", "device", "info", "details", "--device", dev["identifier"], timeout=limits[1])
        if rc != 0:
            return out(dev, "prepare", _with_raw(S_TUNNEL, raw), devices)
        devices, why, fresh = again(dev)
        dev = fresh or dev
        if not services_ready(dev):
            raw = ""
            if _cooled(c, dev, "ddi", polling):
                say("正在准备手机的开发者服务")
                _, raw = step("manage ddis update", "manage", "ddis", "update", timeout=limits[2])
                _, raw2 = step("device info details", "device", "info", "details", "--device", dev["identifier"], timeout=limits[1])
                raw = raw2 or raw
                devices, why, fresh = again(dev)
                dev = fresh or dev
            if not services_ready(dev):
                return out(dev, "prepare", _with_raw(S_DDI, raw), devices)
    return out(dev, "", "", devices)


def _write_link_report(c, link):
    """失败时写进诊断包（私有目录/logs）：每步退出码和原文、每台设备的连接状态；序列号打码，不写设备名"""
    lines = ["# 设备连接诊断"]
    lines += [f"{t['step']}: rc={t['rc']} {t['raw']}" for t in link["trace"]]
    for d in link["devices"]:
        h, p, cp = d.get("hardwareProperties", {}), d.get("deviceProperties", {}), _link(d)
        lines.append(f"device {_mask(h.get('udid') or d.get('identifier') or '')} {h.get('productType')} reality={h.get('reality')} transport={cp.get('transportType')} pairing={cp.get('pairingState')} "
                     f"tunnel={cp.get('tunnelState')} boot={p.get('bootState')} developerMode={p.get('developerModeStatus')} ddi={p.get('ddiServicesAvailable')}")
    lines.append(f"结论: stage={link['stage']} {link['problem']}")
    target = c.private / "logs" / f"{c.task}-device.log"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def phone(c):
    import target
    link = device_link(c, (c.r.get("device") or "").strip())
    if link["problem"]:
        _write_link_report(c, link)
        raise Failure(link["problem"], code="device_not_connected" if link["stage"] == "connected" else "device_not_ready")
    return target._row(link["device"], c.env()["DEVELOPER_DIR"], with_link=True)


def udid_of(c, ident):
    from diag import devicectl
    rc, data, _ = devicectl(c, "list", "devices", timeout=60)
    for d in data.get("result", {}).get("devices", []) if rc == 0 else []:
        if d.get("identifier") == ident:
            return d.get("hardwareProperties", {}).get("udid") or ""
    return ""


def usb_mbps(udid, env=None):
    """这台 iPhone 现在的 USB 连接速率（Mb/s）：system_profiler 里设备序列号 = 去掉横杠的硬件 UDID；没插线 / 走无线 / 读不到 = 0"""
    if not udid:
        return 0
    try:
        p = subprocess.run(["system_profiler", "SPUSBHostDataType", "-json"], capture_output=True, text=True, env=env, timeout=60)
        data = json.loads(p.stdout or "{}")
    except Exception:
        return 0
    want = udid.replace("-", "").upper()
    def walk(o):
        if isinstance(o, dict):
            if str(o.get("USBDeviceKeySerialNumber", "")).replace("-", "").upper() == want:
                m = re.match(r"([\d.]+)\s*([MG])b/s", str(o.get("USBDeviceKeyLinkSpeed", "")))
                return int(float(m.group(1)) * (1000 if m.group(2) == "G" else 1)) if m else 0
            o = list(o.values())
        for v in o if isinstance(o, list) else ():
            r = walk(v)
            if r:
                return r
        return 0
    return walk(data)


# 申请描述文件 / 签名失败时 xcodebuild 的报错 → 玩家能照做的错误码
SIGN_ERRORS = (
    (r"等钥匙串授权超时", "keychain_prompt"),   # pack/ensure_profile.sh：第一次签名的钥匙串框没人点（11-08 Spare-host 实测）
    (r"needs to be unlocked|device is locked|Unlock the device", "device_locked"),   # 10-26 测试设备2 锁屏：xcodebuild 等不到目的设备超时
    (r"maximum number of (?:installed )?(?:apps|App IDs)|App ID limit|(?:maximum|limit)[^\n]{0,100}free development profiles", "free_app_limit"),
    (r"Developer\s*Mode\s*(?:is\s*)?(?:disabled|not enabled|required)|DeveloperModeDisabled", "developer_mode_required"),
    (r"pairing (?:record|relationship)[^\n]{0,80}(?:invalid|missing|expired)|not paired|pair (?:the )?device again|InvalidHostID", "device_pairing_required"),
    (r"provisioning profile[^\n]{0,100}(?:has expired|is expired|expired on)|expired provisioning profile", "provisioning_expired"),
    (r"No Account for Team|No accounts|not logged in|Your session has expired", "xcode_account_missing"),
    (r"No signing certificate|No certificate for team|Signing certificate is invalid|doesn't include signing certificate", "signing_certificate_missing"),
)


def classify(text, default="signing_failed"):
    for pat, code in SIGN_ERRORS:
        if re.search(pat, text, re.I):
            return code
    return default


def latest_ipa(c, gid):
    out = c.library / "iPhone" / gid
    ipas = sorted(out.glob("*.ipa"), key=lambda p: p.stat().st_mtime) if out.is_dir() else []
    if not ipas:
        raise Failure("这款游戏还没出 iPhone 包：先点「打 iPhone 包」。", code="job_failed")
    pack = next((p for p in sorted(out.iterdir()) if p.is_dir() and p.name.endswith("资源包")), None)
    if pack is None:   # 没有资源包就不装、不报成功（审计 18:00 P1-58：原先照装、启动器写死「资源包也拷好了」，打开游戏才进缺资源页）
        raise Failure(f"没找到这款的资源包目录（{out} 下以「资源包」结尾的文件夹），没有装。请重新「打 iPhone 包」后再装。", code="pack_missing")
    return ipas[-1], pack


def push_pack(c, ident, bid, pack, udid=None, docs=None, ctx=None):
    """资源包推进 App 并在机上解开：调 Mac / Windows 共用的 tools/push_pack.py run --apply（同 dev_run、Windows 一键装机、s2armctl push-pack，
    #206 / PR #470、11-06 两端合一）——按 sha256 只推机上没有 / 变了的文件（免费账号 7 天到期重装、改一点重出一版时只推几个文件），
    残留推送包按完整性处理、推前抽查机上实况，一批 ≤1 GB 流式写进 Documents/资源包/.incoming/；推完起一次 App 只解包（S2A_PACK_APPLY_ONLY=1），
    等机上 .applied 对上本包 index_sha256、且解包日志本次新增完成行才算装好（没推东西且 .applied 已是本包就不起 App），玩家打开就能进游戏。
    多款容器：docs = 本款设备侧文档根 Documents/games/<游戏号>、ctx = 本款启动上下文（起 App 解包时带 S2A_LAUNCH_CTX）。
    返回（推的文件数, 字节数）"""
    import diag
    want = (json.loads((pack / "manifest.json").read_text()) or {}).get("index_sha256")
    if not want:
        raise Failure(f"资源包缺清单或清单没有 index_sha256（{pack}）：请重新打 iPhone 包。", code="job_failed")
    got = {}
    def line(text):
        m = re.search(r"推 (\d+) 个文件 (\d+) MB", text)
        if m: got.update(files=int(m.group(1)), mb=int(m.group(2)))
        m = re.search(r"机上解包 (\d+)/(\d+)", text)   # tools/push_pack.py apply 每解完一包打一行：启动器显示「机上解包 13/17」
        if m: c.emit(message=f"机上解包 {m.group(1)}/{m.group(2)}", done=int(m.group(1)), total=int(m.group(2)), unit="包")
    env = dict(c.env(), S2A_DEVICECTL=str(diag.COREDEVICE / "Resources/bin/devicectl"))
    if udid: env["S2A_PUSH_UDID"] = udid   # 有 UDID + App 内置 s2a-devpush（S2A_PREBUILT_TOOLS/devpush）→ 每批流式写手机，否则退回 devicectl（11-06）
    log = c.private / "logs" / (c.task + "-push" + (f"-{docs.rsplit('/', 1)[-1]}" if docs else "") + ".log")
    extra = (["--docs", docs] if docs else []) + (["--launch-ctx", ctx] if ctx else [])
    try:
        c.run([sys.executable, "-B", c.engine / "tools/push_pack.py", "run", ident, bid, pack, "--via", "devicectl", "--apply", *extra], log, env=env, on_line=line)
    except Failure as e:
        if e.code != "tool_exit":
            raise
        text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
        m = re.search(r"资源包推进去了，但(机上解包 \d+ 秒没有新进展[^：\n]*|起 App 后 \d+ 秒机上解包没有开始)", text)   # tools/push_pack.py apply：按进度判停住（11-07）
        if m:
            raise Failure(f"资源包推进去了，但{m.group(1)}：请打开这款游戏，保持在前台等它导入完，会自动进入游戏。", code="job_failed") from e
        m = re.search(r"资源包已传完，还差最后一步：[^\n]*", text)   # tools/push_pack.py NeedsPlayer：电脑拉不起 App，传输本身成功（#192）
        if m:
            c.diagnostic(m.group(0), code="manual_unpack_required", severity="warning")
            return got.get("files", 0), got.get("mb", 0) << 20
        from errors import tool_reason
        raise Failure("资源包推不进 iPhone：" + tool_reason(log), code="resource_transfer_failed") from e
    return got.get("files", 0), got.get("mb", 0) << 20


def pull_seen(c, ident, bid, gid, tag, docs="Documents"):
    """装之前把这台设备记下的见过的键（直连线 Documents/wire/<程序号大写>/pso_user.tsv）拉回游戏库 games/<程序号>/seen/<设备>.tsv，
    下次打包按它重算两段式热键（hotset.py）；第一次装 / 没开过直连 / 拉不到都不算错。设备上是追加写：拉回的是已有那份的续写就覆盖，
    否则（换包后机上重新记了）另存一份，多份由 hotset.rank 合并去重"""
    from diag import devicectl
    dest = c.library / "games" / gid / "seen" / (re.sub(r"[^0-9A-Za-z-]", "", tag) + ".tsv")
    with tempfile.TemporaryDirectory(dir=c.private) as d:
        rc, _, _ = devicectl(c, "device", "copy", "from", "--device", ident, "--domain-type", "appDataContainer", "--domain-identifier", bid,
                             "--source", f"{docs}/wire/{gid.upper()}/pso_user.tsv", "--destination", f"{d}/s", timeout=120)
        got = Path(d, "s")
        if rc or not got.is_file():
            return False
        new = got.read_bytes()
        if dest.is_file():
            old = dest.read_bytes()
            if old.startswith(new):
                return False
            if not new.startswith(old):
                dest = dest.with_name(f"{dest.stem}-{time.strftime('%Y%m%d%H%M%S')}.tsv")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(got, dest)
        return True


SAVE_REL = "S2A/ryujinx/bis/user/save"   # 文档根下的存档目录（Ryujinx 数据根 = <文档根>/S2A/ryujinx）
SAVE_DIR = "Documents/" + SAVE_REL


def save_dir_absent(c, ident, bid, save_dir):
    """存档目录列不出时，判它是不是根本不在：从它的父目录往上逐级列到 Documents，第一个列得出的父目录里没有它（也没有它下面的东西）→ True；
    父目录里看得到它、或一个父目录都列不出（锁屏 / 隧道断）→ False（调用方停装，宁可不装也不冒丢档的险）"""
    from diag import devicectl
    parts = save_dir.split("/")
    for i in range(len(parts) - 1, 0, -1):
        parent, rest = "/".join(parts[:i]), "/".join(parts[i:])
        rc, data, _ = devicectl(c, "device", "info", "files", "--device", ident, "--domain-type", "appDataContainer", "--domain-identifier", bid,
                                "--subdirectory", parent, timeout=120)
        if rc:
            continue
        seen = [f.get("relativePath", "") for f in ((data.get("result") or {}).get("files") or [])]
        return not any(r == rest or r.startswith(rest + "/") or rest.startswith(r + "/") and r == rest.split("/")[0] for r in seen)
    return False

def backup_saves(c, ident, bid, gid, docs="Documents"):
    """覆盖装前把机上这款的整个存档目录（bis/user/save）逐文件拉回游戏库 games/<程序号>/save_backup/<时刻>/（10-27 运维线：
    斯普拉遁3 体感包覆盖装后进了选形象页，原档没有备份、无从比对）。第一次装 / 机上没有存档 = 不备份；拉失败报红停装，宁可不装也不冒丢档的险"""
    from diag import devicectl
    save_dir = f"{docs}/{SAVE_REL}"
    rc, data, err = devicectl(c, "device", "info", "files", "--device", ident, "--domain-type", "appDataContainer", "--domain-identifier", bid,
                              "--subdirectory", save_dir, timeout=120)
    files = [f["relativePath"] for f in ((data.get("result") or {}).get("files") or [])
             if not (f.get("resources") or {}).get("isDirectory") and not f["relativePath"].rsplit("/", 1)[-1].startswith(".")]
    if rc:
        # 列不出：App 不在机上（第一次装）才算没有存档；App 在却列不出要分清两种——devicectl 对「目录不存在」和「锁屏 / 隧道断」报同一个错
        #   （11-07 满屏线 Duo 实测：容器第一次装半途失败 / 从没进过游戏，机上 Documents/games 是空目录，原先每次都拒装、玩家永久卡死）。
        #   往上逐级列父目录，第一个列得出的父目录定：里面看不到存档目录 = 没有存档，照装；看得到却列不出 / 父目录一个都列不出 = 停装
        _, apps, _ = devicectl(c, "device", "info", "apps", "--device", ident, "--bundle-id", bid, timeout=60)
        if any(a.get("bundleIdentifier") == bid for a in (apps.get("result") or {}).get("apps") or []):
            if not save_dir_absent(c, ident, bid, save_dir):
                raise Failure("覆盖装前读不到 iPhone 上这款的存档目录：" + (err.splitlines()[-1] if err else f"devicectl 退出 {rc}") + "。为免丢档，这次没装；手机解锁、数据线插稳后再点一次。", code="job_failed")
        return None
    if not files:
        return None
    dest = c.library / "games" / gid / "save_backup" / time.strftime("%Y%m%d-%H%M%S")
    for rel in files:
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        rc, _, err = devicectl(c, "device", "copy", "from", "--device", ident, "--domain-type", "appDataContainer", "--domain-identifier", bid,
                               "--source", f"{save_dir}/{rel}", "--destination", str(dest / rel), timeout=120)
        if rc or not (dest / rel).is_file():
            raise Failure(f"覆盖装前备份 iPhone 上的存档失败（{rel}）：" + (err.splitlines()[-1] if err else f"devicectl 退出 {rc}") + "。为免丢档，这次没装；手机解锁、数据线插稳后再点一次。", code="job_failed")
    return dest


def readiness(c):
    """装机前清单（用户体验线 11-02）：启动器装机页每几秒问一次、AI 代劳也读它，逐项说「好了没有、没好要谁做什么」，只有 iPhone 版没做好才挡（blocking），其余只提示，真失败由 install 给原因。
    只读不改：不申请描述文件、不签名、不碰设备上的 App。每项 {id, ok, title, detail, who}；who = human（只能玩家本人做）/ app（启动器自己能做）。
    文案都是定句（启动器按原句查英文），账号名、设备名另放 account / device。
    设备一项一项判（连上 → 信任 → 开发者模式），前一项没好，后面的标 waiting、不给误导的「没打开」"""
    gid = c.r.get("gameId") or ""
    if os.name == "nt":
        return readiness_windows(c, gid)
    items = []
    ipa_path = ""
    def item(key, ok, title, detail, who="human", waiting=False):
        wait = waiting and not ok
        items.append(dict(id=key, ok=bool(ok), waiting=wait, blocking=key == "package" and not ok, title=title, detail="" if ok or wait else detail, who=who))   # 等前一步的不给说明，免得 AI 拿去催玩家
    try:
        ipa_path = str(latest_ipa(c, gid)[0])
        item("package", True, "这款游戏的 iPhone 版已经做好", "")
    except Failure:
        item("package", False, "这款游戏的 iPhone 版已经做好", "还没做好：先做 iPhone 版，做好后这里自动打勾。", who="app")
    teams = xcode_teams(c.env())
    team = (pick_team(teams) if teams else None)
    item("appleId", team, "已登录 Apple ID", "" if team else (TEAM_PENDING if xcode_account(c.env())[0] else ACCOUNT_NONE))
    link = device_link(c, (c.r.get("device") or "").strip(), polling=True)
    one, stage = link["device"], link["stage"]
    name = (one.get("deviceProperties", {}).get("name") or "").strip() if one else ""
    item("connected", one, "iPhone 已用数据线连上", link["problem"] if stage == "connected" else "")
    paired = bool(one) and stage != "trusted"
    item("trusted", paired, "iPhone 已信任这台 Mac", link["problem"] if stage == "trusted" else S_TRUST, waiting=not one)
    dev_mode = paired and stage != "developerMode"
    item("developerMode", dev_mode, "iPhone 已打开开发者模式", S_DEV_MODE, waiting=not paired)
    item("deviceReady", dev_mode and stage == "", "iPhone 已准备好安装", link["problem"] if stage == "prepare" else "", who="app", waiting=not dev_mode)
    return dict(gameId=gid, items=items, ready=not any(i["blocking"] for i in items), device=name, account=(team["name"] or team["id"]) if team else "", ipaPath=ipa_path, signing=signing_summary(c.env()))


def linked_sdk(exe):
    """主程序链接时的 iOS SDK 版本（Mach-O LC_BUILD_VERSION 的 sdk 字段，xxxx.yy.zz 按半字节编码），读不到返回 None。
    11-07 Duo 实测：iOS 按主程序链接的 SDK 判 App 认不认识本机型——旧 SDK（26.5）出的壳在 iPhone Duo（iOS 27 新机型）上按旧 iPhone 尺寸
    （667×375 点）缩小居中、四周留黑；同一份代码用 27.1 SDK 链接就铺满 2034×1398。启动屏键（UILaunchStoryboardName / UILaunchScreen）有没有都一样"""
    import struct
    try:
        with open(exe, "rb") as f:
            head = f.read(4096 * 8)
    except OSError:
        return None
    if len(head) < 32 or struct.unpack_from("<I", head)[0] != 0xFEEDFACF:   # 只认 64 位单架构（iOS 壳只出 arm64）
        return None
    ncmds, off = struct.unpack_from("<I", head, 16)[0], 32
    for _ in range(ncmds):
        if off + 8 > len(head):
            return None
        cmd, size = struct.unpack_from("<II", head, off)
        if cmd == 0x32 and off + 16 <= len(head):   # LC_BUILD_VERSION：platform, minos, sdk
            v = struct.unpack_from("<I", head, off + 16)[0]
            return (v >> 16, (v >> 8) & 0xFF, v & 0xFF)
        if size < 8:
            return None
        off += size
    return None


def check_shell_sdk(c, app, dev):
    """iPhone 的系统大版本比壳链接的 SDK 新 → 黄字 shell_sdk_old（照常装）：这一代系统新出的机型（如 iPhone Duo）会把 App 按旧尺寸缩小显示"""
    try:
        exe = app / plistlib.loads((app / "Info.plist").read_bytes()).get("CFBundleExecutable", "S2Arm")
    except (OSError, plistlib.InvalidFileException):
        return
    sdk, os_major = linked_sdk(exe), str(dev.get("os") or "").split(".")[0]
    if sdk and os_major.isdigit() and int(os_major) > sdk[0]:
        c.diagnostic(f"这个包是用 iOS {sdk[0]}.{sdk[1]} 的 Xcode 出的，这台 iPhone 是 iOS {dev.get('os')}：iOS {os_major} 新出的机型（如 iPhone Duo）"
                     "上整个 App 会按旧 iPhone 尺寸缩小在屏幕中间", code="shell_sdk_old", severity="warning")


def sign_and_install(c, ipa, team, dev, udid, bid, games, steps=True):
    """steps=False：不报主进度（出包重叠早装在后台线程里调，进度条归出包）。解 ipa → 改包名 → 申请开发描述文件 → 用玩家 Apple ID 签名 →
    按各款文档根 Documents/games/<游戏号> 拉跑局知识与存档备份 → devicectl 覆盖装（不卸载，Documents 保留）"""
    ents = c.engine / "pack/app.entitlements"
    stage = Path(tempfile.mkdtemp(prefix="s2a-install-", dir=c.private))
    log = c.private / "logs" / (c.task + "-install.log")
    try:
        # ditto 解：保留可执行位与符号链接（zipfile 会丢，codesign / 安装都要）
        r = subprocess.run(["/usr/bin/ditto", "-x", "-k", str(ipa), str(stage)], capture_output=True, text=True)
        app = next((stage / "Payload").glob("*.app"), None)
        if r.returncode or not app:
            raise Failure("解不开 iPhone 包：" + (r.stderr.strip()[-200:] or str(ipa)), code="job_failed")
        info = app / "Info.plist"
        pl = plistlib.loads(info.read_bytes()); pl["CFBundleIdentifier"] = bid
        info.write_bytes(plistlib.dumps(pl))
        check_shell_sdk(c, app, dev)
        if steps: c.begin("申请开发描述文件（第一次要几十秒）", 3, 6)
        try:
            c.run(["bash", c.engine / "pack/ensure_profile.sh", bid, team["id"], ents] + ([udid] if udid else []), log)
            if steps: c.begin("用你的 Apple ID 签名", 4, 6)
            c.run([sys.executable, c.engine / "pack/dev_sign.py", app, bid, team["id"], ents], log)
        except Failure as e:
            if e.code != "tool_exit":
                raise
            from errors import recovery, tool_reason
            text = log.read_text(errors="replace")[-8000:] if log.is_file() else ""
            code = classify(text)
            raise Failure("签名没成：" + tool_reason(log) + "；" + recovery(code, packaged=bool(c.r.get("engineBundle"))), code=code) from e
        if steps: c.begin("装到 iPhone", 5, 6)
        from diag import devicectl
        for g in games:
            pull_seen(c, dev["id"], bid, g, udid or dev["id"], docs=game_docs(g))
            backup_saves(c, dev["id"], bid, g, docs=game_docs(g))
        rc, _, err = devicectl(c, "device", "install", "app", "--device", dev["id"], str(app), timeout=1800)
        if rc:
            # 识别 devicectl 的实际原因，不把额度、配对和开发者模式失败统称签名。
            code = classify(err, default="device_install_failed")
            reason = err.strip()[-2000:] or f"devicectl 退出 {rc}（未提供错误详情）"
            raise Failure("装不上 iPhone：" + reason, code=code)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def win_install_mod(c):   # Windows 签名装机 + 推包（pack/orch/install_win.py：玩家自装的 Impactor 同版 plumesign + s2a-devpush）
    sys.path.insert(0, str(c.engine / "pack/orch"))
    import install_win
    return install_win


def win_udids(W):
    """设备服务里的手机：有 USB 连着的只算 USB 的，同网配对的不算（同 target.cable_first）"""
    devs = W.mux_devices()
    return sorted({u for u, how, _ in devs if u and how == "USB"} or {u for u, _, _ in devs if u})


def win_udid(c, W):
    """要装的那台：请求给的 device（UDID）> 设备服务里唯一连着的一台（同一台 USB + Wi-Fi 算一台）"""
    want = c.r.get("device")
    try:
        udids = win_udids(W)
    except OSError as e:
        raise Failure(f"连不上本机苹果设备服务（{e}）：从 Microsoft Store 装「Apple Devices」后重试。", code="device_pairing_required") from e
    if want:
        if want not in udids:
            raise Failure("没找到要装的那台 iPhone：插上数据线并解锁，在手机上点「信任」。", code="device_not_connected")
        return want
    if len(udids) != 1:
        raise Failure("连着不止一台 iPhone / iPad：只留要装的那一台。" if udids else
                      "没找到 iPhone：插上数据线并解锁，在手机上点「信任」。", code="device_not_connected")
    return udids[0]


def readiness_windows(c, gid):
    """装机前清单的 Windows 版：做好 iPhone 版 → Impactor（实测版本 + plumesign）→ 手机连着（设备服务里有且只有一台）；同 Mac 版的项格式"""
    W = win_install_mod(c)
    items = []
    ipa_path = ""
    def item(key, ok, title, detail, who="human", waiting=False, url=None, action=None):
        wait = waiting and not ok
        # 只有没出包才真挡；其余只提示，直接去签名，真失败再给原因
        items.append(dict(id=key, ok=bool(ok), waiting=wait, blocking=key == "package" and not ok, title=title, detail="" if ok or wait else detail, who=who,
                          url=None if ok else url,   # 玩家自装件的官方下载页（同环境检查 winenv 的 url 字段）：启动器渲染成「打开官方下载页」，不从说明文字里抠链接
                          action=None if ok else action))   # 启动器本地动作（openImpactor：打开随包的 Impactor）；只认枚举，不传路径
    try:
        ipa_path = str(latest_ipa(c, gid)[0])
        item("package", True, "这款游戏的 iPhone 版已经做好", "")
    except Failure:
        item("package", False, "这款游戏的 iPhone 版已经做好", "还没做好：先做 iPhone 版，做好后这里自动打勾。", who="app")
    resources = c.r.get("resources")
    st = W.impactor.status(resources=resources)
    if st.get("bundled"):   # 发版启动器随包（11-07）：零跳转；第一次要在 Impactor 里登录 Apple ID（密码只在 Impactor 里输、只发给 Apple，我们不代输）
        item("impactor", st["verdict"] == "ok", "签名工具 Impactor 已就绪（随 S2Arm 附带的第三方开源工具）",
             recovery("impactor_missing", packaged=True))
        item("impactor_login", st["verdict"] != "ok" or W.impactor.logged_in(), "已在 Impactor 里登录 Apple ID（只需一次）",
             "点「打开 Impactor」，在「设置 → 添加账号」登录你的 Apple ID（免费的就行），登录好回来这里自动打勾。",
             action="openImpactor")
    else:
        item("impactor", st["verdict"] == "ok" and bool(W.plumesign_path()), "Impactor 已装好并登录过 Apple ID",
             recovery(st["code"] or "impactor_missing", packaged=False), url=st["download"])
    try:
        udids = win_udids(W)
    except OSError:
        udids = []
    item("connected", len(udids) == 1, "iPhone 已用数据线连上",
         "连着不止一台 iPhone / iPad：只留要装的那一台。" if len(udids) > 1 else "用数据线把 iPhone 连到这台电脑，解锁手机并点「信任」。")
    return dict(gameId=gid, items=items, ready=not any(i["blocking"] for i in items), device=udids[0] if len(udids) == 1 else "", account="", ipaPath=ipa_path)


def latest_container(c):
    """游戏库里组装好的容器 → (ipa, 各款游戏号)（目录清单 s2a_catalogue.json 的顺序）"""
    out = c.library / "iPhone" / CONTAINER_DIR
    ipa, cat = out / "S2Arm.ipa", out / "S2Arm.app" / "s2a_catalogue.json"
    if not ipa.is_file() or not cat.is_file():
        raise Failure("这台电脑还没往 iPhone 装过游戏。", code="job_failed")
    try:
        games = [g["title_id"] for g in json.loads(cat.read_text(encoding="utf-8"))["games"]]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise Failure(f"容器的目录清单读不了（{cat}）：请重新装一次。", code="job_failed") from e
    return ipa, games


def devpush_client(c, udid, bid, work):
    """s2a-devpush（house_arrest + AFC）：两端都随发版带，devicectl 没有的「删容器里的目录」「整目录拉回」走它"""
    sys.path.insert(0, str(c.engine / "tools"))
    import push_pack as P
    prebuilt = c.engine / "Binaries/Prepare/devpush" / P.EXE
    exe = P.devpush_path(str(prebuilt) if prebuilt.is_file() else None, c.env())
    if not exe:
        raise Failure("找不到 s2a-devpush：请重新安装完整的 S2Arm。", code="tool_missing")
    return P.Devpush(exe, udid, bid, work, c.env())


class MacPhone:
    """Mac：Xcode 账号签名 + devicectl 装 + push_pack 推（出包重叠早装传入开工前认好的账号与设备）"""

    def __init__(self, c, team=None, dev=None, udid=None):
        self.c = c
        self.team = team or pick_team(xcode_teams(c.env()), c.r.get("teamId"))
        self.dev = dev or phone(c)
        self.udid = udid if udid is not None else udid_of(c, self.dev["id"])
        self.bid = container_bundle_id(self.team["id"])
        self.info = dict(device=self.dev["name"], team=self.team["name"], free=self.team["free"])

    def backup(self, g):
        backup_saves(self.c, self.dev["id"], self.bid, g, docs=game_docs(g))

    def install(self, ipa, games, steps=True):
        sign_and_install(self.c, ipa, self.team, self.dev, self.udid, self.bid, games, steps=steps)

    def before_push(self, packs):
        mbps = usb_mbps(self.udid, self.c.env())
        if 0 < mbps < 5000:   # 10-26 实测：两台测试设备经集线器只有 480 Mb/s，资源包约 31 MB/秒；USB 3 直插约 10 倍
            gb = sum(f.stat().st_size for p in packs.values() for f in p.rglob("*") if f.is_file()) / 2**30
            self.c.diagnostic(f"这台 iPhone 现在按 USB 2.0（{mbps} Mb/s）连着：资源包 {gb:.1f} GB 第一次全拷约 {max(1, round(gb * 1024 / 31 / 60))} 分钟，"
                              "换 USB 3 能快约 10 倍（以后重装只推变了的文件）", code="usb_slow", severity="warning")

    def push(self, g, pack):
        return push_pack(self.c, self.dev["id"], self.bid, pack, udid=self.udid, docs=game_docs(g), ctx=game_ctx(g))

    def purge(self, g):
        with tempfile.TemporaryDirectory(dir=self.c.private) as w:
            return devpush_client(self.c, self.udid, self.bid, w).rm(game_docs(g))


class WinPhone:
    """Windows：Impactor 同版 plumesign 签名装机（包名由它按账号定、装完才知道，记在容器目录下 bundle.txt 供下次覆盖前备份）+ s2a-devpush 备份 / 推 / 删"""

    def __init__(self, c):
        self.c, self.W = c, win_install_mod(c)
        self.udid = win_udid(c, self.W)
        self.record = c.library / "iPhone" / CONTAINER_DIR / "bundle.txt"
        self.bid = self.record.read_text(encoding="utf-8").strip() if self.record.is_file() else None
        self.devpush = c.engine / "Binaries/Prepare/devpush/s2a-devpush.exe"
        self.info = dict(device=self.udid)

    def client(self, work):
        return devpush_client(self.c, self.udid, self.bid, work)

    def backup(self, g):
        """覆盖装前备份本款存档（同 Mac backup_saves 口径：拉失败就停装）。还没装过（没记录包名 / 机上没这个 App）= 没有存档"""
        if not self.bid:
            return None
        dest = self.c.library / "games" / g / "save_backup" / time.strftime("%Y%m%d-%H%M%S")
        with tempfile.TemporaryDirectory(dir=self.c.private) as w:
            ok, out = self.client(w).get_tree(f"{game_docs(g)}/{SAVE_REL}", dest)
        if not ok and "取不到" not in out:   # 取不到 = 这个包名的 App 不在机上（s2a-devpush vend_container 的报错）
            raise Failure("覆盖装前备份 iPhone 上的存档失败：" + out[-200:] + "。为免丢档，这次没装；手机解锁、数据线插稳后再点一次。", code="job_failed")
        return dest if dest.is_dir() else None

    def install(self, ipa, games, steps=True):
        for g in games:
            self.backup(g)
        if steps: self.c.begin("签名并装到 iPhone（Impactor 官方命令行 plumesign）", 3, 6)
        b = self.W.impactor.bundled(self.c.r.get("resources")) or {}   # 发版启动器随包的 plumesign（11-07）；源码树没有 → install_win 照旧找
        log = self.c.private / "logs" / (self.c.task + "-install.log")
        self.c.last_log = log
        ns = argparse.Namespace(ipa=str(ipa), udid=self.udid, name=None, plumesign=b.get("plumesign"), log=str(log))
        try:
            self.bid = self.W.install_app(ns, say=lambda m: self.c.emit("message", m))
        except self.W.Fail as e:
            raise Failure(str(e), code=e.err or "device_install_failed") from e
        self.record.write_text(self.bid, encoding="utf-8")

    def before_push(self, packs):
        pass

    def push(self, g, pack):
        try:
            return self.W.push_pack(self.udid, self.bid, str(pack), str(self.devpush) if self.devpush.is_file() else None,
                                    say=lambda m: self.c.emit("message", m), docs=game_docs(g), ctx=game_ctx(g), apply_after=True)
        except self.W.Fail as e:
            if e.err == "manual_unpack_required":
                self.c.diagnostic(str(e), code=e.err, severity="warning")
                return e.transferred
            raise Failure(str(e), code=e.err or "resource_transfer_failed") from e

    def purge(self, g):
        with tempfile.TemporaryDirectory(dir=self.c.private) as w:
            return self.client(w).rm(game_docs(g))


def phone_for(c):
    return WinPhone(c) if os.name == "nt" else MacPhone(c)


def put_game(c, gid, ph, steps=True):
    """把这款放进容器并装到手机：组装（已有各款原样带过来）→ 覆盖装 → 只推本次组装的款的资源包（其余款的数据和存档不动）"""
    import ipa as packer
    latest_ipa(c, gid)   # 这款没有资源包就不组装、不装
    if steps: c.begin("把这款放进手机上的 S2Arm", 2, 6)
    ipa, games, fresh = packer.assemble(c, gid)
    packs = {g: latest_ipa(c, g)[1] for g in fresh}   # 本次组装的每款都要有资源包，缺一款就不装（不装半套）
    ph.install(ipa, games, steps=steps)
    ph.before_push(packs)
    if steps: c.begin("把资源包推进 iPhone 并解开（只推变了的文件；第一次大游戏要几分钟）", 6, 6)
    files = size = 0
    for g in fresh:
        f, n = ph.push(g, packs[g])
        files += f; size += n
    if "free" in ph.info:   # 记下这次签名（游戏库页的「还有几天到期 / 续签」读 games/<id>/ios-install.json）：免费账号的描述文件 7 天到期，容器里每款同一次签名
        for g in games:
            record = contained(c.library, c.library / "games" / g / "ios-install.json")
            record.parent.mkdir(parents=True, exist_ok=True)
            atomic(record, dict(at=time.time(), free=bool(ph.info["free"]), device=ph.info["device"]))
    return dict(outputPath=str(ipa.parent), bundleId=ph.bid, games=games, packFiles=files, packBytes=size, **ph.info)


def install(c):
    """默认「装到 iPhone」：这款进容器（一个 App 装下整个游戏库，免费账号也只占 1 个 App 额度）。加款 = 同一个包名覆盖安装，Documents 保留"""
    gid = c.r.get("gameId") or ""
    latest_ipa(c, gid)
    c.begin("找签名账号与 iPhone", 1, 6)
    return put_game(c, gid, phone_for(c))


def remove_from_phone(c):
    """从手机上的 S2Arm 里删一款，只动这一款：备份本款存档 → 容器摘掉本款（其余各款逐字节不变、不换壳）→ 覆盖装 → 删机上本款文档根。
    新容器装上之后才替换游戏库里的那份（装失败时两边还一致）；机上目录没删掉只报黄字（App 里这款已经没了，占的空间下次再清）"""
    import ipa as packer
    gid = (c.r.get("gameId") or "").upper()
    _, games = latest_container(c)
    if gid not in games:
        raise Failure("手机上的 S2Arm 里没有这款。", code="job_failed")
    if len(games) == 1:
        raise Failure("这是手机上最后一款：在 iPhone 桌面长按 S2Arm 删除 App 即可。", code="container_last_game")
    c.begin("找签名账号与 iPhone", 1, 4)
    ph = phone_for(c)
    if not ph.bid:
        raise Failure("这台电脑没记录手机上的 S2Arm：先装一次再删。", code="job_failed")
    c.begin("备份这款的存档", 2, 4)
    backup = ph.backup(gid)
    c.begin("更新手机上的 S2Arm", 3, 4)
    with packer.dropped(c, gid) as (ipa, left):
        ph.install(ipa, left, steps=False)
        packer.commit_drop(c, ipa)
    c.begin("删除手机上这款的数据", 4, 4)
    ok, out = ph.purge(gid)
    if not ok:
        c.diagnostic("App 里这款已经删了，但手机上它的数据没删掉：" + out[-200:], code="container_purge_failed", severity="warning")
    return dict(games=left, bundleId=ph.bid, removed=gid, saveBackup=str(backup) if backup else "", dataRemoved=ok, **ph.info)
