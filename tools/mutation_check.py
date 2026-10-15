#!/usr/bin/env python3
"""变异检查（mutation check）：把守卫**故意改坏**，确认相关测试真的会失败。

一套全绿的测试本身证明不了什么。这个脚本把若干关键守卫逐个注入到一棵临时副本里，
要求对应的测试**必须**挂掉；某条变异"活下来"（测试全绿）就说明盯它的用例是装饰品，
需要补强。

用法（在仓库根执行）::

    python tools/mutation_check.py            # 逐条注入、逐条跑，最后打印 caught x/y
    python tools/mutation_check.py --list     # 只列出变异，不跑
    python tools/mutation_check.py --only 总线  # 只跑名字里含"总线"的变异

设计与 SLF / MUC 的同类工具一致：**复制一份仓库到临时目录**再改写源码，
绝不碰工作树；每条变异用精确的字符串替换施加，施加不上会被单独报成"锚点失效"
（说明脚本自己过期了），而不是算作"被抓住"。

⚠️ 覆盖范围（最小可用版）：只挑了 9 条**最关键**的守卫 —— 总线鉴权、空口令、
命令注入（玩家名 / 音效 id）、有人在线绝不休眠、常驻子服、真实服务端活着不抢端口、
配置自检的历史口令、外部停服不被当成休眠。三个仓库里 IG 的守卫密度最高，
后续可以按同样的写法继续加；这里不声称覆盖全部。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, List, Sequence, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 变异要改的文件（相对仓库根）
MODELS = 'idle_guard/models.py'
BUS = 'idle_guard/bus.py'
SUPERVISOR = 'idle_guard/supervisor.py'
TAKEOVER = 'idle_guard/takeover.py'


class Mutation:
    """一条变异：名字、目标文件、把源码改坏的函数、以及应该抓住它的测试选择器。"""

    def __init__(self, name: str, path: str, transform: Callable[[str], str],
                 selectors: Sequence[str], why: str) -> None:
        self.name = name
        self.path = path
        self.transform = transform
        self.selectors = list(selectors)
        self.why = why


def replace_once(source: str, old: str, new: str) -> str:
    """精确替换一次；锚点不存在或出现多次就抛错（变异施加不上必须被看见）。"""
    count = source.count(old)
    if count != 1:
        raise LookupError(f'锚点出现 {count} 次（期望 1 次）：{old[:60]!r}')
    return source.replace(old, new, 1)


# --------------------------------------------------------------------------- #
# 各条变异
# --------------------------------------------------------------------------- #

def token_always_matches(source: str) -> str:
    """总线鉴权恒真：任何口令都能通过。"""
    return replace_once(
        source,
        "        try:\n"
        "            return hmac.compare_digest(given.encode('utf-8'), "
        "str(self._token).encode('utf-8'))\n"
        "        except (AttributeError, UnicodeError):\n"
        "            return False\n",
        "        return True  # mutation: 鉴权恒真\n",
    )


def empty_token_still_listens(source: str) -> str:
    """空口令也照常监听（空口令等于没有鉴权）。"""
    return replace_once(
        source,
        "        if not str(self._token or '').strip():",
        "        if False:  # mutation: 空口令照样监听",
    )


def sound_guard_off(source: str) -> str:
    """音效 id 不再过滤：换行会被拼进服务端控制台命令。"""
    return replace_once(
        source,
        "    text = str(sound or '')\n"
        "    if not text or len(text) > MAX_SOUND_ID_LENGTH:\n"
        "        return ''\n"
        "    return text if _SOUND_ID_RE.match(text) else ''\n",
        "    return str(sound or '')  # mutation: 音效 id 不过滤\n",
    )


def player_name_guard_off(source: str) -> str:
    """玩家名不再过滤：它会被拼进 tellraw / playsound。"""
    return replace_once(
        source,
        "    name = str(player or '').strip()\n"
        "    return name if _PLAYER_NAME_RE.match(name) else ''\n",
        "    return str(player or '')  # mutation: 玩家名不过滤\n",
    )


def sleep_with_players_allowed(source: str) -> str:
    """有人在线也允许休眠（用户明确要求绝不做的事）。"""
    return replace_once(
        source,
        "        if players == 0:\n            return True, ''\n",
        "        if players is not None:  # mutation: 有人在也放行\n            return True, ''\n",
    )


def always_on_ignored(source: str) -> str:
    """常驻（always_on）不再拦住休眠。"""
    return replace_once(
        source,
        "        if managed.entry.always_on:\n"
        "            return False, self.tr('action.always_on', server=managed.name)\n",
        "        if False:  # mutation: 常驻不再拦住休眠\n            return False, ''\n",
    )


def legacy_token_not_reported(source: str) -> str:
    """自检不再点名"口令还是历史默认值"。"""
    return replace_once(
        source,
        "        elif stripped_token == LEGACY_BUS_TOKEN:",
        "        elif False:  # mutation: 不再点名历史口令",
    )


def fake_server_always_may_bind(source: str) -> str:
    """去掉"真实服务端还活着就不抢端口"的两道判断。"""
    source = replace_once(
        source,
        "                return not self._mcdr.is_server_running()\n",
        "                return True  # mutation: 服务端在跑也让假服务端绑\n",
    )
    return replace_once(
        source,
        "        return not self._probe_port(managed.entry.host, managed.entry.port, 0.5)\n",
        "        return True  # mutation: 端口有人应答也让假服务端绑\n",
    )


def manual_stop_treated_as_crash(source: str) -> str:
    """外部正常停服（返回码 0）被当成崩溃：于是又去抢端口、还留住 MCDR。"""
    return replace_once(
        source,
        "                if return_code == 0:\n",
        "                if False:  # mutation: 正常停服也按崩溃处理\n",
    )


MUTATIONS: List[Mutation] = [
    Mutation('总线鉴权恒真', BUS, token_always_matches,
             ['tests/test_bus.py'],
             '口令不匹配的客户端必须被拒绝'),
    Mutation('空口令照样监听', BUS, empty_token_still_listens,
             ['tests/test_review_p1_guards.py::test_a_bus_with_an_empty_token_refuses_to_listen',
              'tests/test_review_p1_guards.py::test_a_whitespace_only_token_also_refuses_to_listen'],
             '空口令等于没有鉴权，必须拒绝启动总线'),
    Mutation('音效 id 不过滤', MODELS, sound_guard_off,
             ['tests/test_review_p1_guards.py -k illegal_sound',
              'tests/test_review_p1_guards.py::test_safe_sound_id'],
             '对端给的 sound 会被拼进服务端控制台命令，换行即第二条命令'),
    Mutation('玩家名不过滤', MODELS, player_name_guard_off,
             ['tests/test_state_refresh_and_sound.py -k player_names'],
             '玩家名来自客户端握手，会被拼进 tellraw / playsound'),
    Mutation('有人在也休眠', SUPERVISOR, sleep_with_players_allowed,
             ['tests/test_sleep_guard_and_eta.py -k "players"'],
             '有人在线绝不休眠（用户明确要求）'),
    Mutation('常驻子服被休眠', SUPERVISOR, always_on_ignored,
             ['tests/test_always_on_and_velocity.py -k "always_on"'],
             'always_on 的子服自动与手动都必须拒绝休眠'),
    Mutation('不再点名历史口令', SUPERVISOR, legacy_token_not_reported,
             ['tests/test_config_check.py::test_default_bus_token_is_flagged'],
             '配置自检必须点名 bus.token 还是历史默认值'),
    Mutation('假服务端抢活着的端口', TAKEOVER, fake_server_always_may_bind,
             ['tests/test_review_p3_and_docs.py'
              '::test_the_fake_server_refuses_to_bind_while_the_real_server_is_alive'],
             '真实服务端还活着时绝不抢端口'),
    Mutation('正常停服当成崩溃', SUPERVISOR, manual_stop_treated_as_crash,
             ['tests/test_manual_stop.py'],
             '返回码 0 是"管理员自己停的"，插件不该接管端口'),
]


# --------------------------------------------------------------------------- #
# 运行
# --------------------------------------------------------------------------- #

def purge_bytecode(root: str) -> None:
    """删掉所有 __pycache__：变异与还原的 (mtime, size) 可能撞上，让 .pyc 冒充新代码。"""
    for base, dirs, _files in os.walk(root):
        if '__pycache__' in dirs:
            shutil.rmtree(os.path.join(base, '__pycache__'), ignore_errors=True)
            dirs.remove('__pycache__')


def run_pytest(workdir: str, selectors: Sequence[str]) -> Tuple[int, str]:
    env = dict(os.environ)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    # 选择器写成一个字符串（可以带 `-k 'xxx'`），这里按 shell 规则拆开 ——
    # 否则 "<路径> -k expr" 会被当成一个文件名，pytest 直接报"文件不存在"。
    args = []
    for selector in selectors:
        args.extend(shlex.split(selector))
    proc = subprocess.run(
        [sys.executable, '-m', 'pytest'] + args
        + ['-q', '--no-header', '-p', 'no:cacheprovider'],
        cwd=workdir, env=env, capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=900,
    )
    text = (proc.stdout or '') + (proc.stderr or '')
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    summary = next((line for line in reversed(lines)
                    if 'passed' in line or 'failed' in line or 'error' in line
                    or 'no tests ran' in line), '(没有摘要行)')
    return proc.returncode, summary


def verdict(returncode: int, summary: str) -> str:
    """把一次 pytest 运行判成 caught / SURVIVED / ERRORS-ONLY / HARNESS。"""
    lowered = summary.lower()
    if 'no tests ran' in lowered or returncode in (4, 5):
        return 'HARNESS'
    if 'failed' in lowered:
        return 'caught'
    if returncode != 0:
        return 'ERRORS-ONLY'
    return 'SURVIVED'


def main() -> int:
    parser = argparse.ArgumentParser(description='把守卫改坏，确认测试会失败')
    parser.add_argument('--list', action='store_true', help='只列出变异')
    parser.add_argument('--only', default='', help='只跑名字里含这段文字的变异')
    args = parser.parse_args()

    selected = [item for item in MUTATIONS if args.only in item.name]
    if args.list:
        for item in selected:
            print(f'{item.name}  <- {item.path}  ({item.why})')
            for selector in item.selectors:
                print(f'    {selector}')
        return 0
    if not selected:
        print(f'没有名字含 {args.only!r} 的变异')
        return 1

    workdir = tempfile.mkdtemp(prefix='idle_guard_mutation_')
    repo = os.path.join(workdir, 'repo')
    print(f'临时副本：{repo}')
    try:
        shutil.copytree(REPO, repo, ignore=shutil.ignore_patterns(
            '.git', '.pytest_cache', '__pycache__', '.workbuddy', '*.mcdr'))
        baselines = {}
        for item in selected:
            baselines.setdefault(item.path, open(os.path.join(repo, item.path),
                                                 encoding='utf-8').read())

        caught, survived, broken, noisy = 0, [], [], []
        for item in selected:
            path = os.path.join(repo, item.path)
            original = baselines[item.path]
            digest = hashlib.sha256(original.encode('utf-8')).hexdigest()[:12]
            try:
                mutated = item.transform(original)
            except LookupError as error:
                broken.append(item.name)
                print(f'  锚点失效    {item.name:<24} {error}')
                continue
            try:
                with open(path, 'w', encoding='utf-8', newline='\n') as handle:
                    handle.write(mutated)
                purge_bytecode(repo)
                returncode, summary = run_pytest(repo, item.selectors)
            finally:
                with open(path, 'w', encoding='utf-8', newline='\n') as handle:
                    handle.write(original)
                purge_bytecode(repo)
            outcome = verdict(returncode, summary)
            if outcome == 'caught':
                caught += 1
                print(f'  已抓住      {item.name:<24} sha256={digest} {summary}')
            elif outcome == 'SURVIVED':
                survived.append(item.name)
                print(f'  存活(!)     {item.name:<24} sha256={digest} {summary}')
            elif outcome == 'ERRORS-ONLY':
                noisy.append(item.name)
                print(f'  只报错(!)   {item.name:<24} sha256={digest} {summary}')
            else:
                broken.append(item.name)
                print(f'  选择器失效  {item.name:<24} {summary}')

        print()
        print(f'caught {caught} / {len(selected)}')
        if survived:
            print('以下变异活了下来 —— 盯它的测试是装饰品：')
            for name in survived:
                print(f'  - {name}')
        if noisy:
            print('以下运行只产生错误、没有失败的断言（不算抓住）：')
            for name in noisy:
                print(f'  - {name}')
        if broken:
            print('以下变异施加不上（脚本的锚点过期了，与测试无关）：')
            for name in broken:
                print(f'  - {name}')
        if survived or noisy or broken:
            return 1
        print('每一条变异都被测试抓住了')
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main())
