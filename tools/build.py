"""打包 Idle Guard：生成 .mcdr 打包插件（文件夹插件形态就是仓库根目录本身）。

用法：
    python tools/build.py            # 生成 <仓库根>/idle_guard-<version>.mcdr
    python tools/build.py --list     # 只打印打包清单

仓库布局（与 MCDR-ServerLogFilter / MCDR-ModUpdateChecker 一致）：
仓库根**就是文件夹插件根**（`mcdreforged.plugin.json` + 同名包 `idle_guard/`），
所以"把仓库根整个放进 MCDR 的 plugins/ 下"即可用；README / docs / tests / tools 只是开发资料，
MCDR 不会碰它们。

发版标准（用户 2026-10-07 定的，跨项目）：
- **白名单**打包：只有明确列出的文件会进包（README 一律不进包）；
- 包内 `.py` 的 `#` 注释与 docstring **全部清空**（注释只留在仓库里给开发者看），
  清空但**保留空行**，所以安装版抛异常时的行号与仓库文件一致；
- 包内 CHANGELOG **只留最新一版**（`packaged_changelog()` 真去裁剪并断言只剩一节）；
- 文本文件统一成 LF、zip 时间戳与 `create_system` 固定，于是**同一个源码在任何操作系统上
  打两次都得到完全一样的字节**（谁都能"按 tag 重新构建、比对 sha256"来验证一个发布）。

自检：`python tools/check_artifact.py`（另见该文件）。
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import shutil
import tokenize
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_ID = 'idle_guard'
#: 打包产物落在仓库根：`idle_guard-<版本>.mcdr`
OUTPUT_DIR = ROOT
#: 元数据在仓库根（文件夹插件的外层）
METADATA = os.path.join(ROOT, 'mcdreforged.plugin.json')
#: 代码包目录
PACKAGE_DIR = os.path.join(ROOT, PLUGIN_ID)

#: 进包的包内根文件（白名单）；元数据单独从仓库根取
ROOT_FILES = ('requirements.txt', 'CHANGELOG.md', 'LICENSE')
#: 包内 CHANGELOG 的裁剪（只留最新一版）靠这个文件名识别
CHANGELOG_NAME = 'CHANGELOG.md'
#: 允许整目录打包的子目录：扩展名白名单
PACKAGE_DATA_DIRS = {'lang': ('.json',)}

#: zip 里写死的时间戳：否则同一个源码打两次得到不同的哈希，发布就无法被复验
FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
#: 按文本处理（统一成 LF）的扩展名
TEXT_SUFFIXES = ('.json', '.md', '.txt')


# --------------------------------------------------------------------------- #
# 剥注释 / docstring
# --------------------------------------------------------------------------- #

def _blank_comments(source: str) -> str:
    """把 `#` 注释清空，**保留这些行**。

    用 tokenize 而不是正则：字符串里的 `#`（URL、正则、颜色码）不会被误伤。
    """
    lines = source.splitlines(keepends=True)
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            row, col = token.start
            line = lines[row - 1]
            newline = '\n' if line.endswith('\n') else ''
            lines[row - 1] = line[:col].rstrip() + newline
    return ''.join(lines)


def _blank_docstrings(source: str) -> str:
    """清空 docstring（同样保留行）。body 里只剩 docstring 时补一个 `pass`，否则不能编译。"""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if not body:
            continue
        first = body[0]
        if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            continue
        header_line = getattr(node, 'lineno', None)
        if header_line is not None and first.lineno == header_line:
            continue          # `def f(): "doc"` 这种一行写法：清空会把定义本身删掉
        lines[first.lineno - 1] = ' ' * first.col_offset + 'pass\n' if len(body) == 1 else '\n'
        for index in range(first.lineno, first.end_lineno):
            lines[index] = '\n'
    return ''.join(lines)


def packaged_python(path: str) -> bytes:
    """一个 .py 进包时的字节：注释与 docstring 清空，行号不变，并确认仍然能编译。"""
    source = open(path, encoding='utf-8').read()
    stripped = _blank_comments(_blank_docstrings(source))
    try:
        compile(stripped, path, 'exec')
    except SyntaxError as error:
        raise SystemExit(f'剥离注释后 {path} 不能编译了：{error}') from error
    return stripped.encode('utf-8')


def packaged_bytes(path: str) -> bytes:
    """非 Python 文件的字节：文本统一成 LF（否则同一 tag 在 Windows/Linux 打出的包不一致）。"""
    data = open(path, 'rb').read()
    if path.endswith(TEXT_SUFFIXES):
        return data.replace(b'\r\n', b'\n')
    return data


def packaged_changelog(path: str) -> bytes:
    """进包的 CHANGELOG：**只留最新一个版本节**。

    包内只该带一个版本的条目（更早的在 Release 页上）—— 这里才是让它成真的地方。
    以前 `build_mcdr()` 对 CHANGELOG.md 走的是普通 `packaged_bytes`，而 docstring 与
    运行结束的打印都声称"只留最新一版"：仓库里的文件恰好被人手删成了一节，所以看不出来，
    一旦某次发版忘了删，历史条目会全部进包（MCDR-ModUpdateChecker 的 1.6.0 就这样带进过
    28,363 字节，占产物 29.4%）。两条守卫让它**不可能**静默发生：一节都没有 → 报错退出；
    裁剪后仍然不是恰好一节 → 报错退出。
    """
    text = packaged_bytes(path).decode('utf-8')
    lines = text.splitlines(keepends=True)
    headings = [index for index, line in enumerate(lines) if line.startswith('## ')]
    if not headings:
        raise SystemExit(f'{os.path.basename(path)} 里没有任何 "## <版本>" 节，无法裁剪')
    if len(headings) > 1:
        text = ''.join(lines[:headings[1]])
    if sum(1 for line in text.splitlines() if line.startswith('## ')) != 1:
        raise SystemExit(f'{os.path.basename(path)} 裁剪后不是恰好一个版本节')
    return text.encode('utf-8')


# --------------------------------------------------------------------------- #
# 清单
# --------------------------------------------------------------------------- #

def collect_files() -> list:
    """返回相对包根的路径列表（子目录用 / 分隔）；元数据排在第一个。"""
    files = ['mcdreforged.plugin.json']
    for name in sorted(os.listdir(PACKAGE_DIR)):
        full = os.path.join(PACKAGE_DIR, name)
        if os.path.isfile(full):
            if name in ROOT_FILES or name.endswith('.py'):
                files.append(name)
        elif os.path.isdir(full) and name in PACKAGE_DATA_DIRS:
            suffixes = PACKAGE_DATA_DIRS[name]
            for sub in sorted(os.listdir(full)):
                if os.path.isfile(os.path.join(full, sub)) and sub.endswith(suffixes):
                    files.append(f'{name}/{sub}')
    return files


def source_path(name: str) -> str:
    """某个清单条目在仓库里的实际位置（元数据在仓库根，其余在同名包里）。"""
    if name == 'mcdreforged.plugin.json':
        return METADATA
    return os.path.join(PACKAGE_DIR, *name.split('/'))


def read_version() -> str:
    with open(METADATA, encoding='utf-8') as handle:
        return json.load(handle)['version']


# --------------------------------------------------------------------------- #
# 两种形态
# --------------------------------------------------------------------------- #

def _zip_info(arcname: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(arcname, date_time=FIXED_ZIP_TIMESTAMP)
    info.external_attr = 0o644 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    # ⚠️ 这个字段不写的话由打包机自己决定：win32 写 0（FAT），其他平台写 3（unix），
    # 于是同一棵干净源码在 Windows 与 Linux 上打出**不同字节**（实测 sha256 前缀
    # c515609a… / 4db0ecb3…，大小一样），而"按 tag 重新构建、比对 sha256"正是本仓库
    # 与另两个插件仓库一起承诺的验证方式。钉成 3 之后跨操作系统也能逐字节复现。
    info.create_system = 3              # unix
    return info


def build_mcdr(files: list, version: str) -> str:
    """MCDR 打包插件（.mcdr）：元数据在压缩包根目录，代码在同名包目录里。"""
    target = os.path.join(OUTPUT_DIR, f'{PLUGIN_ID}-{version}.mcdr')
    with zipfile.ZipFile(target, 'w') as zf:
        for name in files:
            path = source_path(name)
            arcname = name if name == 'mcdreforged.plugin.json' else f'{PLUGIN_ID}/{name}'
            if name.endswith('.py'):
                payload = packaged_python(path)
            elif name == CHANGELOG_NAME:
                payload = packaged_changelog(path)
            else:
                payload = packaged_bytes(path)
            zf.writestr(_zip_info(arcname), payload)
    return target


def folder_plugin_root() -> str:
    """文件夹插件形态 = **仓库根目录本身**（外层只有元数据与同名包，外加开发资料）。"""
    return ROOT


def copy_folder_plugin(target_dir: str) -> str:
    """把插件按文件夹插件形态铺到 ``target_dir``（= MCDR 的 plugins 目录）。

    只搬插件真正需要的东西（元数据 + 同名包），仓库里的 README / docs / tests / tools 不带过去。
    覆盖式复制，不做删除，方便在"批量删除会被本机策略拦下"的环境里使用。
    """
    outer = os.path.join(target_dir, PLUGIN_ID)
    inner = os.path.join(outer, PLUGIN_ID)
    os.makedirs(inner, exist_ok=True)
    shutil.copy2(METADATA, os.path.join(outer, 'mcdreforged.plugin.json'))
    # 旧布局（元数据曾在包内）的残留会让 MCDR 看到两份元数据，见到就清掉这一个文件
    stale = os.path.join(inner, 'mcdreforged.plugin.json')
    if os.path.isfile(stale):
        os.remove(stale)
    for name in collect_files():
        if name == 'mcdreforged.plugin.json':
            continue
        destination = os.path.join(inner, *name.split('/'))
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copy2(source_path(name), destination)
    return outer


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--list', action='store_true', help='只列出打包清单')
    args = parser.parse_args()

    files = collect_files()
    version = read_version()
    print(f'插件 {PLUGIN_ID} v{version}，打包清单（{len(files)} 个文件）：')
    for name in files:
        path = source_path(name)
        print(f'  - {name}  ({os.path.getsize(path)} bytes)')
    if args.list:
        return 0

    mcdr_path = build_mcdr(files, version)
    print()
    print(f'已生成打包插件：{os.path.relpath(mcdr_path, ROOT)}  ({os.path.getsize(mcdr_path)} bytes)')
    print('说明：包内 .py 的注释与 docstring 已清空（行号保留），README 不进包，'
          'CHANGELOG 只留最新一版（由 packaged_changelog() 裁剪并断言）。')
    print('安装方式：')
    print(f'  A) 把 {os.path.basename(mcdr_path)} 放进 MCDR 的 plugins/ 目录')
    print(f'  B) 把仓库根目录（含 mcdreforged.plugin.json 与同名包 {PLUGIN_ID}/）'
          f'复制成 MCDR 的 plugins/{PLUGIN_ID}/')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
