#!/usr/bin/env python3
"""Android delivery boundaries without SDK, game files or a device."""
import hashlib
import gzip
import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import struct
import shutil
import tempfile
import unittest
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('android_oneclick', ROOT / 'pack/targets/android/package.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def provider_elf(build_id=b"A" * 20):
    note = struct.pack('<III', 4, len(build_id), 3) + b'GNU\0' + build_id
    header = bytearray(64)
    header[:16] = b'\x7fELF\x02\x01\x01' + b'\0' * 9
    struct.pack_into('<HHIQQQIHHHHHH', header, 16, 3, 183, 1, 0, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    ph = struct.pack('<IIQQQQQQ', 4, 4, 120, 0, 0, len(note), len(note), 4)
    return bytes(header) + ph + note


def write_provider_apk(path, provider):
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('lib/arm64-v8a/libs2arm_android.so', provider)


class SynthDeterminismTest(unittest.TestCase):
    def test_ngword_gzip_has_stable_header_and_payload(self):
        spec = importlib.util.spec_from_file_location('sysdata_synth', ROOT / 'pack/sysdata_synth.py')
        synth = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(synth)
        files = synth.ngword()
        data = files['ac_0_b1_nx']
        self.assertEqual(data.hex(), '1f8b08000000000002ff63604003ffa180811c408a664632f531a1e86544a29131020000bfa0252ce2000000')
        self.assertEqual(gzip.decompress(data), synth.aho_corasick_empty())
        self.assertEqual(gzip.decompress(files['table_similar_form_nx']), synth.similar_form_table_empty())
        self.assertTrue(all(value[9] == 255 for name, value in files.items() if name != 'version.dat'))


class AndroidDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.stage = self.base / 'stage'
        self.stage.mkdir()
        self.apk = self.stage / 'game.apk'
        self.apk.write_bytes(b'fixture-apk')
        self.resources = self.stage / 'resources'
        self.resources.mkdir()
        (self.resources / 'manifest.json').write_text('{}')
        self.identity = dict(target='android', titleId='0100000000000000', contentRoot='sha256-' + '1' * 64,
                             apk=dict(sha256=hashlib.sha256(self.apk.read_bytes()).hexdigest(),
                                      signingCertSha256='2' * 64))
        self.write_artifact()

    def write_artifact(self):
        (self.resources / 'game-build-artifact.json').write_text(json.dumps(self.identity))

    def test_patch_environment_cannot_change_child_output(self):
        patch_file = self.base / 'patch.tsv'; patch_file.write_bytes(b'changed')
        source = "import os,pathlib,sys; p=os.environ.get('S2A_CPATCH_TSV'); sys.stdout.buffer.write(b'base'+(pathlib.Path(p).read_bytes() if p else b''))"
        clean = dict(os.environ); clean.pop('S2A_CPATCH_TSV', None)
        dirty = dict(clean, S2A_CPATCH_TSV=str(patch_file), S2A_REWRITER='python',
                     S2A_REWRITER_C='/invalid', S2A_REWRITER_BUNDLED='/invalid', PYTHONPATH='/invalid')
        def child(env):
            return subprocess.check_output([sys.executable, '-I', '-c', source], env=env)
        baseline = child(mod.build_environment(clean))
        self.assertEqual(child(mod.build_environment(dirty)), baseline)
        self.assertNotEqual(child(dirty), baseline)  # removing the whitelist is a real red control

    def test_docker_builder_forwards_direct_backend_to_container(self):
        commands = self.base / 'bin'; commands.mkdir()
        capture = self.base / 'docker-args'
        docker = commands / 'docker'
        docker.write_text('#!/bin/sh\nif [ "$1" = image ]; then exit 0; fi\nprintf "%s\\n" "$@" > "$CAPTURE_PATH"\n')
        docker.chmod(0o755)
        env = dict(os.environ, PATH=str(commands) + ':' + os.defpath, CAPTURE_PATH=str(capture),
                   S2ARM_ANDROID_VK_ENGINE='1', S2ARM_ANDROID_VK_BACKEND='nvn_vk')
        run = subprocess.run(['bash', str(ROOT / 'android/docker/build.sh'), ':app:assembleDebug'],
                             env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        arguments = capture.read_text().splitlines()
        self.assertIn('S2ARM_ANDROID_VK_ENGINE=1', arguments)
        self.assertIn('S2ARM_ANDROID_VK_BACKEND=nvn_vk', arguments)

    def test_gui_bundled_rewriter_is_derived_from_python_runtime_not_environment(self):
        spec = importlib.util.spec_from_file_location('local_rewriter', ROOT / 'pack/rewriter.py')
        rewriter = importlib.util.module_from_spec(spec); spec.loader.exec_module(rewriter)
        runtime = self.base / 'S2Arm.app/Contents/Resources/Runtime'
        py = runtime / 'python/bin/python3'; py.parent.mkdir(parents=True); py.touch()
        binary = runtime / 'bin/s2a-rewrite'; binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/sh\nexit 0\n'); binary.chmod(0o755)
        stamp = binary.with_name(binary.name + '.source'); stamp.write_text(rewriter.source_digest() + '\n')
        attacker = self.base / 'external-rewriter'; attacker.write_text('#!/bin/sh\nexit 0\n'); attacker.chmod(0o755)
        attacker.with_name(attacker.name + '.source').write_text(rewriter.source_digest() + '\n')
        env = mod.build_environment({'HOME': str(self.base), 'PATH': os.defpath,
                                     'S2A_REWRITER_BUNDLED': str(attacker), 'S2A_REWRITER_C': str(attacker)})
        self.assertNotIn('S2A_REWRITER_BUNDLED', env); self.assertNotIn('S2A_REWRITER_C', env)
        # #1207 复审二轮：local.properties 不进 git archive 导出树——解析出的 SDK
        #   路径必须显式进构建环境，否则非默认 sdk.dir 的用户「验签找得到、构建找不到」。
        bare = {'HOME': str(self.base), 'PATH': os.defpath}
        self.assertEqual(mod.build_environment(bare)['ANDROID_HOME'],
                         mod.android_sdk_home(bare))
        rooted = dict(bare, ANDROID_SDK_ROOT='/opt/custom-sdk')
        self.assertEqual(mod.build_environment(rooted)['ANDROID_HOME'], '/opt/custom-sdk')
        with patch.object(sys, 'executable', str(py)), patch.dict(os.environ, env, clear=True), \
             patch.object(rewriter.subprocess, 'run', side_effect=AssertionError('must not build')):
            self.assertEqual(rewriter.c_path(), binary.resolve())
            stamp.write_text('stale\n')
            with self.assertRaisesRegex(ValueError, 'App.*改写器'):
                rewriter.c_path()
            binary.unlink()
            with self.assertRaisesRegex(ValueError, 'App.*改写器'):
                rewriter.c_path()
        with patch.object(sys, 'executable', str(self.base / 'outside/python3')), patch.dict(os.environ, env, clear=True):
            self.assertIsNone(rewriter.bundled())

    def test_gui_adapter_revalidates_ledger_with_recorded_environment(self):
        import contextlib
        from types import SimpleNamespace
        jobs = str(ROOT / 'pack/jobs'); sys.path.insert(0, jobs); self.addCleanup(sys.path.remove, jobs)
        import library, ipa
        from core import Failure
        from pack.targets.android import adapter
        gid = self.identity['titleId']; library_root = self.base / 'game-library'
        out = library_root / 'Android' / gid; out.mkdir(parents=True)
        release = out / 'release'; shutil.copytree(self.stage, release)
        apk = release / 'game.apk'; resources = release / 'resources'
        env = mod.build_environment({'HOME': str(self.base), 'PATH': os.defpath})
        ledger = mod.manifest(release, apk, resources, env)
        ledger_path = release / 'artifact.json'; ledger_path.write_text(json.dumps(ledger))
        private = self.base / 'private'; private.mkdir()
        c = SimpleNamespace(library=library_root, private=private, task='test', keys=self.base / 'keys',
                            engine=ROOT, r={'gameId': gid}, lock=lambda _: contextlib.nullcontext(),
                            begin=lambda _: None, catalog=lambda: {'games': [{'id': gid}]},
                            fonts_dir=lambda: self.base, env=lambda: {'PATH': os.defpath},
                            run=lambda *args, **kwargs: None, check=lambda: None)
        result = {'ok': True, 'apk': str(apk), 'pack': str(resources), 'artifact': str(ledger_path)}
        with patch.object(library, 'require_game'), patch.object(library, 'selected_packages', return_value=[]), \
             patch.object(ipa, 'dotnet_sdk', return_value=(self.base, self.base / 'dotnet')), \
             patch.object(ipa, 'oneclick_result', return_value=result):
            built = adapter.build(c)
            self.assertEqual(built['artifactManifest'], ledger)
            ledger['details']['buildEnvironment']['variables']['S2A_CPATCH_TSV'] = '/injected'
            ledger_path.write_text(json.dumps(ledger))
            with self.assertRaises(Failure): adapter.build(c)

    def test_source_ids_follows_symlinks_with_host_file_stat(self):
        sources = self.base / 'sources'; sources.mkdir()
        original = self.base / 'original'; original.write_bytes(b'x' * 2048)
        (sources / 'fixture.nsp').symlink_to(original)
        nxu = self.base / 'nxu'
        nxu.write_text('#!/bin/sh\nprintf "title_id\\t0100000000000000\\n" > "$2/unpack.tsv"\n')
        nxu.chmod(0o755)
        env = dict(os.environ, S2A_NX_UNPACK=str(nxu), S2A_GAME_SRC=str(sources),
                   S2A_SRC_IDS=str(self.base / 'ids.tsv'))
        result = subprocess.run(['bash', '-c', 'source "$1/pack/srcs.sh"; src_ids', 'test', str(ROOT)],
                                env=env, capture_output=True, text=True, check=True)
        row = result.stdout.strip().split('\t')
        self.assertEqual(len(row), 4, result.stderr)
        self.assertEqual(row[1], '2048')
        self.assertEqual(row[2].split('.')[0], str(int(original.stat().st_mtime)))   # 脚本记到纳秒
        self.assertEqual(row[3], '0100000000000000')
        self.assertEqual(result.stderr, '')

    def test_host_cores_and_memory_produce_positive_parallelism(self):
        env = {k:v for k,v in os.environ.items() if k not in {'S2A_JOBS', 'S2A_CPU_BAKE', 'S2A_MEM_TOTAL_GB', 'S2A_MEM_BAKE_GB'}}
        env.pop('S2A_LANES', None); lanes = self.base / 'lanes'; lanes.mkdir(exist_ok=True)
        env.update(S2A_MACHINE_NO_LOCAL='1', S2A_CORES_QUIET='1', S2A_LANE_DIR=str(lanes))   # 钉死同机道数与负载（#1247），不往真实 ~/.s2a/lanes 登记
        result = subprocess.run(['bash', '-c', 'source "$1/tools/cores.sh"; s2a_cores; printf "%s %s\\n" "$S2A_JOBS" "$S2A_CPU_BAKE"', 'test', str(ROOT)],
                                env=env, capture_output=True, text=True, check=True)
        values = [int(x) for x in result.stdout.split()]
        self.assertEqual(len(values), 2, result.stderr)
        self.assertGreater(values[0], 0); self.assertGreater(values[1], 0)
        self.assertLessEqual(values[1], values[0])
        self.assertEqual(result.stderr, '')

    def test_build_tree_stays_docker_visible_on_mac(self):
        # Mac 上 docker 缺省只挂家目录：输出在外接盘时源码临时树放家目录下；Linux / 输出本在家目录下则不变（10-31 出包退出码 125）
        home = self.base / 'home'; home.mkdir()
        ssd = self.base / 'Volumes/SSD/out'; ssd.mkdir(parents=True)
        inside = home / 'out'; inside.mkdir()
        self.assertEqual(mod.build_parent(ssd, home, 'Darwin'), home / '.cache/s2arm-android-build')
        self.assertTrue((home / '.cache/s2arm-android-build').is_dir())
        self.assertEqual(mod.build_parent(inside, home, 'Darwin'), inside)
        self.assertEqual(mod.build_parent(ssd, home, 'Linux'), ssd)

    def test_ledger_binds_apk_and_resource_files(self):
        ledger = mod.manifest(self.stage, self.apk, self.resources)
        self.assertEqual(ledger['details']['apkSha256'], self.identity['apk']['sha256'])
        self.assertFalse(ledger['details']['inputFrozen'])
        self.assertIn('T04', ledger['details']['inputStatus'])
        self.assertEqual({x['path'] for x in ledger['files']},
                         {'game.apk', 'resources/manifest.json', 'resources/game-build-artifact.json'})
        self.assertEqual(next(x['sha256'] for x in ledger['files'] if x['path'] == 'game.apk'),
                         self.identity['apk']['sha256'])

    def test_mismatched_apk_and_symlinks_are_rejected(self):
        self.identity['apk']['sha256'] = '0' * 64
        self.write_artifact()
        with self.assertRaises(ValueError):
            mod.manifest(self.stage, self.apk, self.resources)
        self.identity['apk']['sha256'] = hashlib.sha256(self.apk.read_bytes()).hexdigest()
        self.write_artifact()
        (self.resources / 'link').symlink_to(self.apk)
        with self.assertRaises(ValueError):
            mod.manifest(self.stage, self.apk, self.resources)

    def test_pipeline_runs_real_entry_points_and_publishes_only_complete_delivery(self):
        repo = self.base / 'repo'
        app = repo / 'android/app'
        (app / 'src/main/java/org/s2arm/android').mkdir(parents=True)
        (app / 'build.gradle').write_text('s2arm.nativeMode')
        (app / 'src/main/java/org/s2arm/android/LibraryActivity.java').touch()
        product = self.base / 'out/roots/product'
        product.mkdir(parents=True)
        (app / 'build').mkdir()
        (app / 'build/stale').write_text('old build')
        provider = provider_elf()
        write_provider_apk(self.apk, provider)
        self.identity['apk']['sha256'] = hashlib.sha256(self.apk.read_bytes()).hexdigest(); self.write_artifact()
        calls = []
        exported = []
        built_variants = []
        native_flags = ['-O3 -DNDEBUG']
        def export(destination, env=None):
            shutil.copytree(repo, destination)
            exported.append(destination)
        def run(args, **kwargs):
            args = list(map(str, args)); calls.append((args, dict(kwargs.get('env', {}))))
            build_app = exported[-1] / 'android/app'
            if args[0].endswith('/java') or args[0] == 'java':
                return subprocess.CompletedProcess(args, 0)
            if '--direct-identity' in args:
                return
            if args[1].endswith('make_pack.py'):
                Path(args[2]).mkdir()
            elif args[1].endswith('direct_library.py'):
                Path(args[-1]).write_text('{}')
            elif args[1].endswith('docker/build.sh') and not any(a.startswith(':app:assemble') for a in args):
                self.assertFalse((build_app / 'build').exists())
            elif args[1].endswith('game_apk.py') and '--resources' not in args:
                Path(args[3]).mkdir()
            elif any(a.startswith(':app:assemble') for a in args):
                variant = next(a for a in args if a.startswith(':app:assemble'))[len(':app:assemble'):].lower()
                built_variants.append(variant)
                apk = build_app / f'build/outputs/apk/{variant}/app-{variant}.apk'
                apk.parent.mkdir(parents=True)
                commands = build_app / f'.cxx/{variant}/x/arm64-v8a/compile_commands.json'
                commands.parent.mkdir(parents=True, exist_ok=True)
                commands.write_text(json.dumps([dict(file=str(exported[-1] / 'android/native/bridge.cpp'),
                    command=f'clang++ {native_flags[0]} -c bridge.cpp -o CMakeFiles/s2arm_android.dir/native/bridge.cpp.o')]))
                shutil.copyfile(self.apk, apk)
                native = build_app / 'build/intermediates/native/libs2arm_android.so'
                native.parent.mkdir(parents=True); native.write_bytes(provider)
                report = build_app / '.cxx/Debug/core-link-Debug.json'
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(json.dumps(dict(provider_role='game', provider_link_complete=True,
                    artifacts=dict(native_library=dict(path='/src/' + str(native.relative_to(exported[-1])),
                        sha256=hashlib.sha256(native.read_bytes()).hexdigest(), build_id='41' * 20)))))
            elif '--resources' in args:
                dest = Path(args[-1]); dest.mkdir()
                (dest / 'manifest.json').write_text('{}')
                (dest / 'game-build-artifact.json').write_text(json.dumps(self.identity))
        with patch.object(mod, 'ROOT', repo), patch.object(mod, 'export_sources', side_effect=export), \
             patch.object(mod, 'build_backend', return_value='docker'), \
             patch.dict(os.environ, {'S2A_REWRITE_SPIN': '1', 'S2A_CPATCH_TSV': '/malicious/patch', 'S2A_REWRITER': 'python', 'S2A_REWRITER_C': '/malicious/rewrite', 'PYTHONPATH': '/malicious/imports'}), \
             patch.object(mod.subprocess, 'run', side_effect=run):
            result = mod.package(product, self.base / 'out')
        self.assertTrue(Path(result['apk']).is_file())
        self.assertTrue(Path(result['resources']).is_dir())
        ledger = json.loads(Path(result['artifact']).read_text())
        self.assertFalse(ledger['details']['inputFrozen'])
        self.assertEqual(ledger['details']['apkSha256'], self.identity['apk']['sha256'])
        self.assertTrue(any(':app:clean' in cmd and '-Ps2arm.nativeMode=game' in cmd for cmd, _ in calls))
        apk_call = next(cmd for cmd, _ in calls if cmd[1].endswith('game_apk.py') and '--resources' not in cmd)
        self.assertGreater(int(apk_call[apk_call.index('--version-code') + 1]), 2)   # 机上旧包是 2：后出的包必须更大才能覆盖装
        self.assertEqual(next(env['S2A_ANDROID_SKIP_APPLE_ARTIFACTS'] for cmd, env in calls if cmd[1].endswith('make_pack.py')), '1')
        pack_calls = [(cmd, env) for cmd, env in calls if cmd[1].endswith('make_pack.py')]
        self.assertEqual(len(pack_calls), 2)
        self.assertIn('--direct-graphics', pack_calls[1][0])
        describe = next(cmd for cmd, _ in calls if cmd[1].endswith('direct_library.py') and 'describe' in cmd)
        identity_at = next(i for i, (cmd, _) in enumerate(calls) if cmd[1].endswith('docker/build.sh') and '--direct-identity' in cmd)
        self.assertLess(identity_at, next(i for i, (cmd, _) in enumerate(calls) if cmd is describe))
        self.assertEqual(describe[describe.index('--identity') + 1],
                         str(exported[0] / 'build/android-direct/identity.json'))
        self.assertEqual(pack_calls[1][1]['S2ARM_ANDROID_VK_ENGINE'], '1')
        self.assertEqual(pack_calls[1][1]['S2ARM_ANDROID_VK_BACKEND'], 'nvn_vk')
        self.assertEqual(next(env['S2A_REWRITE_SPIN'] for cmd, env in calls if cmd[1].endswith('make_pack.py')), '0')
        make_env = next(env for cmd, env in calls if cmd[1].endswith('make_pack.py'))
        self.assertEqual(ledger['details']['buildEnvironment']['variables'], dict(sorted(make_env.items())))
        for forbidden in ('S2A_CPATCH_TSV', 'S2A_REWRITER', 'S2A_REWRITER_C', 'PYTHONPATH'):
            self.assertNotIn(forbidden, make_env)
            self.assertNotIn(forbidden, ledger['details']['buildEnvironment']['variables'])
        self.assertTrue((app / 'build/stale').exists())
        self.assertFalse(list((self.base / 'out').glob('.android-build-*')))
        self.assertFalse((Path(result['apk']).parent / 'source-pack').exists())
        # 玩家包一律正式构建（10-31 维护者）：assembleRelease + lintRelease，验签用 release 产物
        self.assertEqual(built_variants, ['release'])
        self.assertTrue(any(':app:assembleRelease' in cmd and ':app:lintRelease' in cmd for cmd, _ in calls))
        self.assertFalse(any(':app:assembleDebug' in cmd for cmd, _ in calls))
        sign = [cmd for cmd, _ in calls if cmd[0] == 'docker' and 'apksigner' in cmd[-1]]
        self.assertTrue(all('apk/release/app-release.apk' in cmd[-1] for cmd in sign))
        # 我们自己的验证包：S2A_ANDROID_DEBUG_BUILD=1 才出调试构建
        mod_env = {'S2A_ANDROID_DEBUG_BUILD': '1'}
        def again(extra_env):
            with patch.object(mod, 'ROOT', repo), patch.object(mod, 'export_sources', side_effect=export), \
                 patch.object(mod, 'build_backend', return_value='docker'), patch.dict(os.environ, extra_env), \
                 patch.object(mod.subprocess, 'run', side_effect=run):
                return mod.package(product, self.base / 'out')
        again(mod_env)
        self.assertEqual(built_variants[-1], 'debug')
        # 本次游戏构建之前留下的未优化编译命令（诊断用的默认调试构建）不算：只核本次构建写的
        tree = self.base / 'vtree'
        fresh = tree / 'android/app/.cxx/RelWithDebInfo/x/arm64-v8a/compile_commands.json'
        stale = tree / 'android/app/.cxx/Debug/old/arm64-v8a/compile_commands.json'
        for path, cmd in ((fresh, 'clang -O2 -g -DNDEBUG -c bridge.cpp -o CMakeFiles/s2arm_android.dir/native/bridge.cpp.o'),
                          (stale, 'clang -g -c memory.c -o CMakeFiles/s2arm_android_memory.dir/native/memory.c.o')):
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps([dict(file=str(tree / 'android/native/x.c'), command=cmd)]))
        os.utime(stale, ns=(1, 1))
        asm = tree / 'android/app/.cxx/RelWithDebInfo/y/arm64-v8a/compile_commands.json'
        asm.parent.mkdir(parents=True)   # 汇编源不吃 -O，不算未优化
        asm.write_text(json.dumps([dict(file=str(tree / 'android/native/selftest_gate.S'),
            command='clang -g -c selftest_gate.S -o CMakeFiles/s2arm_android.dir/native/selftest_gate.S.o')]))
        mod.verify_native_optimized(tree, 'release', start_ns=10)
        with self.assertRaisesRegex(ValueError, '未按优化编译'):
            mod.verify_native_optimized(tree, 'release')   # 不按时刻过滤就会扫到它：反证过滤真在起作用
        # 负控：原生没按优化编（无 -O2/-O3）或正式构建没带 -DNDEBUG → 出包报红，不发布
        for flags, why in (('-O0 -DNDEBUG', '未按优化编译'), ('-O3', '没带 -DNDEBUG')):
            native_flags[0] = flags
            with self.assertRaisesRegex(ValueError, why):
                again({})

    def test_linux_host_pipeline_uses_real_host_entry_and_verifies_local_apk(self):
        repo = self.base / 'repo'; app = repo / 'android/app'
        (app / 'src/main/java/org/s2arm/android').mkdir(parents=True)
        (app / 'build.gradle').write_text('s2arm.nativeMode')
        (app / 'src/main/java/org/s2arm/android/LibraryActivity.java').touch()
        product = self.base / 'out/roots/product'; product.mkdir(parents=True)
        sdk = self.base / 'sdk'; signer = sdk / 'build-tools/36.0.0/apksigner'
        signer.parent.mkdir(parents=True); signer.write_text('#!/bin/sh\nexit 0\n'); signer.chmod(0o755)
        provider = provider_elf()
        write_provider_apk(self.apk, provider)
        self.identity['apk']['sha256'] = hashlib.sha256(self.apk.read_bytes()).hexdigest(); self.write_artifact()
        calls = []; envs = []; exported = []
        def export(destination, env=None):
            shutil.copytree(repo, destination); exported.append(destination)
        def run(args, **kwargs):
            args = list(map(str, args)); calls.append(args); envs.append(kwargs.get('env', {}))
            if len(args) > 1 and args[1].endswith('make_pack.py'):
                Path(args[2]).mkdir()
            elif len(args) > 1 and args[1].endswith('direct_library.py'):
                Path(args[-1]).write_text('{}')
            elif len(args) > 1 and args[1].endswith('game_apk.py') and '--resources' not in args:
                Path(args[3]).mkdir()
            elif any(a.startswith(':app:assemble') for a in args):
                root = exported[0]; built = root / 'android/app/build/outputs/apk/release/app-release.apk'
                built.parent.mkdir(parents=True); shutil.copyfile(self.apk, built)
                commands = root / 'android/app/.cxx/release/x/arm64-v8a/compile_commands.json'
                commands.parent.mkdir(parents=True, exist_ok=True)
                commands.write_text(json.dumps([dict(file=str(root / 'android/native/bridge.cpp'),
                    command='clang++ -O3 -DNDEBUG -c bridge.cpp -o CMakeFiles/s2arm_android.dir/native/bridge.cpp.o')]))
                native = root / 'android/app/build/libs2arm_android.so'
                native.write_bytes(provider)
                report = root / 'android/app/.cxx/core-link-Debug.json'; report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(json.dumps(dict(provider_role='game', provider_link_complete=True,
                    artifacts=dict(native_library=dict(path=str(native),sha256=hashlib.sha256(native.read_bytes()).hexdigest(),build_id='41' * 20)))))
            elif '--resources' in args:
                dest = Path(args[-1]); dest.mkdir()
                (dest / 'manifest.json').write_text('{}')
                (dest / 'game-build-artifact.json').write_text(json.dumps(self.identity))
        with patch.object(mod, 'ROOT', repo), patch.object(mod, 'export_sources', side_effect=export), \
             patch.object(mod.platform, 'system', return_value='Darwin'), \
             patch.object(mod.platform, 'machine', return_value='arm64'), \
             patch.dict(os.environ, {'PATH':'/nonexistent', 'ANDROID_HOME':str(sdk)}, clear=True), \
             patch.object(mod.subprocess, 'run', side_effect=run):
            result = mod.package(product, self.base / 'out')
        self.assertTrue(Path(result['artifact']).is_file())
        self.assertEqual(next(env['S2A_REWRITE_SPIN'] for args, env in zip(calls, envs) if len(args) > 1 and args[1].endswith('make_pack.py')), '0')
        host = [args for args in calls if len(args) > 1 and args[1].endswith('android/host/build.sh')]
        self.assertEqual(len(host), 3)
        self.assertEqual(host[0][2:], ['--direct-identity'])
        self.assertIn('-Ps2arm.nativeMode=game', host[2])
        self.assertTrue(any(args[0] == str(signer) and args[1] == 'verify' for args in calls))
        self.assertFalse(any(args[0] == 'docker' for args in calls))






    def test_android_pack_excludes_all_apple_shader_files(self):
        source = ROOT / 'android/tools/make_pack.py'
        spec = importlib.util.spec_from_file_location('android_make_pack', source)
        producer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(producer)
        root = self.base / 'product'
        for directory in ('exefs', 'romfs', 'ctl', 'shaders'):
            (root / directory).mkdir(parents=True)
        (root / 'exefs/main.npdm').touch()
        (root / 'ctl/control.nacp').touch()
        # B requires refresh-prepared system data. Use the shared ID list so
        # this shader fixture follows the same contract as the real producer.
        ids = subprocess.check_output([sys.executable, str(ROOT / 'pack/sysdata_synth.py'), '--ids'], text=True).split()
        self.assertTrue(ids)
        for sid in ids:
            payload = root / 'systemdata' / sid / 'fixture.bin'
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b'fixture')
        (root / 'shaders/old-metal.metallib').write_bytes(b'fixture')
        (root / 's2a_caps.tsv').write_text('plat\tandroid\nwiden\t1\nfps60\t1\n')
        verified = '0100A3501946E000\t0d9649011312f62e93f6a5673a09d7e9\tmk8d\tandroid\t2026-10-26\tfixture table'
        for name, body in (('widen_enable.handheld.tsv', 'fixture table'), ('widen_full.handheld.tsv', 'fixture table'), ('widen_verified.tsv', verified)):
            (root / name).write_text(body)
        stage = self.base / 'first/modules'; stage.mkdir(parents=True)
        with patch.object(producer, 'rewrite'), patch.object(producer, 'stage_address_requirements', return_value='need'), \
             patch.dict(os.environ, {'S2A_ANDROID_SKIP_APPLE_ARTIFACTS': '1'}):
            entries = producer.entries_from_root(str(root), str(stage))
        self.assertFalse(any(name.startswith('shaders/') for name, _ in entries))
        self.assertIn(('s2a_caps.tsv', str(root / 's2a_caps.tsv')), entries)
        library = root / 'shaders/vulkan/direct'; library.mkdir(parents=True)
        (library / 'library.v1.json').write_text('fixture')
        third = self.base / 'third/modules'; third.mkdir(parents=True)
        with patch.object(producer, 'rewrite'), patch.object(producer, 'stage_address_requirements', return_value='need'), \
             patch.dict(os.environ, {'S2A_ANDROID_SKIP_APPLE_ARTIFACTS': '1'}):
            entries = producer.entries_from_root(str(root), str(third))
        self.assertEqual([name for name, _ in entries if name.startswith('shaders/')], ['shaders/vulkan/direct'])
        for name in ('widen_enable.handheld.tsv', 'widen_full.handheld.tsv', 'widen_verified.tsv'):
            self.assertEqual((stage / name).read_text(), (root / name).read_text())
        second_stage = self.base / 'second/modules'; second_stage.mkdir(parents=True)
        with patch.object(producer, 'rewrite'), patch.object(producer, 'stage_address_requirements', return_value='need'), \
             patch.dict(os.environ, {'S2A_ANDROID_SKIP_APPLE_ARTIFACTS': '0'}):
            entries = producer.entries_from_root(str(root), str(second_stage))
        self.assertTrue(any(name.startswith('shaders/') for name, _ in entries))
        self.assertTrue((second_stage / 'widen_enable.handheld.tsv').is_file())

    def test_selected_library_files_are_checked_before_copy(self):
        from types import SimpleNamespace
        sys.path.insert(0, str(ROOT / 'pack/jobs'))
        from core import Failure
        from pack.targets.android.adapter import copy_selected
        library = self.base / 'library'; library.mkdir()
        source = library / 'game.nsp'; source.write_bytes(b'fixture source')
        stage = self.base / 'input'; stage.mkdir()
        context = SimpleNamespace(library=library, check=lambda: None, progress=lambda *_: None)
        identity = hashlib.sha256(source.read_bytes()).hexdigest()
        copy_selected(context, [dict(path='game.nsp', sha256=identity)], stage)
        self.assertEqual((stage / 'game.nsp').read_bytes(), b'fixture source')
        source.write_bytes(b'changed')
        with self.assertRaises(Failure):
            copy_selected(context, [dict(path='game.nsp', sha256=identity)], stage)

    def test_ios_entry_does_not_require_android_target_files(self):
        isolated = self.base / 'apple-engine' / 'pack'
        isolated.mkdir(parents=True)
        shutil.copytree(ROOT / 'pack/jobs', isolated / 'jobs', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copytree(ROOT / 'pack/contracts', isolated / 'contracts', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (isolated / 'targets').mkdir()
        shutil.copytree(ROOT / 'pack/targets/ios', isolated / 'targets/ios', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        result = subprocess.run([sys.executable, '-I', '-S', str(isolated / 'jobs/target_main.py'), '--help'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_versioned_android_request_and_target_contract(self):
        from pack.contracts.validate import ContractError, validate
        from pack.targets.android import adapter
        target = json.loads((ROOT / 'pack/targets/android/target.json').read_text())
        validate('target', target)
        request = dict(schema=1, contract='s2a.job-request.v1', operation='android', targetId='android',
                       privateRoot=str(self.base), gameId='0100000000000000', library=str(self.base), libraryId='fixture')
        with patch.object(adapter.platform, 'system', return_value='Darwin'), \
             patch.object(adapter.platform, 'machine', return_value='arm64'):
            adapter.check_request(request)
            with self.assertRaises(ContractError):
                adapter.check_request(dict(request, operation='ipa'))
            with self.assertRaises(ContractError):
                adapter.check_request(dict(request, gameId=''))
        self.assertEqual(set(target['host']['requires']['os']), {'macos', 'linux'})
        self.assertEqual(set(target['host']['requires']['arch']), {'arm64', 'x64'})
        self.assertNotIn('docker', target['host']['requires']['tools'])
        for system, machine in [('Linux', 'x86_64'), ('Linux', 'aarch64'), ('Darwin', 'arm64')]:
            with self.subTest(system=system, machine=machine), patch.object(adapter.platform, 'system', return_value=system), \
                 patch.object(adapter.platform, 'machine', return_value=machine):
                adapter.check_request(request)
        for system, machine in [('Linux', 'i686'), ('Darwin', 'x86_64'), ('Windows', 'AMD64')]:
            with self.subTest(system=system, machine=machine), patch.object(adapter.platform, 'system', return_value=system), \
                 patch.object(adapter.platform, 'machine', return_value=machine), self.assertRaises(ContractError):
                adapter.check_request(request)

    def test_bundled_engine_includes_android_runtime_and_tools(self):
        from launchers.macos.bundle_engine import INPUTS
        self.assertIn('android', INPUTS)
        self.assertIn('graphics', INPUTS)
        for relative in ('android/docker/build.sh', 'android/docker/Dockerfile',
                         'android/app/build.gradle', 'android/tools/make_pack.py',
                         'android/tools/game_apk.py', 'android/verify-apk.py',
                         'graphics/vulkan/core/DeviceFeatureSnapshot.cs'):
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_bundled_android_excludes_only_test_trees(self):
        from launchers.macos import bundle_engine
        excluded = ('android/codecs/tests/vectors/tiny.h264',
                    'android/app/src/test/java/Foo.java',
                    'android/app/src/androidTest/java/Foo.java',
                    'android/core-link/runtime-tests/foo.c',
                    'graphics/vulkan/wsi/tests/run.py',
                    'graphics/vulkan/core/cache-tests/fixture.cs')
        included = ('android/native/selftest.c', 'android/test.sh',
                    'android/codecs/LICENSE', 'graphics/vulkan/core/DeviceFeatureSnapshot.cs',
                    'pack/tests/fixture.dat', 'tests/visual/galrec/fixture.dat')
        for path in excluded: self.assertFalse(bundle_engine.include_source(Path(path)), path)
        for path in included: self.assertTrue(bundle_engine.include_source(Path(path)), path)

    def test_bundled_fingerprint_ignores_android_test_vectors(self):
        from launchers.macos import bundle_engine
        source = self.base / 'engine'
        prod = source / 'android/native/input.c'; prod.parent.mkdir(parents=True)
        vec = source / 'android/codecs/tests/vectors/fixture.h264'; vec.parent.mkdir(parents=True)
        vk = source / 'graphics/vulkan/core/tests/fixture.cs'; vk.parent.mkdir(parents=True)
        prod.write_text('production'); vec.write_text('first'); vk.write_text('first')
        with patch.object(bundle_engine, 'INPUTS', ['android', 'graphics/vulkan']):
            initial = bundle_engine.source_fingerprint(source)
            vec.write_text('second'); vk.write_text('second')
            self.assertEqual(initial, bundle_engine.source_fingerprint(source))
            prod.write_text('changed')
            self.assertNotEqual(initial, bundle_engine.source_fingerprint(source))

    def test_provider_report_requires_current_game_link_and_matching_binary(self):
        repo = self.base / 'repo'; report = repo / 'android/app/.cxx/core-link-Debug.json'
        report.parent.mkdir(parents=True, exist_ok=True)
        native = repo / 'android/app/build/libs2arm_android.so'
        native.parent.mkdir(parents=True); native.write_bytes(provider_elf())
        write_provider_apk(self.apk, provider_elf())
        doc = dict(provider_role='game', provider_link_complete=True,
                   artifacts=dict(native_library=dict(path='/src/' + str(native.relative_to(repo)),
                       sha256=hashlib.sha256(native.read_bytes()).hexdigest(), build_id='41' * 20)))
        report.write_text(json.dumps(doc))
        with patch.object(mod, 'ROOT', repo):
            mod.verify_game_provider(repo, 0, self.apk)
            native.write_bytes(b'tampered')
            with self.assertRaises(ValueError): mod.verify_game_provider(repo, 0, self.apk)
            with self.assertRaises(ValueError): mod.verify_game_provider(repo, 10**30, self.apk)
            doc['provider_role'] = 'diagnostic'; report.write_text(json.dumps(doc))
            with self.assertRaises(ValueError): mod.verify_game_provider(repo, 0, self.apk)

    def test_host_provider_report_stays_inside_export_and_checks_hash(self):
        repo = self.base / 'repo'; report = repo / 'android/app/.cxx/core-link-Debug.json'
        report.parent.mkdir(parents=True, exist_ok=True)
        native = repo / 'android/app/build/libs2arm_android.so'
        native.parent.mkdir(parents=True); native.write_bytes(provider_elf())
        write_provider_apk(self.apk, provider_elf())
        doc = dict(provider_role='game', provider_link_complete=True,
                   artifacts=dict(native_library=dict(path=str(native),sha256=hashlib.sha256(native.read_bytes()).hexdigest(), build_id='41' * 20)))
        report.write_text(json.dumps(doc))
        mod.verify_game_provider(repo, 0, self.apk, containerized=False)
        outside = self.base / 'outside'; outside.write_bytes(provider_elf())
        doc['artifacts']['native_library']['path'] = str(outside); report.write_text(json.dumps(doc))
        with self.assertRaises(ValueError): mod.verify_game_provider(repo, 0, self.apk, containerized=False)
        doc['artifacts']['native_library']['path'] = str(native); report.write_text(json.dumps(doc))
        native.write_bytes(b'tampered')
        with self.assertRaises(ValueError): mod.verify_game_provider(repo, 0, self.apk, containerized=False)

    def test_apk_provider_build_id_rejects_replacement_and_missing(self):
        repo = self.base / 'repo'
        native = repo / 'android/app/build/libs2arm_android.so'
        native.parent.mkdir(parents=True); native.write_bytes(provider_elf())
        report = repo / 'android/app/.cxx/core-link-Debug.json'
        report.parent.mkdir(parents=True, exist_ok=True)
        identity = dict(path='/src/' + str(native.relative_to(repo)),
                        sha256=hashlib.sha256(native.read_bytes()).hexdigest(), build_id='41' * 20)
        report.write_text(json.dumps(dict(provider_role='game', provider_link_complete=True,
                                          artifacts=dict(native_library=identity))))
        write_provider_apk(self.apk, provider_elf())
        mod.verify_game_provider(repo, 0, self.apk)
        write_provider_apk(self.apk, provider_elf(b'B' * 20))
        with self.assertRaisesRegex(ValueError, 'build.id'):
            mod.verify_game_provider(repo, 0, self.apk)
        write_provider_apk(self.apk, b'not-an-elf')
        with self.assertRaises(ValueError):
            mod.verify_game_provider(repo, 0, self.apk)
        write_provider_apk(self.apk, provider_elf(b''))
        with self.assertRaisesRegex(ValueError, 'build.id'):
            mod.verify_game_provider(repo, 0, self.apk)
        write_provider_apk(self.apk, provider_elf())
        del identity['build_id']
        report.write_text(json.dumps(dict(provider_role='game', provider_link_complete=True,
                                          artifacts=dict(native_library=identity))))
        with self.assertRaises(ValueError):
            mod.verify_game_provider(repo, 0, self.apk)

    def test_version_code_increases_between_packs(self):
        first, second = mod.android_version_code(1793380000), mod.android_version_code(1793380001)
        self.assertLess(first, second)
        self.assertLessEqual(mod.android_version_code(), mod.ANDROID_MAX_VERSION_CODE)
        with self.assertRaises(ValueError):
            mod.android_version_code(mod.ANDROID_MAX_VERSION_CODE + 1)

    def test_host_backend_and_local_signer_do_not_require_docker(self):
        sdk = self.base / 'sdk'; signer = sdk / 'build-tools/36.0.0/apksigner'
        signer.parent.mkdir(parents=True); signer.write_text('#!/bin/sh\nexit 0\n'); signer.chmod(0o755)
        env = {'ANDROID_HOME': str(sdk), 'PATH': '/nonexistent'}
        with patch.object(mod.platform, 'system', return_value='Linux'):
            with self.assertRaises(ValueError):
                mod.build_backend(env)
            with patch.object(mod.shutil, 'which', return_value='/bin/docker'), \
                 patch.object(mod.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
                with self.assertRaises(ValueError):
                    mod.build_backend(env)
            with patch.object(mod.shutil, 'which', return_value='/bin/docker'), \
                 patch.object(mod.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
                self.assertEqual(mod.build_backend(env), 'docker')
        with patch.object(mod.subprocess, 'run') as run:
            mod.verify_signature(self.base, self.apk, env, 'host')
            self.assertEqual(run.call_args.args[0], [str(signer), 'verify', str(self.apk)])
        with patch.object(mod, 'java_runtime_ok', return_value=True), \
             patch.object(mod.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, [str(signer)])) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                mod.verify_signature(self.base, self.apk, env, 'docker')
            self.assertEqual(run.call_count, 1)  # 签名失败不能退回 Docker 重试冒充通过
        # Docker 后端、本机有 apksigner 但没有能用的 Java（Mac 上 /usr/bin/java 空壳）：事先选容器里的 apksigner，只跑一次
        with patch.object(mod, 'java_runtime_ok', return_value=False), patch.object(mod.subprocess, 'run') as run:
            mod.verify_signature(self.base, self.apk, env, 'docker')
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][:2], ['docker', 'run'])
        with patch.object(mod, 'java_runtime_ok', return_value=True), patch.object(mod.subprocess, 'run') as run:
            mod.verify_signature(self.base, self.apk, env, 'docker')
            self.assertEqual(run.call_args.args[0], [str(signer), 'verify', str(self.apk)])
        signer.unlink()
        with self.assertRaisesRegex(ValueError, 'apksigner'):
            mod.verify_signature(self.base, self.apk, env, 'host')

    def test_failed_build_does_not_replace_previous_delivery(self):
        repo = self.base / 'repo'
        app = repo / 'android/app'
        (app / 'src/main/java/org/s2arm/android').mkdir(parents=True)
        (app / 'build.gradle').write_text('s2arm.nativeMode')
        (app / 'src/main/java/org/s2arm/android/LibraryActivity.java').touch()
        product = self.base / 'out/roots/product'; product.mkdir(parents=True)
        old = self.base / 'out/android-releases/old'; old.mkdir(parents=True)
        (old / 'game.apk').write_bytes(b'previous')
        with patch.object(mod, 'ROOT', repo), \
             patch.object(mod, 'export_sources', side_effect=lambda destination, env: shutil.copytree(repo, destination)), \
             patch.object(mod.subprocess, 'run', side_effect=subprocess.CalledProcessError(7, ['fixture'])):
            with self.assertRaises(subprocess.CalledProcessError):
                mod.package(product, self.base / 'out')
        self.assertEqual((old / 'game.apk').read_bytes(), b'previous')
        self.assertEqual([p.name for p in old.parent.iterdir()], ['old'])
        self.assertFalse(list((self.base / 'out').glob('.android-build-*')))

    def test_export_contains_only_committed_sources_and_no_git_directory(self):
        repo = self.base / 'synthetic-repo'; repo.mkdir()
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                   GIT_AUTHOR_NAME='fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
                   GIT_COMMITTER_NAME='fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid')
        def git(*args):
            subprocess.run(['git', '-C', str(repo), *args], env=env, check=True, capture_output=True)
        git('init', '-q', '--template=')
        (repo / 'source.txt').write_text('source')
        git('add', 'source.txt')
        git('-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'fixture')
        (repo / 'untracked-game.bin').write_bytes(b'private fixture')
        out = self.base / 'export'
        with patch.object(mod, 'ROOT', repo):
            mod.export_sources(out, mod.build_environment(os.environ))
        self.assertEqual((out / 'source.txt').read_text(), 'source')
        self.assertFalse((out / 'untracked-game.bin').exists())
        self.assertFalse((out / '.git').exists())
        (repo / 'source.txt').write_text('modified')
        with patch.object(mod, 'ROOT', repo), self.assertRaisesRegex(ValueError, '未提交'):
            mod.export_sources(self.base / 'dirty-export', mod.build_environment(os.environ))

    def test_game_output_cannot_be_inside_checkout(self):
        repo = self.base / 'repo'; repo.mkdir()
        product = self.base / 'product'; product.mkdir()
        with patch.object(mod, 'ROOT', repo), self.assertRaisesRegex(ValueError, '仓库之外'):
            mod.package(product, repo / 'output')

    def test_current_diagnostic_only_runtime_fails_before_build(self):
        repo = self.base / 'repo'
        (repo / 'android/app').mkdir(parents=True)
        (repo / 'android/app/build.gradle').write_text('diagnostic only')
        product = self.base / 'product'
        product.mkdir()
        with patch.object(mod, 'ROOT', repo), patch.object(mod.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, '运行时'):
                mod.package(product, self.base / 'out')
            run.assert_not_called()


class SdkDirResolutionTest(unittest.TestCase):
    """#1207 复审三条：sdk.dir 解析只有 package.py:sdk_dir_from_properties 一处。

    语义是 hostenv.sh 原来的口径（Java Properties）：多行时末行生效、值取
    `=` 后整行（路径可含空格）；区别于旧的「第一行、首个非空白词」。
    """
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.props = self.base / 'local.properties'

    def test_missing_file_returns_none(self):
        self.assertIsNone(mod.sdk_dir_from_properties(self.props))

    def test_path_with_spaces_keeps_rest_of_line(self):
        self.props.write_text('sdk.dir=/opt/Android SDK/sdk\n')
        self.assertEqual(mod.sdk_dir_from_properties(self.props),
                         '/opt/Android SDK/sdk')

    def test_last_sdk_dir_line_wins(self):
        self.props.write_text('sdk.dir=/opt/first-sdk\n'
                              'sdk.dir=/opt/second sdk\n')
        self.assertEqual(mod.sdk_dir_from_properties(self.props), '/opt/second sdk')

    def test_value_surrounded_by_whitespace_is_trimmed(self):
        self.props.write_text('  sdk.dir = /opt/sdk  \n')
        self.assertEqual(mod.sdk_dir_from_properties(self.props), '/opt/sdk')

    def test_no_sdk_dir_line_returns_none(self):
        self.props.write_text('other.key=value\n# sdk.dir=/commented-ish\n')
        # 注释行以 # 开头：regex 要求行内 sdk.dir= 前只有空白，不匹配「# sdk.dir=」
        self.assertIsNone(mod.sdk_dir_from_properties(self.props))


if __name__ == '__main__':
    unittest.main()
