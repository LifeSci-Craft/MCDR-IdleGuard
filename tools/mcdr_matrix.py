"""跨版本矩阵：让每个 MCDR 版本各装一次本插件，跑同几条命令，逐项核对。

用户定的标准（2026-10-04，跨项目）：**任何改到插件运行时行为的改动，都要跑一次各版本测试**，
最低覆盖「支持的最低版本 + 若干中间版本 + 最新版本」，外加**低于最低版本者应被干净拒绝**。

这里拿本机已装好的多个 MCDR 环境各跑一遍真实 MCDR（不是 mock）：
每个版本起一个 MCDR、装上当前构建、发 `!!ig help` / `!!ig status` / `!!ig reload`，
然后断言「插件加载成功（或被干净拒绝）+ 三屏都渲染出来 + 没有 Traceback」。

用法：
    python tools/mcdr_matrix.py                 # 跑本机全部已知版本
    python tools/mcdr_matrix.py --versions 2.16.0 2.15.7
    python tools/mcdr_matrix.py --allow-missing-low-env   # 没建"低于最低版本"的环境时显式跳过该项
"""
from __future__ import annotations

import argparse
import json
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

#: dependencies.mcdreforged 里解析不出下限时的回退值
MIN_MCDR_FALLBACK = '2.13.0'

#: 依赖写法里的下限，例如 ">=2.13.0" / ">= 2.13.0, <3"
MIN_MCDR_PATTERN = re.compile(r'>=\s*([0-9][0-9A-Za-z.\-+]*)')


def read_min_mcdr() -> str:
    """从 mcdreforged.plugin.json 的 dependencies.mcdreforged 里解析出最低 MCDR 版本。

    以前这里是写死的常量 —— 那等于同一件事存了两份真相：改了元数据却忘了改这里，
    矩阵就会拿错误的版本线去判断"低于最低版本"。解析不出来（字段缺失、写法不认识）
    时回退到 ``MIN_MCDR_FALLBACK`` 并打一条 warning，绝不静默用错值。
    """
    path = os.path.join(ROOT, 'mcdreforged.plugin.json')
    try:
        with open(path, encoding='utf-8') as handle:
            requires = json.load(handle)['dependencies']['mcdreforged']
    except Exception as error:  # noqa: BLE001
        print(f'⚠️ 读不出 {path} 的 dependencies.mcdreforged'
              f'（{type(error).__name__}: {error}），最低版本回退到 {MIN_MCDR_FALLBACK}')
        return MIN_MCDR_FALLBACK
    match = MIN_MCDR_PATTERN.search(str(requires))
    if match is None:
        print(f'⚠️ dependencies.mcdreforged 的写法 {requires!r} 里没有 ">=" 下限，'
              f'最低版本回退到 {MIN_MCDR_FALLBACK}')
        return MIN_MCDR_FALLBACK
    return match.group(1)


#: 插件声明的最低 MCDR 版本（直接来自 mcdreforged.plugin.json，不再硬编码）
MIN_MCDR = read_min_mcdr()

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
    """跑一个版本，返回 {'env','version','loaded','rejected','screens','traceback','out','returncode'}。"""
    version = env_version(env)
    workdir = prepare(env, reuse=reuse)
    out_path = os.path.join(workdir, 'run_out.txt')
    probe = os.path.join(ROOT, 'tools', 'mcdr_probe.py')
    command = [sys.executable, probe, workdir, '--mcdr', mcdr_exe(env),
               '--send', '!!ig help', '--send', '!!ig status', '--send', '!!ig reload',
               '--wait', '10', '--sleep', '3', '--timeout', '26']
    # 探针的退出码要留下来：以前直接丢掉，于是"MCDR 起不来 / 一个字都没输出"这种失败
    # 只能靠下面的正则去猜，断言不过时也看不出是探针本身没跑成。
    # 顺手显式按 UTF-8 解码它的 stdout：探针固定输出 UTF-8，而 `text=True` 默认用 locale
    # （中文机器上是 GBK）解码，中文界面文字会炸在读取线程里。
    completed = subprocess.run(command, capture_output=True, text=True, timeout=300,
                               encoding='utf-8', errors='replace')
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
            'screens': screens, 'traceback': traceback, 'out': out_path,
            'returncode': completed.returncode}


def check_below_minimum(problems: list, reuse: bool = False, allow_missing: bool = False) -> dict:
    """低于最低版本的 MCDR 必须**被干净拒绝**（报"不满足依赖"，而不是裸报错或照常加载）。

    本机为此专门建了 `.workbuddy/env/mcdr212`（MCDR 2.12.0）。这个环境不在仓库里
    （仓库目录本身随时可以克隆到别处），所以两处都找一遍：仓库自身的 `.workbuddy/`，
    以及它的上一级目录（本机的会话目录）。
    注意：老版本 MCDR 的 CLI 没有 `--no-server-start`，这里不传任何额外参数
    （起不来服务端不影响"插件能不能加载"这件事）。

    找不到这个环境时**默认判失败**（退出码非 0）：这条检查缺了，"低于最低版本会被干净拒绝"
    就等于没验证过。确实没有这个环境而只想跑其余版本时，用 ``--allow-missing-low-env``
    显式跳过（跳过也会打醒目提示，不会静默）。
    """
    candidates = [os.path.join(ROOT, '.workbuddy', 'env', 'mcdr212'),
                  os.path.join(os.path.dirname(ROOT), '.workbuddy', 'env', 'mcdr212')]
    env_dir = next((path for path in candidates
                    if os.path.isfile(os.path.join(path, 'Scripts', 'mcdreforged.exe'))), candidates[0])
    exe = os.path.join(env_dir, 'Scripts', 'mcdreforged.exe')
    if not os.path.isfile(exe):
        message = (f'没找到"低于最低版本 {MIN_MCDR}"的 MCDR 环境（找过 {candidates}）'
                   f'—— 这条检查是矩阵的一半，缺了就等于没验证')
        if allow_missing:
            print(f'⚠️ 已按 --allow-missing-low-env 显式跳过：{message}')
        else:
            print(f'✗ {message}')
            problems.append(f'{message}；确实没有这个环境就加 --allow-missing-low-env 显式跳过')
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
                   capture_output=True, text=True, timeout=240,
                   encoding='utf-8', errors='replace')
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
    # 输出被重定向（管道 / 文件）时，Windows 会按 locale 编码 stdout（中文机器上是 GBK），
    # 打印 ⚠ / ✗ 会直接 UnicodeEncodeError 崩掉 —— 而矩阵本来就是被自动跑法重定向调用的。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser()
    parser.add_argument('--versions', nargs='*', default=None, help='只跑这些版本（按 MCDR 版本号）')
    parser.add_argument('--reuse', action='store_true',
                        help='复用已有实例目录（只覆盖插件，不做删除动作）——批量删除被本机策略拦下时用')
    parser.add_argument('--allow-missing-low-env', action='store_true',
                        help='找不到"低于最低版本"的 MCDR 环境时允许跳过（默认判失败）')
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
    check_below_minimum(below_min_problems, reuse=args.reuse,
                        allow_missing=args.allow_missing_low_env)
    failures.extend({'env': 'below_min', 'version': MIN_MCDR, 'out': problem}
                    for problem in below_min_problems)

    if failures:
        print('\n失败的版本：')
        for row in failures:
            code = '' if row.get('returncode') is None else f"（探针退出码 {row['returncode']}）"
            print(f"  {row['env']}（MCDR {row['version']}）{code}输出：{row['out']}")
        return 1
    print('\n全部版本通过。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
