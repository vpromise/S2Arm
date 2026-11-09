#!/usr/bin/env python3
# 门禁覆盖：pack/* pack/jobs/core.py pack/jobs/community.py pack/rewriter.py pack/fonts/* graphics/tools/* graphics/metal/tools/* graphics/widen/scan/* services/*.sh runtime/run_mac.sh runtime/rewriter/* launchers/macos/*.py launchers/macos/*.sh tools/machine.sh 维护方工具 tools/mac_save.sh 维护方工具 tools/gc.sh 维护方工具 tools/cores.sh tools/progress.sh tools/toolchain.sh tools/nospace.sh tools/mem_admit.sh tools/csproj_closure.py tools/doctor.sh tools/stage_deps.py tools/push_pack.py
# 玩家机上出包不许写 ~/.s2arm/run（10-26 交付线：Spare-host 斯普拉遁3 出包后数据目录外多出直连缓存 6.5 GB + 翻译缓存 5.3 GB，清数据目录 / 删游戏都清不到）：
#   出包链脚本里落到 ~/.s2arm/run 的缺省必须经 S2A_RUN_ROOT（bash `${S2A_RUN_ROOT:-$HOME/.s2arm/run}`、Python `os.environ.get('S2A_RUN_ROOT') or …`），
#   启动器作业环境（pack/jobs/core.py Context.env）把 S2A_RUN_ROOT 指到私有目录下。我方开发机不设，照旧 ~/.s2arm/run。
# 11-07 启动器零配置（联合开发者反馈：direct_lib.sh 回退 ~/.s2arm/venv、改写器依赖报错列 ${S2A_RUN_ROOT:-$HOME/.s2arm/run}、字体依赖提示指向 ~/.s2arm/venv）：
#   玩家路径（出包 / 起局链）与「从源码构建」路径不许出现开发机专用路径：~/.s2arm 下的 venv / data / toolchain / S2Arm / wt / build / gate_data / sw、
#   s2arm-wt、.s2arm/rw、/Users/<我方账号>。开发机靠 tools/machine.local / 环境变量显式声明（bash tools/machine.sh get <名>），代码里不写死。
# 期望（按规则手写）：① 扫出包链 + 构建路径全部 .sh / .py（不含 tests/ 与测试 / 门禁壳）的非注释行，裸写 ~/.s2arm/run 的 0 处；远端机路径（$RH / {home}，gate-host 上的队列）不算
#   ② 反证：扫描函数对一段裸写的样例报 1 处、对包过的样例报 0 处（证明 ① 测得到）③ Context.env 给的 S2A_RUN_ROOT 在私有目录下
#   ④ 10-26 维护者（侍魂晓被我方 130 G 磁盘闸 + 独占局白卡 43 分钟）：Context.env 写死 S2A_MACHINE_NO_LOCAL=1、S2A_CPU_LEDGER=0，父进程环境给了 0 / 1 也盖掉
#   ⑤ 同一范围的非注释行，开发机专用路径 0 处（白名单只 WHITE 里那几行，各写理由）⑥ 反证：venv / data / 账号路径样例各报 1 处，注释行报 0 处
#   ⑦ 行为：假家目录里放一份可执行的 .s2arm/venv/bin/python3、社区补丁目录，不设 S2A_PY / S2A_COMMUNITY_DIR、不读 machine.local →
#      出包链取解释器（pack/stage_keyfiles.sh _sd_py）得 python3、不是假 venv；启动器社区索引（pack/jobs/community.py）为空；
#      machine.sh get 按 环境 > machine.local > 空 的顺序取值，S2A_MACHINE_NO_LOCAL=1 时不读文件
#   ⑧ 行为：C 改写器固定依赖全缺时报错列出实际查过的路径（在 S2A_RUN_ROOT 下）并指向一键准备 prepare_build.py，不出现「$HOME」字面量
#   扫描范围 = refresh.sh 及它真正调到的脚本（runtime/run_mac.sh 经直连抽取的 Vulkan 抓取局、维护方工具 / mac_save / 维护方工具 经 run_mac.sh、维护方工具 经出包入口的盘判定、
#     mem_admit 经 dsh_batch.py build、csproj_closure 经 shader_pack.sh）+ 从源码构建（launchers/macos 的 build_app / bundle_* / runtime/rewriter / pack/fonts / tools/doctor.sh）
import os, re, subprocess, sys, tempfile
from pathlib import Path
R = Path(__file__).resolve().parent.parent
bad = 0
def ck(ok, what):
    global bad
    print(("通 " if ok else "不通 ") + what); bad += 0 if ok else 1

SH = re.compile(r'(?<!S2A_RUN_ROOT:-)\$HOME/.s2arm/run\b|(?<![\w{"])~/.s2arm/run\b')   # 双引号里的 ~ 本机不展开 = 交给远端（gate-host）的路径，不算
PY = re.compile(r"""os\.path\.expanduser\(\s*['"]~/.s2arm/run|Path\.home\(\)\s*/\s*['"].s2arm/run""")
def code_part(name, l):
    """去掉行尾注释（bash「 #」、Python「  #」），整行注释返回空"""
    s = l.strip()
    if s.startswith('#'): return ''
    sep = '  #' if name.endswith('.py') else ' #'
    return l.split(sep)[0] if sep in l else l
def hits(name, text):
    out = []
    for i, l in enumerate(text.split('\n'), 1):
        s = l.strip()
        if not s or s.startswith('#') or '$RH/' in l or '{home}/' in l: continue
        if name.endswith('.py'):
            for m in PY.finditer(l):
                if "S2A_RUN_ROOT" not in l[max(0, m.start() - 60):m.start()]: out.append((i, s[:120]))
        elif SH.search(l.split(' #')[0] if ' # ' in l else l): out.append((i, s[:120]))
    return out

# ⑤ 开发机专用路径：样例与模式运行时拼（字面量会被共享路径检查 tests/shared_path_test.sh 与公开仓库扫描当成真路径）
NX = '.s2arm'
DEV = re.compile(NX + r'/(venv|data|toolchain|S2Arm|wt|build|gate_data|sw)\b|s2arm-wt\b|' + NX + r'-rw\b|/Users/' + 'acct' + r'\b')
# 白名单（文件, 行内必含片段）：只放开发机配置本身，每条写理由
WHITE = [
    # 开发机配置定位器：各线工作树 / 冻结副本找主检出的 machine.local；S2A_MACHINE_NO_LOCAL=1（启动器作业环境恒设）时整段不跑，玩家机上这个文件也不存在
    ('tools/machine.sh', '/S2Arm/tools/machine.local'),
    # 开发机运维（各线工作树 / 记分板 / 主检出清理，维护方工具 远端段与本机巡检调）：出包 / 起局链不调它（只在注释里被提到），玩家机不跑；
    #   在 ① 的范围里是因为它清的缓存目录要与出包链同经 S2A_RUN_ROOT，这条只放开它的开发机目录那几行
    ('tools/gc.sh', '/S2Arm'), ('tools/gc.sh', 's2arm-wt'), ('tools/gc.sh', NX + '/wt'),
]
def dev_hits(name, text):
    out = []
    for i, l in enumerate(text.split('\n'), 1):
        c = code_part(name, l)
        if not c.strip() or '$RH/' in c or '{home}/' in c: continue
        if DEV.search(c) and not any(name == f and frag in c for f, frag in WHITE): out.append((i, l.strip()[:120]))
    return out

SCOPE = ['pack', 'graphics/tools', 'graphics/metal/tools', 'graphics/widen/scan', 'services', 'runtime/run_mac.sh', 'runtime/rewriter', 'launchers/macos',
         'tools/machine.sh', 'tools/mac_slot.sh', 'tools/mac_save.sh', 'tools/cost_log.sh', 'tools/gc.sh', 'tools/disk_low.sh', 'tools/cores.sh', 'tools/progress.sh',
         'tools/toolchain.sh', 'tools/nospace.sh', 'tools/mem_admit.sh', 'tools/csproj_closure.py', 'tools/doctor.sh', 'tools/stage_deps.py', 'tools/push_pack.py']
files = [f for f in subprocess.run(['git', '-C', str(R), 'ls-files', *SCOPE], capture_output=True, text=True, check=True).stdout.split()
         if re.search(r'\.(sh|py)$', f) and not re.search(r'(^|/)tests?/|(^|/)(test|gate)\.sh$|_test\.(sh|py)$', f)
         and not f.startswith('launchers/macos/Tests/')]
found = [(f, i, s) for f in files for i, s in hits(f, (R / f).read_text(errors='replace'))]
for f, i, s in found[:10]: print(f'    {f}:{i}: {s}')
ck(not found, f'① 出包链 + 构建路径 {len(files)} 个脚本裸写 ~/.s2arm/run：{len(found)} 处（期望 0）')
RUN = '.s2arm/' + 'run'   # 样例运行时拼：字面量会被共享路径检查（tests/shared_path_test.sh）当成真路径
ck(len(hits('x.sh', 'C=${X:-$HOME/' + RUN + '/s2arm/cache}/a')) == 1 and not hits('x.sh', 'C=${X:-${S2A_RUN_ROOT:-$HOME/' + RUN + '}/s2arm/cache}/a')
   and len(hits('x.py', "d = os.path.expanduser('~/" + RUN + "/x')")) == 1 and not hits('x.py', "d = os.path.join(os.environ.get('S2A_RUN_ROOT') or os.path.expanduser('~/" + RUN + "'), 'x')"),
   '② 反证：裸写的 bash / Python 样例各报 1 处，包过的报 0 处')
devf = [(f, i, s) for f in files for i, s in dev_hits(f, (R / f).read_text(errors='replace'))]
for f, i, s in devf[:10]: print(f'    {f}:{i}: {s}')
ck(not devf, f'⑤ 同 {len(files)} 个脚本的非注释行出现开发机专用路径：{len(devf)} 处（期望 0；白名单 {len(WHITE)} 条）')
V = NX + '/' + 'venv'
ck(len(dev_hits('x.sh', 'PY=${S2A_PY:-$HOME/' + V + '/bin/python3}')) == 1 and len(dev_hits('x.py', 'd = Path.home() / "' + NX + '/data/community_patches"')) == 1
   and len(dev_hits('x.sh', 'B=/Users/' + 'acct/x')) == 1 and len(dev_hits('x.sh', 'PY=$HOME/' + V + '/bin/python3   # 说明')) == 1
   and not dev_hits('x.sh', '# 缺省 ~/' + V) and not dev_hits('x.sh', 'echo hi   # 旧缺省 ~/' + V) and not dev_hits('x.py', 'x = 1  # 旧缺省 ~/' + V)
   and len(dev_hits('tools/machine.sh', 'for f in "$HOME/' + NX + '/S2Arm/tools/machine.local"; do')) == 0
   and len(dev_hits('tools/machine.sh', 'PY=$HOME/' + V + '/bin/python3')) == 1,
   '⑥ 反证：venv / data / 账号路径样例各报 1 处（行尾带注释的照报）、注释报 0 处；白名单只放 machine.sh 那一行，同文件别的写死照报')

sys.path.insert(0, str(R / 'pack/jobs'))
import core
with tempfile.TemporaryDirectory() as t:
    class C: pass
    c = C(); c.private = Path(t) / 'S2Arm 私有'; c.private.mkdir(); c.work_root = c.private; c.r = {}; c.default_developer = None; c.engine = Path(t) / 'engine'; c.task = 't'
    os.environ.update(S2A_MACHINE_NO_LOCAL='0', S2A_CPU_LEDGER='1')   # 模拟我方开发机的外层环境：作业环境要盖掉
    try: env = core.Context.env(c)
    except Exception as e: env = {}; print('    env 失败：', type(e).__name__, e)
    rr = env.get('S2A_RUN_ROOT', '')
    ck(rr.startswith(str(c.private)), f'③ 启动器作业环境 S2A_RUN_ROOT={rr or "（没设）"}，在私有目录下')
    ck(env.get('S2A_MACHINE_NO_LOCAL') == '1' and env.get('S2A_CPU_LEDGER') == '0',
       f"④ 启动器作业环境不读我方 machine.local：S2A_MACHINE_NO_LOCAL={env.get('S2A_MACHINE_NO_LOCAL')}、S2A_CPU_LEDGER={env.get('S2A_CPU_LEDGER')}（期望 1 / 0）")

with tempfile.TemporaryDirectory() as t:
    T = Path(t); home = T / 'home'; venv = home / NX / 'venv/bin'; venv.mkdir(parents=True)
    (venv / 'python3').write_text('#!/bin/sh\necho fake-dev-venv\n'); (venv / 'python3').chmod(0o755)
    cp = home / NX / 'data/community_patches/p/full/n'; cp.mkdir(parents=True); (cp / 'INDEX.tsv').write_text('title_id\n')
    base = {k: v for k, v in os.environ.items() if not k.startswith(('S2A_', 'PYTHON'))}
    clean = dict(base, HOME=str(home), S2A_MACHINE_NO_LOCAL='1')
    got = subprocess.run(['bash', '-c', 'source "$1/pack/stage_keyfiles.sh"; _sd_py', '_', str(R)], env=clean, capture_output=True, text=True).stdout.strip()
    ml = T / 'm.local'; ml.write_text('S2A_PY=$HOME/from-machine-local\nS2A_CPU_LEDGER=1\n')
    def mget(extra):
        return subprocess.run(['bash', str(R / 'tools/machine.sh'), 'get', 'S2A_PY'], env=dict(base, HOME=str(home), **extra), capture_output=True, text=True).stdout.strip()
    m_env, m_file, m_no, m_none = (mget(dict(S2A_PY='/env/py', S2A_MACHINE_LOCAL=str(ml))), mget(dict(S2A_MACHINE_LOCAL=str(ml))),
                                   mget(dict(S2A_MACHINE_LOCAL=str(ml), S2A_MACHINE_NO_LOCAL='1')), mget(dict(S2A_MACHINE_LOCAL=str(T / 'none'))))
    idx = subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import community; print(len(community._indexes()))', str(R / 'pack/jobs')],
                         env=clean, capture_output=True, text=True)
    ck(got == 'python3' and m_env == '/env/py' and m_file == str(home / 'from-machine-local') and m_no == '' and m_none == '' and idx.stdout.strip() == '0',
       f'⑦ 假家目录有 ~/{NX}/venv 与社区补丁、没声明：_sd_py={got!r}（期望 python3）；machine.sh get 环境 / 文件 / NO_LOCAL / 无 = '
       f'{m_env!r} / {m_file!r} / {m_no!r} / {m_none!r}；社区索引 {idx.stdout.strip() or idx.stderr.strip()[-80:]!r}（期望 0）')
    rr = T / 'run'
    probe = ('import sys; sys.path.insert(0, sys.argv[1]); import rewriter; from pathlib import Path; rewriter.ROOT = Path(sys.argv[2])\n'
             'try: rewriter.dependency_dir(); print("找到了")\nexcept ValueError as e: print(e)')
    msg = subprocess.run([sys.executable, '-c', probe, str(R / 'pack'), str(T / 'repo')], env=dict(base, HOME=str(home), S2A_RUN_ROOT=str(rr)),
                         capture_output=True, text=True).stdout.strip()
    ck('缺 C 改写器固定依赖' in msg and str(rr / 's2arm/deps/rewriter-deps') in msg and str(T / 'repo/build/rewriter-deps') in msg
       and 'prepare_build.py' in msg and '$HOME' not in msg, f'⑧ 改写器依赖全缺的报错：{msg[:160]}…')
print(f"{'✓' if bad == 0 else '✗'} userpath_cache：不通 {bad}"); sys.exit(1 if bad else 0)
