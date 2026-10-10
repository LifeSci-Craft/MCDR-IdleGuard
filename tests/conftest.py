"""测试公共配置：把插件目录与测试辅助模块加入 import 路径。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for extra in (ROOT, os.path.join(ROOT, 'tests')):
    if extra not in sys.path:
        sys.path.insert(0, extra)
