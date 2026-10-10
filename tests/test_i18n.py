"""多语言（i18n）测试。

覆盖：
- 语言选择（auto 跟随 MCDR / 指定语言 / 不认识的语言退回兜底并提示）；
- 同语族兜底（zh_tw -> zh_cn）、缺键时退回 en_us、键完全不存在时返回键名；
- 用户覆盖文件（config/idle_guard/lang/<语言>.json）与内置文件合并；
- 两份内置语言文件的键必须一一对齐（漏翻译会被这条测试抓住）；
- 配置里的文案字段已经移除，文案全部来自 lang 文件。
"""
from __future__ import annotations

import json
import os

import pytest

from idle_guard import i18n
from idle_guard.config import AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry
from idle_guard.supervisor import Supervisor, State
from mc_test_utils import StubMCDR, free_port

LANG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'idle_guard', 'lang')


@pytest.fixture(autouse=True)
def clean_i18n():
    i18n.set_override_dir(None)
    i18n.clear_cache()
    i18n.pop_errors()
    yield
    i18n.set_override_dir(None)
    i18n.clear_cache()
    i18n.pop_errors()


# --------------------------------------------------------------------------- #
# 语言文件本身
# --------------------------------------------------------------------------- #

def test_builtin_catalogs_have_identical_keys():
    zh = json.load(open(os.path.join(LANG_DIR, 'zh_cn.json'), encoding='utf-8'))
    en = json.load(open(os.path.join(LANG_DIR, 'en_us.json'), encoding='utf-8'))
    assert set(zh) == set(en), f'两份语言文件的键不一致：zh-en={set(zh) - set(en)} en-zh={set(en) - set(zh)}'
    assert all(isinstance(value, str) for value in zh.values())


def test_available_languages():
    assert i18n.available_languages() == ['en_us', 'zh_cn']


# --------------------------------------------------------------------------- #
# 选语言
# --------------------------------------------------------------------------- #

def test_resolve_auto_follows_mcdr_language():
    assert i18n.resolve('auto', 'zh_cn')[0] == 'zh_cn'
    assert i18n.resolve('auto', 'en_us')[0] == 'en_us'
    assert i18n.resolve('', 'zh_cn')[0] == 'zh_cn'


def test_resolve_auto_accepts_region_variants():
    assert i18n.resolve('auto', 'zh-CN')[0] == 'zh_cn'
    assert i18n.resolve('auto', 'zh_tw')[0] == 'zh_cn'      # 同语族兜底
    assert i18n.resolve('auto', 'en_GB')[0] == 'en_us'


def test_resolve_auto_with_unknown_mcdr_language_uses_fallback():
    assert i18n.resolve('auto', 'fr_fr')[0] == 'en_us'
    assert i18n.resolve('auto', None)[0] == 'en_us'


def test_resolve_explicit_language():
    assert i18n.resolve('en_us', 'zh_cn')[0] == 'en_us'
    language, warning = i18n.resolve('klingon', 'zh_cn')
    assert language == 'en_us' and warning, '不认识的语言应当退回兜底语言并提示'
    assert 'klingon' in warning and 'zh_cn' in warning


# --------------------------------------------------------------------------- #
# 取文案
# --------------------------------------------------------------------------- #

def test_translate_both_languages():
    assert '已休眠' in i18n.translate('state.sleeping', 'zh_cn')
    assert 'sleeping' in i18n.translate('state.sleeping', 'en_us')


def test_translate_placeholders():
    text = i18n.translate('announce.waking', 'zh_cn', server='survival', player='Steve', eta=30)
    assert 'survival' in text and 'Steve' in text and '30' in text


def test_translate_missing_key_returns_key():
    assert i18n.translate('not.a.key', 'zh_cn') == 'not.a.key'


def test_translate_falls_back_to_english_for_missing_key(monkeypatch):
    """zh_cn 缺某个键时应当退回 en_us，而不是显示键名。"""
    monkeypatch.setattr(i18n, 'catalog', lambda language: {'only.in.en': 'EN'} if language == 'en_us' else {})
    assert i18n.translate('only.in.en', 'zh_cn') == 'EN'


def test_missing_placeholder_does_not_crash():
    assert i18n.translate('announce.opened', 'zh_cn')      # 不传参数也不应抛异常


# --------------------------------------------------------------------------- #
# 用户覆盖文件
# --------------------------------------------------------------------------- #

def test_override_file_merges_and_wins(tmp_path):
    lang_dir = tmp_path / 'lang'
    lang_dir.mkdir()
    (lang_dir / 'zh_cn.json').write_text(
        json.dumps({'fake.version_name': '§7打烊了'}, ensure_ascii=False), encoding='utf-8')
    i18n.set_override_dir(str(lang_dir))

    assert i18n.translate('fake.version_name', 'zh_cn') == '§7打烊了'      # 覆盖生效
    assert '已休眠' in i18n.translate('state.sleeping', 'zh_cn')            # 其余仍然来自内置文件


def test_override_file_can_add_new_language(tmp_path):
    lang_dir = tmp_path / 'lang'
    lang_dir.mkdir()
    (lang_dir / 'fr_fr.json').write_text(json.dumps({'state.running': 'en marche'}), encoding='utf-8')
    i18n.set_override_dir(str(lang_dir))

    assert 'fr_fr' in i18n.available_languages()
    assert i18n.translate('state.running', 'fr_fr') == 'en marche'
    assert i18n.resolve('fr_fr', 'zh_cn')[0] == 'fr_fr'


def test_broken_override_file_is_reported(tmp_path):
    lang_dir = tmp_path / 'lang'
    lang_dir.mkdir()
    (lang_dir / 'zh_cn.json').write_text('{ 这不是 json', encoding='utf-8')
    i18n.set_override_dir(str(lang_dir))

    assert '已休眠' in i18n.translate('state.sleeping', 'zh_cn')
    assert i18n.pop_errors(), '损坏的语言文件应当被记录（加载时会打日志）'


# --------------------------------------------------------------------------- #
# 与 Supervisor 的集成
# --------------------------------------------------------------------------- #

def build(language: str, mcdr_language: str = 'zh_cn'):
    config = Config(
        instance_name='survival',
        language=language,
        check_interval=9999.0,
        idle_timeout=9999.0,
        bus=BusConfig(bind=None),
        fake_server=FakeServerConfig(enabled=False, bind='127.0.0.1'),
        announce=AnnounceConfig(enabled=False),
        servers=[ServerEntry(name='survival', host='127.0.0.1', port=free_port(), role='self')],
    )
    stub = StubMCDR()
    stub.mcdr_language = mcdr_language
    return Supervisor(stub, config), stub


def test_supervisor_language_auto():
    instance, _ = build('auto', 'en_us')
    try:
        assert instance.language == 'en_us'
        assert instance.tr('state.running') == i18n.translate('state.running', 'en_us')
        assert instance.state_text(State.SLEEPING) == i18n.translate('state.sleeping', 'en_us')
    finally:
        instance.shutdown()


def test_supervisor_language_explicit():
    instance, _ = build('zh_cn', 'en_us')
    try:
        assert instance.language == 'zh_cn'
        assert '已休眠' in instance.state_text(State.SLEEPING)
        assert '已休眠' in instance.role_text('watch') or '只监视' in instance.role_text('watch')
    finally:
        instance.shutdown()


def test_supervisor_warns_on_unknown_language():
    instance, stub = build('klingon')
    try:
        assert instance.language == 'en_us'
        assert 'klingon' in stub.logger.text()
    finally:
        instance.shutdown()


def test_action_messages_are_localized():
    en_instance, _ = build('en_us')
    zh_instance, _ = build('zh_cn')
    try:
        ok, detail = en_instance.request_action('sleep', 'survival', by='test')
        assert not ok and 'no need to sleep' in detail, detail
        ok, detail = zh_instance.request_action('sleep', 'survival', by='test')
        assert not ok and '不用再休眠' in detail, detail
    finally:
        en_instance.shutdown()
        zh_instance.shutdown()


def test_config_has_no_text_templates():
    """配置里不应该再出现文案字段（文案统一放 lang 文件）。"""
    fields = set(Config.get_field_annotations())
    assert 'language' in fields
    announce_fields = set(AnnounceConfig.get_field_annotations())
    # sound 不是文案，是原版音效 id（无语言的标识符）
    assert announce_fields == {'enabled', 'warn_before_sleep', 'tell_attempt_players', 'sound'}
    fake_fields = set(FakeServerConfig.get_field_annotations())
    assert fake_fields == {'enabled', 'bind'}
    assert Config().check_interval == 600.0, '默认侦察间隔应为 10 分钟'
