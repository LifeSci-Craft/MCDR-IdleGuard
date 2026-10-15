"""打包器的回归测试（REPO-19 / REPO-20）。

两条都是"打包器自己的承诺与实现对不上"：

* **REPO-19**：`build.py` 的 docstring 与 stdout 都写「CHANGELOG 只留最新一版」，
  而 `build_mcdr()` 以前对它走普通 `packaged_bytes` —— 仓库里的文件恰好只有一节，
  所以看不出问题；一旦某次发版忘了删，历史条目会全部进包，而 `check_artifact.py` 也不查。
* **REPO-20**：`_zip_info()` 漏了 `create_system`，它由打包机自己决定（win32 = 0/FAT，
  其他 = 3/unix），于是同一棵源码在 Windows 与 Linux 上打出不同字节 ——
  "按 tag 重新构建、比对 sha256"这条验证方式跨系统就失败。
"""
from __future__ import annotations

import json
import os
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tools.build as build  # noqa: E402


def test_packaged_changelog_keeps_only_the_latest_section(tmp_path):
    path = tmp_path / 'CHANGELOG.md'
    path.write_text(
        '# CHANGELOG\n\n说明。\n\n---\n\n'
        '## v1.2.2\n\n### 修复\n\n- 新的一条\n\n'
        '## v1.2.1\n\n### 修复\n\n- 假的旧条目 FAKE_OLD_SECTION\n',
        encoding='utf-8',
    )
    text = build.packaged_changelog(str(path)).decode('utf-8')
    assert [line for line in text.splitlines() if line.startswith('## ')] == ['## v1.2.2'], text
    assert 'v1.2.2' in text
    assert 'FAKE_OLD_SECTION' not in text, '历史条目原样进包了'
    assert text.startswith('# CHANGELOG'), '标题被裁掉了'


def test_packaged_changelog_rejects_a_file_without_a_version_section(tmp_path):
    path = tmp_path / 'CHANGELOG.md'
    path.write_text('# CHANGELOG\n\n没有版本节。\n', encoding='utf-8')
    with pytest.raises(SystemExit):
        build.packaged_changelog(str(path))


def test_packaged_changelog_keeps_a_single_section_untouched(tmp_path):
    path = tmp_path / 'CHANGELOG.md'
    original = '# CHANGELOG\n\n## v9.9.9\n\n- 只有一节\n'
    path.write_text(original, encoding='utf-8')
    assert build.packaged_changelog(str(path)).decode('utf-8') == original


def test_every_zip_member_pins_create_system(tmp_path, monkeypatch):
    """不钉 create_system 时，同一棵源码在 Windows 与 Linux 上 sha256 不同。"""
    monkeypatch.setattr(build, 'OUTPUT_DIR', str(tmp_path))
    target = build.build_mcdr(build.collect_files(), build.read_version())
    with zipfile.ZipFile(target) as archive:
        systems = {info.filename: info.create_system for info in archive.infolist()}
    assert systems, '产物是空的'
    assert set(systems.values()) == {3}, systems


def test_a_new_changelog_section_would_reach_the_artifact(tmp_path, monkeypatch):
    """反例自证：给源码里的 CHANGELOG 追加一节，产物里**不能**出现它。"""
    changelog = os.path.join(build.PACKAGE_DIR, build.CHANGELOG_NAME)
    # ⚠️ 一律按**二进制**读写：文本模式在 Windows 上会把 \n 翻译成 \r\n，恢复时就把仓库里
    # 本该是 LF 的文件改成了 CRLF（`.gitattributes` 里 `* text=auto eol=lf` 说得很清楚），
    # 于是测试"跑过了"却留下一个被改动的源文件。
    with open(changelog, 'rb') as handle:
        original = handle.read()
    monkeypatch.setattr(build, 'OUTPUT_DIR', str(tmp_path))
    try:
        with open(changelog, 'wb') as handle:
            handle.write(original + b'\n## v0.0.1-fake\n\n- FAKE_SECTION_BODY\n')
        target = build.build_mcdr(build.collect_files(), build.read_version())
        with zipfile.ZipFile(target) as archive:
            packed = archive.read(f'{build.PLUGIN_ID}/{build.CHANGELOG_NAME}').decode('utf-8')
        assert 'FAKE_SECTION_BODY' not in packed, '追加的历史条目原样进包了'
        # 从元数据取期望的版本号，而不是写死一个：写死的话每次升版本都会在这里变红
        assert [line for line in packed.splitlines() if line.startswith('## ')] == [
            '## v{}'.format(build.read_version())]
    finally:
        # 绝不留下被改动的源码文件（这里不用 git，手写恢复）
        with open(changelog, 'wb') as handle:
            handle.write(original)
    with open(changelog, 'rb') as handle:
        assert handle.read() == original, '测试没有把源文件逐字节还原'


def test_the_artifact_metadata_carries_a_homepage_and_a_license(tmp_path, monkeypatch):
    """REPO-18 / REPO-33：主页与许可证在任何 MCDR 版本上都不该是空的。"""
    metadata = json.load(open(build.METADATA, encoding='utf-8'))
    assert metadata['author'] == ['Pau1am']
    assert metadata.get('link'), 'legacy 的 link 是空的（2.13–2.15 读的就是它）'
    assert (metadata.get('links') or {}).get('homepage') == metadata['link']
    assert metadata.get('license') == 'MIT'
    assert 'authors' not in metadata, '2.16 会把 authors 与 legacy author 合并成两个同名作者'
