"""界面渲染：每一屏都是一个「框」——顶栏 + 内容 + 底线，顶栏与底线**同宽**。

一份行数据，两种渲染（读者不同，需求正好相反）：

| | 谁看 | 补齐方式 | 细节放哪 |
|---|---|---|---|
| 控制台 / 日志 | 管理员 | 按**显示宽度**（`east_asian_width`，中文算 2 列） | 印在行上（控制台不能悬停） |
| 游戏内聊天 | 玩家 | 按**字宽模型**（比例字体连 ASCII 都不等宽） | 收进浮窗，参数、副作用、可选项都在那里 |

配色由「这一块代表什么」决定，同一件事在所有界面同色：**金色 = 顶栏/底线的框架**，
**aqua = 可操作/可点**，**绿 = 正常运行**，**红 = 有问题**，**黄 = 正在变动**，**灰 = 只是信息**。
没有一处按文本内容猜颜色。

浮窗是手工断行的：客户端只在**空格**处折行，中文句子没有空格，写成一条会直接画出屏幕，
所以每个浮窗行都必须自己 ≤ 32 显示列（`wrap_hover` 负责）。
"""
from __future__ import annotations

import unicodedata
from typing import List, Sequence

from mcdreforged.api.all import RColor, RText, RTextBase, RTextList

#: 插件名（顶栏、聊天前缀都用它，随插件元数据）
PLUGIN_NAME = 'Idle Guard'

#: 浮窗每行最多多少显示列（中文算 2 列）
HOVER_WIDTH = 32


# --------------------------------------------------------------------------- #
# 宽度
# --------------------------------------------------------------------------- #

def strip_codes(text: str) -> str:
    """去掉 § 颜色码（量宽度时要先去掉，否则颜色码会被当成字符）。"""
    out: List[str] = []
    index = 0
    while index < len(text):
        if text[index] == '§' and index + 1 < len(text):
            index += 2
            continue
        out.append(text[index])
        index += 1
    return ''.join(out)


def display_width(text: str) -> int:
    """等宽字体里的显示宽度：汉字/全角算 2 列，其余算 1 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
               for ch in strip_codes(text))


def pad(text: str, width: int) -> str:
    """按显示宽度右侧补空格（控制台、日志用）。"""
    return text + ' ' * max(0, width - display_width(text))


#: 比例字体里明显比 `A` 窄的字形（单位 1/4 个字母）：原版字库实测
_HELP_GLYPH_QUARTERS = {'i': 3, 'l': 3, 't': 3, 'f': 3, '!': 1}


def glyph_width(text: str) -> int:
    """游戏内比例字体的近似宽度（单位：1/4 个字母）。"""
    total = 0
    for ch in strip_codes(text):
        if unicodedata.east_asian_width(ch) in ('W', 'F'):
            total += 8
        elif ch in (' ', '.'):
            total += 2
        else:
            total += _HELP_GLYPH_QUARTERS.get(ch, 4)
    return total


def pad_glyph(text: str, quarters: int) -> str:
    """按字宽模型右侧补空格（游戏内帮助页用；空格按 1/4 个字母算宽的字体取半格）。"""
    deficit = quarters - glyph_width(text)
    if deficit <= 0:
        return text
    return text + ' ' * ((deficit + 3) // 4)


# --------------------------------------------------------------------------- #
# 浮窗文本
# --------------------------------------------------------------------------- #

def wrap_hover(lines: Sequence[str], width: int = HOVER_WIDTH) -> str:
    """把浮窗文本手工断行（中文没有空格，客户端不会自己折行）。"""
    wrapped: List[str] = []
    for line in lines:
        current = ''
        for ch in line:
            if display_width(current) + display_width(ch) > width:
                wrapped.append(current)
                current = ch
            else:
                current += ch
        wrapped.append(current)
    return '\n'.join(wrapped)


# --------------------------------------------------------------------------- #
# 顶栏 / 底线
#
# 一屏是一个"框"：顶栏印插件名与版本、底线印这一屏的提示，两条**同宽**，
# 两侧都用金色 `=` 填满，所以名字/提示都落在正中（`==== 提示文字 ====` 那种观感）。
# 宽度不写死：由本屏最宽的一行现算（顶栏、底线、正文共用同一个宽度），
# 保证"框"正好包住内容，也不会比内容短。
# --------------------------------------------------------------------------- #

#: 游戏内用字宽模型时，一个填充字符（`=`）等于多少个宽度单位
FILL_UNIT = 4


def measure(text: str, chat: bool) -> int:
    """按渲染方式量宽度：控制台/日志走显示宽度，游戏内走字宽模型。"""
    return glyph_width(text) if chat else display_width(text)


def _ceil_div(value: int, divisor: int) -> int:
    return -(-value // divisor)


def frame_cells(text: str, chat: bool) -> int:
    """这段文字占多少个「填充位」（一个 `=` 就是一个填充位）。"""
    return _ceil_div(measure(text, chat), FILL_UNIT if chat else 1)


def frame_width(chat: bool, texts: Sequence[str]) -> int:
    """本屏所有行里最宽的那一条，折算成填充位。"""
    return max((frame_cells(text, chat) for text in texts), default=0)


def frame_line(center: RTextBase, width: int, *, chat: bool, spacer: bool = True) -> RTextBase:
    """把 ``center`` 居中夹在两侧金色 `=` 里，整行正好 ``width`` 个填充位。

    ``spacer=False`` 时不加空格、也不放内容（控制台那条素线就是这么来的）。
    """
    total = max(0, width)
    center_cells = frame_cells(center.to_plain_text(), chat)
    gaps = 2 if spacer else 0
    free = max(0, total - center_cells - gaps)
    left = free // 2
    right = free - left
    parts: List[RTextBase] = [RText('=' * left, RColor.gold)]
    if spacer:
        parts.append(RText(' '))
    parts.append(center)
    if spacer:
        parts.append(RText(' '))
    parts.append(RText('=' * right, RColor.gold))
    return RTextList(*parts)


def title_text(version: str) -> RTextBase:
    """顶栏中间那段：插件名 aqua + 版本黄（所有界面共用）。"""
    return RTextList(RText(PLUGIN_NAME, RColor.aqua), RText(f' v{version}', RColor.yellow))

