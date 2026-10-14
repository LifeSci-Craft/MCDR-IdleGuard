# CHANGELOG

本插件所有值得一提的变更都记录在此。插件包内也附带同一份 `CHANGELOG.md`。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

**本文件只保留最新一个版本的条目**，更早版本的记录见
[GitHub Releases](https://github.com/LifeSci-Craft/MCDR-IdleGuard/releases)。

---

## v1.2.1

### 新增

- 配置新增 `velocity` 分组（`velocity.enabled` / `velocity.path`），可手动指定 `velocity.toml` 的路径
- `!!ig check` 增加 velocity 一行：读了哪个文件、读到几个子服，没找到时列出候选路径

### 变更

- 读到可用的 `[servers]` 时 `velocity.toml` 是子服清单的权威来源：缺的子服补进来，默认配置自带且
  未被改动过的示例条目（lobby / survival）按它改写或删除；用户改过的条目与自建条目一律不动
- 由同步生成的子服自动填入总线地址（`127.0.0.1:25600` 起，按名字排序），不再留空
- 旧的顶层 `velocity_config` 自动迁移为 `velocity` 分组（关闭值转为 `enabled: false`，其余转为 `path`）

### 修复

- `velocity.path` 指向的文件不存在时不再静默跳过：控制台给出 warning，`!!ig check` 指出文件不存在

### 验证

- 357 项测试通过
- MCDR 2.13.0 / 2.14.1 / 2.15.0 / 2.15.7 / 2.16.0 全部通过；2.12.0（低于最低版本）被依赖检查拒绝
- 产物自检通过（可复现、内容白名单、注释与 docstring 已剥离）；`ruff check .` 与 `tools/soak.py` 通过
