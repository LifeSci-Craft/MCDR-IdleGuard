# CHANGELOG

本插件所有值得一提的变更都记录在此。插件包内也附带同一份 `CHANGELOG.md`。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

**本文件只保留最新一个版本的条目**，更早版本的记录见
[GitHub Releases](https://github.com/LifeSci-Craft/MCDR-IdleGuard/releases)。

---

## v1.2.2

### 修复

- 收到对端「已休眠 / 已开启」广播后，本实例立刻补一次对端状态查询：以前要等下一个状态心跳
  （`sleep.state_tick`，默认 5 秒），这段时间状态表会显示旧状态、开关列慢一拍
- 对**已经开着**的远端子服执行 `wake`（或对已休眠的执行 `sleep`）时，原样回报负责方的那句话
  （「已经开着了」/「不用再休眠了」）：以前会被统一换成「已开启，可以进入了」，而且还会白等一次状态变化

### 验证

- 360 项测试通过
- 真机端到端（Velocity + 2×Paper 1.21.4 + 2 个 MCDR 实例 + mineflayer 假玩家）：**40/40 全部通过**
- MCDR 2.13.0 / 2.14.1 / 2.15.0 / 2.15.7 / 2.16.0 全部通过；2.12.0（低于最低版本）被依赖检查拒绝
- 产物自检通过（可复现、内容白名单、注释与 docstring 已剥离）；`ruff check .` 与 `tools/soak.py` 通过
