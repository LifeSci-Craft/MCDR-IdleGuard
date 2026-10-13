#!/usr/bin/env python3
"""校验打出来的 .mcdr：可复现、内容干净、注释确实被剥掉、而且真的能加载。

四件事分开查，因为它们各自会以不同的方式静默出错：

* **可复现**：打包器固定了 zip 时间戳与权限位、并把文本统一成 LF。同一个源码打两次必须
  字节相同 —— 否则"按 tag 重新构建再比 sha256"这句承诺就是假的。
* **内容**：只允许插件代码 + 元数据 + LICENSE + CHANGELOG 进包。白名单之外的任何东西
  （README、tests/、tools/、__pycache__）都是打包器的 bug。
* **不漏包**：源码目录 `idle_guard/` 下的每个 `*.py` 与 `lang/*.json` 都必须出现在产物成员里。
  白名单是手工维护的，"新增模块忘了加进白名单"会静默丢包 —— 必须在这里被拦下。
* **剥离**：注释与 docstring 属于仓库、不属于产物。这里对包内每个 .py 再跑一遍 tokenize
  与 ast —— 只看体积的话，"剥离器悄悄失效"是发现不了的。
* **可加载**：包内 mcdreforged.plugin.json 能解析，且 id / author 符合预期、version 与
  `idle_guard/__init__.py` 里的 `PLUGIN_VERSION` 一致；每个 .py 剥离之后仍然能编译
  （剥离用的是 tokenize + ast，出错就会变成用户那边的语法错误）。

用法::

    python tools/check_artifact.py            # 打两次到临时目录并逐项核对
    python tools/check_artifact.py path.mcdr  # 只查一个已有产物（不重新构建）
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import tokenize
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tools.build as build  # noqa: E402

PLUGIN_ID = build.PLUGIN_ID

#: 元数据里该写的作者（原样比对，多了少了都算错）
EXPECTED_AUTHOR = ['Pau1am']

#: 源码里版本号的写法（`idle_guard/__init__.py`）
VERSION_PATTERN = re.compile(r"^PLUGIN_VERSION\s*=\s*['\"]([^'\"]+)['\"]", re.M)

#: 允许出现在包里的条目（其余一律视为打包器 bug）。代码/数据在 PLUGIN_ID/ 目录下。
EXPECTED_ROOT = {'mcdreforged.plugin.json'}
EXPECTED_MEMBERS = {'CHANGELOG.md', 'LICENSE', 'requirements.txt', '__init__.py', 'bus.py',
                    'commands.py', 'config.py', 'diag.py', 'fakeserver.py', 'i18n.py', 'mcproto.py',
                    'models.py', 'remote.py', 'stats.py', 'supervisor.py', 'takeover.py', 'ui.py',
                    'velocity.py', 'lang/en_us.json', 'lang/zh_cn.json'}

#: 绝不能出现的根级条目：MCDR 拒绝加载带根级模块的包，这些不是"不整齐"而是"装了不能用"
FORBIDDEN_ROOTS = ('README.md', 'README-dev.md', 'pack.py', 'conftest.py', 'pytest.ini',
                   'ruff.toml', 'tests/', 'tools/', 'docs/', '__pycache__/')


def sha256(path: str) -> str:
    with open(path, 'rb') as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _members(path: str) -> list:
    with zipfile.ZipFile(path) as archive:
        return sorted(archive.namelist())


def _read(path: str, name: str) -> bytes:
    with zipfile.ZipFile(path) as archive:
        return archive.read(name)


def _packed_members(path: str) -> set:
    """产物里 PLUGIN_ID/ 目录下的成员（相对包目录，子目录用 / 分隔）。"""
    return {name[len(PLUGIN_ID) + 1:] for name in _members(path) if name.startswith(PLUGIN_ID + '/')}


def source_members() -> list:
    """源码目录里"本该进包"的每一个文件：`*.py` 与 `lang/*.json`。

    故意不查 ``build.collect_files()``：那正是白名单本身，拿它比对等于自己跟自己比。
    这里独立地把源码目录数一遍，才能发现"新加了模块但白名单没跟"。
    """
    members = []
    package = os.path.join(ROOT, PLUGIN_ID)
    for name in sorted(os.listdir(package)):
        full = os.path.join(package, name)
        if os.path.isfile(full):
            if name.endswith('.py'):
                members.append(name)
        elif os.path.isdir(full) and name == 'lang':
            members.extend(f'lang/{sub}' for sub in sorted(os.listdir(full))
                           if os.path.isfile(os.path.join(full, sub)) and sub.endswith('.json'))
    return members


def source_version(problems: list) -> str:
    """从 `idle_guard/__init__.py` 里读出 `PLUGIN_VERSION`（读不出来时记一条问题并返回空串）。

    为什么用文本匹配而不是 `import idle_guard`：那会连带导入 mcdreforged，
    于是这个自检只能在装好 MCDR 的解释器里跑（CI 里未必装）。
    """
    path = os.path.join(ROOT, PLUGIN_ID, '__init__.py')
    try:
        with open(path, encoding='utf-8') as handle:
            match = VERSION_PATTERN.search(handle.read())
    except OSError as error:
        problems.append(f'读不出源码版本（{path}）：{type(error).__name__}: {error}')
        return ''
    if match is None:
        problems.append(f'{path} 里找不到 PLUGIN_VERSION = "..."，无法核对产物版本')
        return ''
    return match.group(1)


def check_member_coverage(path: str, problems: list) -> None:
    """源码目录里有的，产物里必须都有 —— 防"新增模块忘记进白名单"式的静默丢包。"""
    packed = _packed_members(path)
    missing = [name for name in source_members() if name not in packed]
    if missing:
        problems.append(f'源码里有、产物白名单没包含（新增文件忘了加进白名单？）：{sorted(missing)}')


def check_contents(path: str, problems: list) -> dict:
    names = _members(path)
    roots = {name for name in names if '/' not in name}
    if roots != EXPECTED_ROOT:
        problems.append(f'压缩包根目录只有 {sorted(roots)}，期望 {sorted(EXPECTED_ROOT)}')
    for forbidden in FORBIDDEN_ROOTS:
        if any(name == forbidden.rstrip('/') or name.startswith(forbidden) for name in names):
            problems.append(f'包里不该出现 {forbidden}')
    members = _packed_members(path)
    if members != EXPECTED_MEMBERS:
        missing = EXPECTED_MEMBERS - members
        extra = members - EXPECTED_MEMBERS
        problems.append(f'包内文件不对：缺少 {sorted(missing)}，多出 {sorted(extra)}')
    metadata = json.loads(_read(path, 'mcdreforged.plugin.json').decode('utf-8'))
    if metadata.get('id') != PLUGIN_ID:
        problems.append(f'元数据 id 不是 {PLUGIN_ID}：{metadata.get("id")!r}')
    if metadata.get('author') != EXPECTED_AUTHOR:
        problems.append(f'元数据 author 不是 {EXPECTED_AUTHOR}：{metadata.get("author")!r}')
    # 版本号在源码里也有一份，两处对不上就说明发版时漏改了其中一个
    declared = source_version(problems)
    if declared and metadata.get('version') != declared:
        problems.append(f'产物版本 {metadata.get("version")!r} 与 {PLUGIN_ID}/__init__.py 的 '
                        f'PLUGIN_VERSION {declared!r} 不一致（发版时漏改了其中一个？）')
    return metadata


def check_stripped(path: str, problems: list) -> None:
    """包内每个 .py：不得有注释、不得有 docstring，且仍然能编译。"""
    for name in _members(path):
        if not name.endswith('.py'):
            continue
        source = _read(path, name).decode('utf-8')
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                problems.append(f'{name} 里还有注释：第 {token.start[0]} 行 {token.string[:40]!r}')
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                problems.append(f'{name} 里还有 docstring：第 {body[0].lineno} 行')
        try:
            compile(source, name, 'exec')
        except SyntaxError as error:
            problems.append(f'{name} 不能编译：{error}')


def member_hashes(path: str) -> dict:
    """产物里每个成员 -> 解包后内容的 sha256。

    这一层刻意不看 zip 本身（压缩级别、deflate 实现、时间戳都不参与），
    所以**换解释器、换 zlib 版本也应当一致** —— 是"可复现"里跨机器成立的那一半。
    """
    with zipfile.ZipFile(path) as archive:
        return {name: hashlib.sha256(archive.read(name)).hexdigest()
                for name in archive.namelist()}


def check_reproducible(tmp: str, problems: list) -> str:
    """在临时目录里打两次，逐成员内容与整包字节都要一致。返回第一次的产物路径。

    两个层次的结论分开报，措辞不混：**逐成员内容**是跨解释器 / 跨 zlib 都成立的那一层；
    **整包字节**更严（连 zip 头、压缩流都要求逐字节相同），同一解释器下必然成立，
    换解释器就可能天然不同 —— 那种情况下以逐成员一致为准。
    """
    original_output = build.OUTPUT_DIR
    hashes, paths = [], []
    try:
        build.OUTPUT_DIR = tmp
        for index in (1, 2):
            version = build.read_version()
            files = build.collect_files()
            target = build.build_mcdr(files, version)
            paths.append(target)
            hashes.append(sha256(target))
            if index == 1:
                first = os.path.join(tmp, 'copy1.mcdr')
                os.replace(target, first)
                paths[0] = first
    finally:
        build.OUTPUT_DIR = original_output

    first_members, second_members = member_hashes(paths[0]), member_hashes(paths[1])
    if first_members == second_members:
        print('  逐成员内容 sha256：一致（跨解释器 / 跨 zlib 也成立）')
    else:
        differing = sorted(set(first_members) ^ set(second_members))
        differing += sorted(name for name in set(first_members) & set(second_members)
                            if first_members[name] != second_members[name])
        problems.append(f'同一源码打两次，解包后内容不一致的成员：{differing}')

    if hashes[0] == hashes[1]:
        print('  整包字节 sha256：一致（更严的附加项：连 zip 头与压缩流都逐字节相同）')
    else:
        problems.append(f'整包字节不一致（同一解释器下这是打包不确定性的 bug；'
                        f'跨解释器 / 跨 zlib 时这一项天然会不同，此时以"逐成员内容一致"为准）：'
                        f'{hashes[0]} != {hashes[1]}')
    return paths[0]


def check_folder_layout(tmp: str, problems: list) -> None:
    """文件夹插件形态：仓库根就是插件根（外层只有元数据与同名包，外加开发资料）。

    仓库根会有 README / docs / tests / tools 这些开发资料（MCDR 不碰它们），
    所以这里分两步查：① 仓库根该有的东西都在、且没有外层 `__init__.py`；
    ② 真正铺到 `plugins/` 时（`copy_folder_plugin`）只剩元数据与同名包。
    """
    version = build.read_version()
    root = build.folder_plugin_root()
    entries = set(os.listdir(root))
    for needed in ('mcdreforged.plugin.json', build.PLUGIN_ID):
        if needed not in entries:
            problems.append(f'仓库根缺少文件夹插件必需的 {needed}')
    if os.path.exists(os.path.join(root, '__init__.py')):
        problems.append('仓库根出现了 __init__.py（MCDR 会不认这个文件夹插件）')
    inner = os.path.join(root, build.PLUGIN_ID)
    if not os.path.isfile(os.path.join(inner, '__init__.py')):
        problems.append(f'仓库根缺少 {build.PLUGIN_ID}/__init__.py')

    materialized = build.copy_folder_plugin(tmp)
    laid_out = sorted(os.listdir(materialized))
    if laid_out != sorted([build.PLUGIN_ID, 'mcdreforged.plugin.json']):
        problems.append(f'铺进 plugins/ 后外层应当只有元数据与同名包，实际 {laid_out}')
    staged_inner = os.path.join(materialized, build.PLUGIN_ID)
    for name in build.collect_files():
        if name == 'mcdreforged.plugin.json':
            continue
        if not os.path.isfile(os.path.join(staged_inner, *name.split('/'))):
            problems.append(f'铺进 plugins/ 后缺少 {name}')
    if os.path.isfile(os.path.join(staged_inner, 'mcdreforged.plugin.json')):
        problems.append('铺进 plugins/ 后包里多出一份元数据（MCDR 会看到两份）')
    print(f'  文件夹插件布局：{materialized}（v{version}）')


def main() -> int:
    # 输出被重定向（管道 / 文件）时，Windows 会按 locale 编码 stdout（中文机器上是 GBK），
    # 下面那些 ✗ 会直接 UnicodeEncodeError 崩掉 —— 失败信息反而看不见。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser()
    parser.add_argument('artifact', nargs='?', help='已有的 .mcdr；不给就重新构建两次')
    args = parser.parse_args()

    problems: list = []
    if args.artifact:
        paths = [args.artifact]
        print(f'检查已有产物：{args.artifact}')
        for path in paths:
            metadata = check_contents(path, problems)
            check_member_coverage(path, problems)
            check_stripped(path, problems)
            print(f'  {os.path.basename(path)}  size={os.path.getsize(path)} '
                  f'sha256={sha256(path)[:16]}…  version={metadata.get("version")}')
    else:
        print('构建两次并核对…')
        tmp = tempfile.mkdtemp(prefix='idle_guard_artifact_')
        try:
            path = check_reproducible(tmp, problems)
            metadata = check_contents(path, problems)
            check_member_coverage(path, problems)
            check_stripped(path, problems)
            print(f'  {os.path.basename(path)}  size={os.path.getsize(path)} '
                  f'sha256={sha256(path)[:16]}…  version={metadata.get("version")}')
            check_folder_layout(tmp, problems)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    if problems:
        print('\n发现问题：')
        for problem in problems:
            print(f'  ✗ {problem}')
        return 1
    print('\n全部通过：可复现（逐成员 + 整包）、内容白名单、无漏包、注释与 docstring 已剥离、'
          '元数据与源码版本一致、代码仍可编译。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
