"""跨版本矩阵：让每个 MCDR 版本各装一次本插件，跑同几条命令，逐项核对。

用户定的标准（2026-10-04，跨项目）：**任何改到插件运行时行为的改动，都要跑一次各版本测试**，
最低覆盖「支持的最低版本 + 若干中间版本 + 最新版本」，外加**低于最低版本者应被干净拒绝**。

这里拿本机已装好的多个 MCDR 环境各跑一遍真实 MCDR（不是 mock）：
每个版本起一个 MCDR、装上当前构建、发 `!!ig help` / `!!ig status` / `!!ig reload`，
然后断言「插件加载成功（或被干净拒绝）+ 三屏都渲染出来 + 没有 Traceback」。

用法：
    python tools/mcdr_matrix.py                 # 跑本机全部已知版本
    python tools/mcdr_matrix.py --versions 2.16.0 2.15.7
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import build  # noqa: E402

ENVS_DIR = os.path.expanduser('~/.workbuddy/binaries/python/envs')
#: 环境名 -> 期望的 MCDR 版本（只用于报告）
ENV_NAMES = ('mcdr213', 'mcdr214', 'mcdr2150', 'mcdr215', 'mcdr216')

#: 插件声明的最低 MCDR 版本（mcdreforged.plugin.json 的 dependencies）
MIN_MCDR = '2.13.0'

WORK_ROOT = os.path.join(ROOT, '.workbuddy', 'env', 'matrix')

#: ANSI 转义序列（MCDR 控制台输出里的颜色码）
ANSI = re.compile('\x1b\\[[0-9;]*m')   # ESC [ ... m



def mcdr_exe(env: str) -> str:
    return os.path.join(ENVS_DIR, env, 'Scripts', 'mcdreforged.exe')


def env_version(env: str) -> str:
    python = os.path.join(ENVS_DIR, env, 'Scripts', 'python.exe')
    try:
        output = subprocess.run([python, '-c', 'import mcdreforged; print(mcdreforged.__version__)'],
                                capture_output=True, text=True, timeout=60)
        return output.stdout.strip() or '?'
    except Exception:  # noqa: BLE001
        return '?'


def prepare(env: str, reuse: bool = False) -> str:
    """给某个 MCDR 版本准备一个干净的实例目录（首次会生成 config.yml）。

    ``reuse=True`` 时**复用已有目录、只把新构建覆盖进去**（全程不做删除动作）——
    给"批量删除会被本机安全策略拦下"的环境用。语义仍然是"用当前构建跑一遍"：
    目录里除了插件都是上一轮 init 出来的 MCDR 骨架，不参与断言。
    """
    workdir = os.path.join(WORK_ROOT, env)
    if reuse and os.path.isdir(os.path.join(workdir, 'plugins')):
        build.copy_folder_plugin(os.path.join(workdir, 'plugins'))
        return workdir
    if os.path.isdir(workdir):
        shutil.rmtree(workdir)
    os.makedirs(workdir)
    subprocess.run([mcdr_exe(env), 'init'], cwd=workdir, capture_output=True, text=True, timeout=180)
    build.copy_folder_plugin(os.path.join(workdir, 'plugins'))
    # 无 tty：关掉高级控制台，否则 MCDR 会尝试接管终端
    config = os.path.join(workdir, 'config.yml')
    if os.path.isfile(config):
        with open(config, encoding='utf-8') as handle:
            text = handle.read()
        text = re.sub(r'^(\s*)advanced_console:.*$', r'\1advanced_console: false', text, flags=re.M)
        with open(config, 'w', encoding='utf-8') as handle:
            handle.write(text)
    return workdir


def run_one(env: str, reuse: bool = False) -> dict:
    """跑一个版本，返回 {'version','loaded','rejected','screens','traceback','lines'}。"""
    version = env_version(env)
    workdir = prepare(env, reuse=reuse)
    out_path = os.path.join(workdir, 'run_out.txt')
    probe = os.path.join(ROOT, 'tools', 'mcdr_probe.py')
    command = [sys.executable, probe, workdir, '--mcdr', mcdr_exe(env),
               '--send', '!!ig help', '--send', '!!ig status', '--send', '!!ig reload',
               '--wait', '10', '--sleep', '3', '--timeout', '26']
    subprocess.run(command, capture_output=True, text=True, timeout=300)
    # MCDR 在真实控制台里会保留 ANSI 颜色码（本插件界面的颜色就是这样生效的），
    # 断言前先剥掉，免得"颜色码夹在中间"导致匹配不上
    text = ANSI.sub('', open(out_path, encoding='utf-8', errors='replace').read())
    loaded = bool(re.search(r'Plugin idle_guard@\S+ loaded', text))
    rejected = bool(re.search(r'不满足依赖|dependency|Dependencies .* not|version requirement',
                              text, re.I))
    # 界面按语言分中英两套文案，所以断言只认「两种语言都成立」的部分。
    # 顶栏 / 表头都写成了"两侧一串 = 夹着中间"的形式（1.3.0 起顶栏与底线同宽），
    # 所以标题匹配用正则而不是固定字面量。
    header = bool(re.search(r'={3,}\s*Idle Guard v[\d.]+\s*={3,}', text))
    screens = {
        'help': header and 'status' in text and 'reload' in text and 'sleep' in text,
        # 表头四列都在，就说明状态表确实渲染出来了（中英两套列名都认）
        'status': header
                  and ('Server' in text or '子服' in text)
                  and ('Role' in text or '角色' in text)
                  and ('Players' in text or '人数' in text)
                  and ('Switch' in text or '开关' in text),
        'reload': 'Idle Guard 重载完成' in text or 'Idle Guard reloaded' in text,
    }
    traceback = 'Traceback (most recent call last)' in text
    return {'env': env, 'version': version, 'loaded': loaded, 'rejected': rejected,
            'screens': screens, 'traceback': traceback, 'out': out_path}


def check_below_minimum(problems: list, reuse: bool = False) -> dict:
    """低于最低版本的 MCDR 必须**被干净拒绝**（报"不满足依赖"，而不是裸报错或照常加载）。

    本机为此专门建了 `.workbuddy/env/mcdr212`（MCDR 2.12.0）。这个环境不在仓库里
    （仓库目录本身随时可以克隆到别处），所以两处都找一遍：仓库自身的 `.workbuddy/`，
    以及它的上一级目录（本机的会话目录）。
    注意：老版本 MCDR 的 CLI 没有 `--no-server-start`，这里不传任何额外参数
    （起不来服务端不影响"插件能不能加载"这件事）。
    """
    candidates = [os.path.join(ROOT, '.workbuddy', 'env', 'mcdr212'),
                  os.path.join(os.path.dirname(ROOT), '.workbuddy', 'env', 'mcdr212')]
    env_dir = next((path for path in candidates
                    if os.path.isfile(os.path.join(path, 'Scripts', 'mcdreforged.exe'))), candidates[0])
    exe = os.path.join(env_dir, 'Scripts', 'mcdreforged.exe')
    if not os.path.isfile(exe):
        print('跳过"低于最低版本"检查：没找到 MCDR 2.12.0 环境（.workbuddy/env/mcdr212）')
        return {}
    python = os.path.join(env_dir, 'Scripts', 'python.exe')
    version = subprocess.run([python, '-c', 'import mcdreforged; print(mcdreforged.__version__)'],
                             capture_output=True, text=True, timeout=60).stdout.strip()
    workdir = os.path.join(WORK_ROOT, 'below_min')
    if reuse and os.path.isdir(os.path.join(workdir, 'plugins')):
        build.copy_folder_plugin(os.path.join(workdir, 'plugins'))
    else:
        if os.path.isdir(workdir):
            shutil.rmtree(workdir)
        os.makedirs(workdir)
        subprocess.run([exe, 'init'], cwd=workdir, capture_output=True, text=True, timeout=180)
        build.copy_folder_plugin(os.path.join(workdir, 'plugins'))
    config = os.path.join(workdir, 'config.yml')
    if os.path.isfile(config):
        text = re.sub(r'^(\s*)advanced_console:.*$', r'\1advanced_console: false',
                      open(config, encoding='utf-8').read(), flags=re.M)
        open(config, 'w', encoding='utf-8').write(text)
    subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'mcdr_probe.py'), workdir,
                    '--mcdr', exe, '--extra-args', '', '--wait', '10', '--timeout', '14'],
                   capture_output=True, text=True, timeout=240)
    raw = open(os.path.join(workdir, 'run_out.txt'), encoding='utf-8', errors='replace').read()
    text = ANSI.sub('', raw)
    # MCDR 会先把它 loaded 进来，再在依赖检查里卸掉 —— 所以不能只看 "loaded"：
    # 必须看到 "Unloading plugin ... due to ... Dependency ... does not meet version requirement"
    loaded = bool(re.search(r'Plugin idle_guard@\S+ loaded', text))
    unloaded = bool(re.search(r'Unloading plugin idle_guard@\S+ due to[^\n]*[Dd]ependency', text))
    rejected = unloaded or bool(re.search(r'(does not meet version requirement|不满足依赖)', text, re.I))
    if loaded and not unloaded:
        problems.append(f'MCDR {version} 低于最低版本 {MIN_MCDR}，插件却照常运行了'
                        f'（应当在依赖检查里被卸掉）：{workdir}/run_out.txt')
    elif not rejected:
        problems.append(f'MCDR {version} 低于最低版本，没看到"不满足依赖"之类的干净提示：'
                        f'{workdir}/run_out.txt')
    verdict = ('被干净拒绝（依赖检查卸掉）' if (loaded and unloaded)
               else ('被拒绝' if rejected else '既没加载也没提示'))
    print(f'  MCDR {version}（低于最低版本 {MIN_MCDR}）：{verdict}')
    return {'version': version, 'loaded': loaded, 'rejected': rejected}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--versions', nargs='*', default=None, help='只跑这些版本（按 MCDR 版本号）')
    parser.add_argument('--reuse', action='store_true',
                        help='复用已有实例目录（只覆盖插件，不做删除动作）——批量删除被本机策略拦下时用')
    args = parser.parse_args()

    version_now = build.read_version()
    print(f'用仓库根（= 文件夹插件根）的当前源码跑矩阵：{build.PLUGIN_ID} v{version_now}')
    os.makedirs(WORK_ROOT, exist_ok=True)

    rows = []
    for env in ENV_NAMES:
        if not os.path.isfile(mcdr_exe(env)):
            print(f'跳过 {env}：没有这个环境')
            continue
        version = env_version(env)
        if args.versions and version not in args.versions:
            continue
        print(f'--- 跑 {env}（MCDR {version}）---')
        try:
            rows.append(run_one(env, reuse=args.reuse))
        except Exception as exc:  # noqa: BLE001
            rows.append({'env': env, 'version': version, 'loaded': False, 'rejected': False,
                         'screens': {}, 'traceback': True, 'out': f'异常：{exc}'})

    print()
    print(f'{"环境":<9} {"MCDR":<8} {"加载":<6} {"帮助":<6} {"状态":<6} {"重载":<6} 结论')
    failures = []
    for row in rows:
        screens = row.get('screens') or {}
        ok_screens = all(screens.get(key) for key in ('help', 'status', 'reload'))
        below_min = False
        try:
            from packaging.version import Version      # type: ignore
            below_min = Version(row['version']) < Version(MIN_MCDR)
        except Exception:  # noqa: BLE001
            below_min = False
        ok = (row['rejected'] and below_min) or (row['loaded'] and ok_screens and not row['traceback'])
        verdict = 'OK' if ok else '失败'
        if not ok:
            failures.append(row)
        print(f"{row['env']:<9} {row['version']:<8} "
              f"{'是' if row['loaded'] else '否':<6} "
              f"{'OK' if screens.get('help') else '-':<6} "
              f"{'OK' if screens.get('status') else '-':<6} "
              f"{'OK' if screens.get('reload') else '-':<6} {verdict}")

    print()
    print('— 低于最低版本的版本应当被干净拒绝 —')
    below_min_problems: list = []
    check_below_minimum(below_min_problems, reuse=args.reuse)
    failures.extend({'env': 'below_min', 'version': MIN_MCDR, 'out': problem}
                    for problem in below_min_problems)

    if failures:
        print('\n失败的版本：')
        for row in failures:
            print(f"  {row['env']}（MCDR {row['version']}）输出：{row['out']}")
        return 1
    print('\n全部版本通过。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
