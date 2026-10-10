"""插件内多语言（i18n）支持。

文案不再放在 `config/idle_guard/config.json` 里，而是放在插件自带的
``lang/<语言代码>.json``：每个文件是一层扁平的 `"键": "模板"` 对象，
模板里用 ``{名字}`` 作占位符。

语言怎么选（由 config.json 的 ``language`` 字段决定）：

- ``"auto"``（默认）—— 跟随 MCDR 自己的语言设置（``config.yml`` 里的 ``language``）；
- ``"zh_cn"`` / ``"en_us"`` —— 强制指定；
- 其它 ``lang/`` 里存在的语言代码也可以直接填。
  指定了不存在的语言时会退回兜底语言（``en_us``）并在控制台提示可选值。

想改文案（MOTD、休眠提示等）又不想动插件本体：在
``<MCDR>/config/idle_guard/lang/<语言>.json`` 里只写要改的键，
它会与插件内置的同语言文件**合并**（同键以你的为准），插件升级不会覆盖它。
"""
from __future__ import annotations

import json
import os
import pkgutil
from typing import Dict, List, Optional, Sequence, Tuple

#: 找不到语言/键时的兜底语言
FALLBACK_LANGUAGE = 'en_us'
#: language 配置项的默认值：跟随 MCDR
AUTO = 'auto'
LANG_DIR_NAME = 'lang'

#: 插件内置的语言（对应包内 lang/<代码>.json；文件缺失会被忽略）
SHIPPED_LANGUAGES: Tuple[str, ...] = ('en_us', 'zh_cn')

#: 同语族之间的兜底顺序：某个语言缺某个键时先试这些
FAMILY_FALLBACKS: Dict[str, Sequence[str]] = {
    'zh_tw': ('zh_cn',),
    'zh_hk': ('zh_tw', 'zh_cn'),
    'zh_cn': ('zh_tw',),
    'en_gb': ('en_us',),
}

_PACKAGE = __package__ or 'idle_guard'
_OVERRIDE_DIR: Optional[str] = None
_CACHE: Dict[str, Dict[str, str]] = {}
_ERRORS: List[str] = []


# --------------------------------------------------------------------------- #
# 读取语言文件
# --------------------------------------------------------------------------- #

def _package_file(relative: str) -> Optional[bytes]:
    """读取插件包内的文件（同时兼容 .mcdr 打包插件与文件夹插件）。"""
    try:
        data = pkgutil.get_data(_PACKAGE, relative)
    except (OSError, ImportError, ValueError):
        data = None
    if data is not None:
        return data
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), *relative.split('/'))
    try:
        with open(path, 'rb') as handle:
            return handle.read()
    except OSError:
        return None


def _parse(data: bytes, source: str) -> Dict[str, str]:
    try:
        parsed = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        _ERRORS.append(f'{source}: {type(exc).__name__}: {exc}')
        return {}
    if not isinstance(parsed, dict):
        _ERRORS.append(f'{source}: 顶层必须是 JSON 对象')
        return {}
    return {str(key): value for key, value in parsed.items() if isinstance(value, str)}


def set_override_dir(directory: Optional[str]) -> None:
    """登记"用户自己的语言文件"目录（<config>/idle_guard/lang）。"""
    global _OVERRIDE_DIR
    _OVERRIDE_DIR = directory or None
    clear_cache()


def clear_cache() -> None:
    _CACHE.clear()


def pop_errors() -> List[str]:
    """取出并清空读取语言文件时积累的错误（供加载时打日志）。"""
    errors, _ERRORS[:] = list(_ERRORS), []
    return errors


def catalog(language: str) -> Dict[str, str]:
    """取某语言的完整文案表：内置文件 + 用户覆盖文件（后者优先）。"""
    cached = _CACHE.get(language)
    if cached is not None:
        return cached
    messages: Dict[str, str] = {}
    relative = f'{LANG_DIR_NAME}/{language}.json'
    data = _package_file(relative)
    if data is not None:
        messages.update(_parse(data, relative))
    if _OVERRIDE_DIR:
        path = os.path.join(_OVERRIDE_DIR, f'{language}.json')
        if os.path.isfile(path):
            try:
                with open(path, 'rb') as handle:
                    messages.update(_parse(handle.read(), path))
            except OSError as exc:
                _ERRORS.append(f'{path}: {type(exc).__name__}: {exc}')
    _CACHE[language] = messages
    return messages


def available_languages() -> List[str]:
    """可用的语言代码列表（内置的 + 用户覆盖目录里出现的）。"""
    codes = {code for code in SHIPPED_LANGUAGES if catalog(code)}
    if _OVERRIDE_DIR and os.path.isdir(_OVERRIDE_DIR):
        try:
            for name in os.listdir(_OVERRIDE_DIR):
                if name.endswith('.json'):
                    codes.add(name[:-5])
        except OSError:
            pass
    return sorted(codes) or [FALLBACK_LANGUAGE]


# --------------------------------------------------------------------------- #
# 语言选择
# --------------------------------------------------------------------------- #

def normalize(code) -> str:
    """把 "zh-CN" / " ZH_cn " 之类统一成 "zh_cn"。"""
    if not isinstance(code, str):
        return ''
    return code.strip().lower().replace('-', '_')


def _match(code: str, available: Sequence[str]) -> Optional[str]:
    if not code:
        return None
    if code in available:
        return code
    for fallback in FAMILY_FALLBACKS.get(code, ()):
        if fallback in available:
            return fallback
    base = code.split('_')[0]
    for candidate in available:
        if candidate.split('_')[0] == base:
            return candidate
    return None


def _last_resort(available: Sequence[str]) -> str:
    if FALLBACK_LANGUAGE in available:
        return FALLBACK_LANGUAGE
    return available[0] if available else FALLBACK_LANGUAGE


def resolve(setting, mcdr_language=None) -> Tuple[str, Optional[str]]:
    """由配置项解析出真正使用的语言。

    返回 ``(语言代码, 提示消息或 None)``：提示消息非空表示配置里写的语言不认识，
    调用方应当把它打到控制台（消息已按兜底语言格式化好）。
    """
    languages = available_languages()
    wanted = normalize(setting)

    if wanted in ('', AUTO):
        return _match(normalize(mcdr_language), languages) or _last_resort(languages), None

    matched = _match(wanted, languages)
    if matched is not None:
        return matched, None

    fallback = _last_resort(languages)
    message = translate(
        'language.unsupported', fallback,
        value=str(setting), fallback=fallback, options=', '.join([AUTO] + list(languages)),
    )
    return fallback, message


# --------------------------------------------------------------------------- #
# 取文案
# --------------------------------------------------------------------------- #

def _lookup(key: str, language: str) -> Optional[str]:
    """按「本语言 -> 同语族 -> 兜底语言」的顺序找模板，找不到返回 None。"""
    template = catalog(language).get(key)
    if template is not None:
        return template
    for fallback in FAMILY_FALLBACKS.get(language, ()):
        template = catalog(fallback).get(key)
        if template is not None:
            return template
    if language != FALLBACK_LANGUAGE:
        return catalog(FALLBACK_LANGUAGE).get(key)
    return None


def translate(key: str, language: str = FALLBACK_LANGUAGE, **kwargs) -> str:
    """取一条文案并填充占位符。键不存在时直接返回键名（方便定位漏翻译）。"""
    template = _lookup(key, normalize(language) or FALLBACK_LANGUAGE)
    if template is None:
        return key
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError, AttributeError, TypeError):
        return template


def has(key: str, language: str = FALLBACK_LANGUAGE) -> bool:
    return _lookup(key, normalize(language) or FALLBACK_LANGUAGE) is not None
