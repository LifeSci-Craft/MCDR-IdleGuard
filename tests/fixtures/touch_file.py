#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试辅助：创建一个（空）文件，用来给 dummy_server.py 发"停止"信号。

用独立的脚本而不是 `python -c "..."`，是为了避开不同 shell 下的引号转义问题。
"""
from __future__ import annotations

import argparse
import os


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--path', required=True)
    args = parser.parse_args()
    directory = os.path.dirname(os.path.abspath(args.path))
    os.makedirs(directory, exist_ok=True)
    with open(args.path, 'w', encoding='utf-8'):
        pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
