## 改了什么

<!-- 一两句话说明这次改动；涉及行为变化时写清「以前是 X，现在是 Y」。 -->

## 为什么

<!-- 问题是什么、影响谁。关联的 issue 用 `#编号` 引用。 -->

## 如何验证

<!--
写清你实际跑过的命令与结果，例如：
- `python -m pytest tests -q` → 528 passed
- `python tools/mutation_check.py` → caught 8/8
- `python tools/build.py && python tools/check_artifact.py` → 全部通过，sha256=d552639f…
- `python tools/mcdr_matrix.py`（真实 MCDR 2.13.0 / 2.14.1 / 2.15.0 / 2.15.7 / 2.16.0）
没跑的项请写明「没跑」以及原因，不要用「应该没问题」代替。
-->

## 检查项

- [ ] 改动有对应测试（或说明了为什么不必加）
- [ ] 用户可见的变化已写进 `CHANGELOG.md`
- [ ] 配置项变化已同步 `README.md` 与 `README_en.md`
- [ ] 改动过 `idle_guard/lang/*.json` 时两份语言文件的键与占位符仍然一致
- [ ] 运行时行为有变化时跑过跨版本矩阵（`tools/mcdr_matrix.py`）
- [ ] 打包器 / 产物有影响时跑过 `tools/build.py && python tools/check_artifact.py`
      （**注意**：产物变了要把仓库根的 `idle_guard-<版本>.mcdr` 一起更新，并在说明里写出新 sha256）
