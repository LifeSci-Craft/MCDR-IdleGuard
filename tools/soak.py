"""长时间运行的稳定性采样：反复驱动心跳与侦察，看线程数 / 对象数会不会慢慢涨。

用法：
    python tools/soak.py                      # 默认 300 轮，无间隔
    python tools/soak.py --rounds 5000 --interval 0.01
    python tools/soak.py --fake-cycles 50     # 顺便反复起停假服务端（真实占端口）

它在一个隔离的 Supervisor 上跑（StubMCDR，不接 MCDR、不起真实服务端），每 N 轮采样一次
`threading.active_count()` 与 `gc` 的对象数，最后给出趋势与结论。

为什么需要它：插件每 5 秒一轮心跳、休眠期间有假服务端线程、还要跟对端实例建连接 ——
跑上几天会不会有线程或内存慢慢爬，光靠单元测试看不出来。
"""
from __future__ import annotations

import argparse
import gc
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)                       # 插件包所在的仓库根
sys.path.insert(0, os.path.join(ROOT, 'tests'))  # 测试桩

from idle_guard.config import (  # noqa: E402
    AnnounceConfig, BusConfig, Config, FakeServerConfig, ServerEntry,
)
from idle_guard.supervisor import Supervisor  # noqa: E402
from mc_test_utils import StubMCDR, free_port  # noqa: E402


def build(servers: int, *, fake: bool) -> Supervisor:
    entries = []
    for index in range(servers):
        entries.append(ServerEntry(
            name=f'server{index + 1}',
            host='127.0.0.1',
            port=free_port(),                  # 没人监听的端口：探测会失败，正是想压的路径
            role='self' if index == 0 else 'watch',
            auto_sleep=True,
            auto_wake=True,
        ))
    config = Config(
        instance_name='soak',
        language='zh_cn',
        check_interval=0.5,
        state_tick=0.1,
        idle_timeout=3600.0,
        min_uptime=0.0,                    # v1.1.0 起"唤醒保护期"并进了这里（grace_after_wake 已删）
        startup_timeout=5.0,
        stop_timeout=3.0,
        port_release_timeout=3.0,
        bus=BusConfig(bind=None, token='soak', timeout=0.3),
        fake_server=FakeServerConfig(enabled=fake),   # 监听地址固定 0.0.0.0（bind 字段已删）
        announce=AnnounceConfig(enabled=False),
        servers=entries,
    )
    stub = StubMCDR()
    # 模拟"服务端没在跑"：这样才会走"假服务端接管端口"那条路（默认桩是运行中）
    stub.running = False
    return Supervisor(stub, config, plugin_version='soak')


def sample() -> tuple:
    gc.collect()
    return threading.active_count(), len(gc.get_objects())


#: 判定趋势前丢弃的预热比例：线程池、缓存、解释器自己的对象都在前段长出来，
#: 把它们算进去，一批长寿命对象也会被当成"一直在涨"。
WARMUP_RATIO = 0.2
#: 对象数增长阈值（兜底用：样本不足以分段时看整段首尾）
GROWTH_LIMIT = 0.25


def _mean(values: list) -> float:
    return sum(values) / max(1, len(values))


def judge_leak(samples: list) -> tuple:
    """用"丢弃预热后的首尾均值"判断有没有持续增长，返回 (是否通过, 趋势增长率)。

    ⚠️ 为什么不看首尾两个单点：`gc.get_objects()` 的噪声很大，某一轮多出几百个临时对象
    很正常，单点相减很容易"假绿"（也可能假红）。改成比较两段均值之后噪声被摊平，
    而且**丢弃前 20% 轮次**再看趋势 —— 剩下的增长才像是真的在泄漏。
    样本太少（例如冒烟跑法）时分不出三段，退回原来的整体 25% 阈值兜底。
    """
    first_round, last_round = samples[0][0], samples[-1][0]
    warmup_until = first_round + (last_round - first_round) * WARMUP_RATIO
    kept = [item for item in samples if item[0] > warmup_until]
    spread = last_round - first_round
    if len(kept) < 3:
        growth = (samples[-1][2] - samples[0][2]) / max(1, samples[0][2])
        print(f'泄漏判定：丢弃预热（第 {first_round}–{warmup_until:.0f} 轮，共 {spread} 轮）后只剩 '
              f'{len(kept)} 个采样，不足以分段，退回整体阈值 {GROWTH_LIMIT:.0%} 兜底：'
              f'对象 {samples[0][2]} → {samples[-1][2]}（{growth:+.1%}）')
        return growth < GROWTH_LIMIT, growth
    third = len(kept) // 3
    head, tail = kept[:third], kept[-third:]
    head_objects, tail_objects = _mean([item[2] for item in head]), _mean([item[2] for item in tail])
    head_threads, tail_threads = _mean([item[1] for item in head]), _mean([item[1] for item in tail])
    growth = (tail_objects - head_objects) / max(1.0, head_objects)
    print(f'泄漏判定：丢弃预热（第 {first_round}–{warmup_until:.0f} 轮，共 {spread} 轮）后留 '
          f'{len(kept)} 个采样；对象均值 前 1/3 {head_objects:.0f} → 后 1/3 {tail_objects:.0f}'
          f'（{growth:+.1%}），线程均值 前 1/3 {head_threads:.2f} → 后 1/3 {tail_threads:.2f}'
          f'（{tail_threads - head_threads:+.2f}），阈值 {GROWTH_LIMIT:.0%}')
    # 线程用"没有多出半个线程"当门槛：单点抖动多是 +1，取均值后半个线程已经能盖住它
    ok = growth < GROWTH_LIMIT and (tail_threads - head_threads) < 0.5
    return ok, growth


def main() -> int:
    # 输出被重定向（管道 / 文件）时，Windows 会按 locale 编码 stdout（中文机器上是 GBK），
    # 打印 ✓ / ✗ 会直接 UnicodeEncodeError 崩掉 —— 而 CI 与自动跑法恰恰都是重定向调用的。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser()
    parser.add_argument('--rounds', type=int, default=300, help='驱动多少轮心跳')
    parser.add_argument('--interval', type=float, default=0.0, help='每轮之间的间隔（秒）')
    parser.add_argument('--servers', type=int, default=2, help='配置里放几台子服')
    parser.add_argument('--fake-cycles', type=int, default=0,
                        help='另外做多少次"起假服务端 → 撤下"的循环')
    parser.add_argument('--sample-every', type=int, default=100, help='每多少轮采样一次')
    parser.add_argument('--real-probe', action='store_true',
                        help='用真实的状态查询（慢；默认用一个"立即返回不可达"的替身，'
                             '这样压的是心跳/状态机/线程，而不是网络超时）')
    args = parser.parse_args()

    if not args.real_probe:
        import idle_guard.supervisor as supervisor_module
        from idle_guard.mcproto import PingResult
        supervisor_module.status_ping = (
            lambda host, port, timeout=1.5, **kwargs: PingResult(online=False, error='soak: 模拟不可达')
        )

    supervisor = build(args.servers, fake=args.fake_cycles > 0)
    print(f'轮数 {args.rounds}，子服 {args.servers} 台，间隔 {args.interval}s，'
          f'假服务端起停 {args.fake_cycles} 次')
    samples = []                      # (轮次, 线程数, 对象数)：趋势判定要用整条序列，不只首尾
    try:
        supervisor.start()
        managed = list(supervisor._managed.values())
        started = time.monotonic()
        for round_index in range(1, args.rounds + 1):
            supervisor._tick_states()
            supervisor.poll_once()
            for item in managed:
                supervisor._check_idle(item)
            if args.interval:
                time.sleep(args.interval)
            if round_index % args.sample_every == 0 or round_index == 1:
                threads, objects = sample()
                samples.append((round_index, threads, objects))
                print(f'  第 {round_index:>5} 轮：线程 {threads:>3}，对象 {objects:>7}')

        fake_started = fake_stopped = 0
        for cycle in range(args.fake_cycles):
            for item in managed:
                supervisor._ensure_fake_server(item, retry=False)
                if item.fake is not None and item.fake.running:
                    fake_started += 1
                    item.fake.stop()
                    fake_stopped += 1
        if args.fake_cycles:
            print(f'  假服务端起停：起 {fake_started} 次 / 停 {fake_stopped} 次')

        threads, objects = sample()
        samples.append((args.rounds + 1, threads, objects))
        print(f'  结束采样：线程 {threads:>3}，对象 {objects:>7}')
        elapsed = time.monotonic() - started
        print(f'用时 {elapsed:.1f} 秒')

        ok, growth = judge_leak(samples)
        start_threads, start_objects = samples[0][1], samples[0][2]
        delta_threads = threads - start_threads
        overall = (objects - start_objects) / max(1, start_objects)
        print(f'\n结论：线程 {start_threads} → {threads}（{delta_threads:+d}），'
              f'对象 {start_objects} → {objects}（首尾 {overall:+.1%}，趋势 {growth:+.1%}）')
        print('✓ 没有观察到线程或内存持续增长' if ok
              else '✗ 有增长趋势，需要进一步排查（看是哪个线程/对象在涨）')
        return 0 if ok else 1
    finally:
        supervisor.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
