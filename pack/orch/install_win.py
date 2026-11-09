#!/usr/bin/env python3
"""Windows 签名装机 + 推资源包，一条命令、不用点（11-06 Windows 端到端线：维护者「3 款纯 Windows 一键出包、装机、进局，维护者 不用动手」）。
签名装机用玩家自己装的 Impactor 同版本官方命令行 plumesign（维护者 11-04/11-05 定：不内嵌、不冒充，用原版；账号是玩家在 Impactor 里登过的那份，
  plumesign 与图形版共用 %APPDATA%\\PlumeImpactor 账号存档，登过一次就不再要密码）；推资源包用两端共用的 tools/push_pack.py（Windows 上走 s2a-devpush：house_arrest + AFC）。

  python -X utf8 -m pack.orch.install_win <ipa> <资源包目录> --udid <手机 UDID> [--name <桌面显示名>] [--plumesign <路径>] [--devpush <路径>] [--full]

步骤：① 按 UDID 在本机设备服务（usbmuxd）里找设备编号（plumesign 的 --udid 实际比的是设备编号，不是 UDID；USB 优先，Wi-Fi 也认）；
  ② plumesign sign --apple-id --register-and-install（登记设备、注册包名、签名、装机）并 -o 存一份签好的 ipa，从里面读装机后的实际包名（免费账号会加团队号后缀）；
  ③ tools/push_pack.py 按实际包名推资源包（同 Mac 启动器、dev_run 一份规则）。
退出码：0 装好并推完；1 失败（最后一行【红】带共用错误码，见 pack/jobs/errors.py）；2 用法；75 缺 plumesign / 设备服务里没这台手机。
实测（11-06 Windows 出包机 + 测试设备2，Wi-Fi）：金刚 c3 ipa 46 MB 签名 + 装机 48 秒。"""
import argparse, glob, os, plistlib, re, shutil, socket, struct, subprocess, sys, tempfile, zipfile
from pathlib import Path

R = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(R / 'pack/jobs')); sys.path.insert(0, str(R))
import impactor   # noqa: E402  找玩家装的 Impactor、报错 → 共用错误码


class Fail(Exception):
    def __init__(self, msg, code=1, err=None): super().__init__(msg); self.code = code; self.err = err


def plumesign_path(given=None):
    """plumesign 位置：指定 > S2A_PLUMESIGN > PATH > 玩家 Impactor 同目录 > 桌面 / 下载"""
    exe = 'plumesign.exe' if os.name == 'nt' else 'plumesign'
    cands = [given, os.environ.get('S2A_PLUMESIGN'), shutil.which('plumesign')]
    st = impactor.status()
    if st.get('path'): cands += glob.glob(os.path.join(os.path.dirname(st['path']), 'plumesign*'))
    home = os.path.expanduser('~')
    for d in ('Desktop', 'Downloads', os.path.join('OneDrive', 'Desktop')): cands += sorted(glob.glob(os.path.join(home, d, 'plumesign*')), reverse=True)
    for c in cands:
        if c and os.path.isfile(c) and (os.name != 'nt' or c.lower().endswith('.exe')): return c
    return None


def mux_devices():
    """本机设备服务列出的设备 → [(UDID, 连接方式, 设备编号)]"""
    if os.name == 'nt': s = socket.create_connection(('127.0.0.1', 27015), 5)
    else: s = socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect('/var/run/usbmuxd')
    with s:
        p = plistlib.dumps({'MessageType': 'ListDevices', 'ClientVersionString': 'S2Arm-install', 'ProgName': 'S2Arm-install', 'kLibUSBMuxVersion': 3})
        s.sendall(struct.pack('<IIII', 16 + len(p), 1, 8, 1) + p)
        h = b''
        while len(h) < 16: h += s.recv(16 - len(h))
        n = struct.unpack('<I', h[:4])[0] - 16; b = b''
        while len(b) < n:
            c = s.recv(n - len(b))
            if not c: break
            b += c
    out = []
    for d in plistlib.loads(b).get('DeviceList', []):
        pr = d.get('Properties', {}); out.append((pr.get('SerialNumber'), pr.get('ConnectionType'), d.get('DeviceID')))
    return out


def device_id(udid, devs):
    hit = [d for d in devs if d[0] == udid]
    hit.sort(key=lambda d: d[1] != 'USB')   # 同一台既插线又在 Wi-Fi：走 USB
    return hit[0][2] if hit else None


def bundle_id_of(ipa):
    with zipfile.ZipFile(ipa) as z:
        n = next(x for x in z.namelist() if re.fullmatch(r'Payload/[^/]+\.app/Info\.plist', x))
        return plistlib.loads(z.read(n))['CFBundleIdentifier']


def sign_install(exe, ipa, dev_id, name, log, out_ipa):
    cmd = [exe, 'sign', '--package', str(ipa), '--apple-id', '--register-and-install', '--udid', str(dev_id), '-o', str(out_ipa)]
    if name: cmd += ['--custom-name', name]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', env=dict(os.environ, RUST_LOG=os.environ.get('RUST_LOG', 'info')))
    text = p.stdout + p.stderr
    Path(log).write_text(text, encoding='utf-8')
    if p.returncode or 'Installation complete' not in text:
        tail = [ln for ln in text.splitlines() if ln.strip() and 'DEBUG' not in ln][-1:] or ['（没有输出）']
        code = impactor.explain(text)
        raise Fail(f'签名装机失败（{code}）：{tail[0][-300:]}', 1, code)


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, 'reconfigure'): s.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('ipa'); ap.add_argument('pack'); ap.add_argument('--udid', required=True); ap.add_argument('--name')
    ap.add_argument('--plumesign'); ap.add_argument('--devpush'); ap.add_argument('--full', action='store_true'); ap.add_argument('--log')
    a = ap.parse_args(argv)
    try:
        print(f'🟢 已装好：{run(a)}', flush=True); return 0
    except Fail as e:
        print(f'【红】{e}', flush=True); return e.code


def install_app(a, say=lambda m: print(m, flush=True)):
    """签名 + 覆盖装（同包名不卸载，Documents 保留）→ 装机后的实际包名；失败抛 Fail"""
    exe = plumesign_path(a.plumesign)
    if not exe: raise Fail('找不到 plumesign（Impactor 官方同版命令行，和 Impactor 放在同一目录，或用 --plumesign / S2A_PLUMESIGN 指定）', 75, 'impactor_missing')
    try: devs = mux_devices()
    except OSError as e: raise Fail(f'连不上本机苹果设备服务（{e}）：装 iTunes 或「Apple 设备」', 75, 'device_pairing_required')
    dev = device_id(a.udid, devs)
    if dev is None: raise Fail(f'设备服务里没有 {a.udid}（在列：{", ".join(f"{u}/{c}" for u, c, _ in devs) or "无"}）：插上线并解锁，或打开 Wi-Fi 同步', 75, 'device_pairing_required')
    log = a.log or os.path.join(os.path.dirname(os.path.abspath(a.ipa)), 'plumesign.log')
    with tempfile.TemporaryDirectory() as t:
        out = Path(t, 'signed.ipa'); say(f'签名 + 装机（设备编号 {dev}）…')
        sign_install(exe, a.ipa, dev, a.name, log, out)
        bid = bundle_id_of(out) if out.is_file() else None
    if not bid: raise Fail('装好了但读不到签好的 ipa 里的包名，推不了资源包', 1, 'device_install_failed')
    return bid


def run(a, say=lambda m: print(m, flush=True)):
    """→ 装机后的实际包名；失败抛 Fail（oneclick_win --install 同用）"""
    bid = install_app(a, say)
    say(f'推资源包 → {bid}')
    push_pack(a.udid, bid, a.pack, a.devpush, a.full, say)
    return bid


def push_pack(udid, bid, pack, devpush=None, full=False, say=lambda m: print(m, flush=True), docs=None, ctx=None, apply_after=False):
    """推资源包到已装 App（Windows 没有 devicectl：s2a-devpush 走本机苹果设备服务 usbmuxd + house_arrest + AFC）→ (文件数, 字节数)；失败抛 Fail。
    容器：docs / ctx = 本款设备侧文档根 / 启动上下文；apply_after = 推完起一次 App 只解包并等解完（同 Mac --apply）"""
    sys.path.insert(0, str(R / 'tools')); import push_pack as P   # Mac / Windows 共用的推资源包 tools/push_pack.py（11-06 两端合一）
    with tempfile.TemporaryDirectory(dir=os.environ.get('S2A_PUSH_TMP')) as w:
        try:
            dev = P.device(udid, bid, w, udid, devpush, 'devpush', say=say, **({'docs': docs, 'ctx': ctx} if docs else {}))
            nb, nt, nbytes = P.run(dev, Path(pack), Path(w), full, say)
            if apply_after:
                try: P.apply(dev, Path(pack), Path(w), nb, say)
                except P.NeedsPlayer as e:   # 传输是成功的：带上传了多少，装机作业按提醒收尾（#192）
                    need = Fail(str(e), 0, 'manual_unpack_required'); need.transferred = (nt, nbytes); raise need
        except P.Fail as e:
            raise Fail(f'推资源包失败：{e}', 1, 'device_install_failed')
    return nt, nbytes


if __name__ == '__main__':
    sys.exit(main())
