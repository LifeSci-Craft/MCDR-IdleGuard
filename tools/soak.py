"""长时间运行的稳定性采样：反复驱动心跳与侦察，看线程数 / 对象数会不会慢慢涨。

用法：
    python tools/soak.py                      # 默认 1500 轮，无间隔
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
from idle_guard.supervisor import State, Supervisor  # noqa: E402
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
        min_uptime=0.0,
        grace_after_wake=0.0,
        startup_timeout=5.0,
        stop_timeout=3.0,
        port_release_timeout=3.0,
        bus=BusConfig(bind=None, token='soak', timeout=0.3),
        fake_server=FakeServerConfig(enabled=fake, bind='127.0.0.1'),
        announce=AnnounceConfig(enabled=False),
        servers=entries,
    )
    return Supervisor(StubMCDR(), config, plugin_version='soak')


def sample() -> tuple:
    gc.collect()
    return threading.active_count(), len(gc.get_objects())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--rounds', type=int, default=300, help='驱动多少轮心跳')
    parser.add_argument('--interval', type=float, default=0.0, help='每轮之间的间隔（秒）')
    parser.add_argument('--servers', type=int, default=2, help='配置里放几台子服')
    parser.add_argument('--fake-cycles', type=int, default=0,
                        help='另外做多少次"起假服务端 → 撤下"的循环')
    parser.add_argument('--sample-every', type=int, default=250, help='每多少轮采样一次')
    args = parser.parse_args()

    supervisor = build(args.servers, fake=args.fake_cycles > 0)
    print(f'轮数 {args.rounds}，子服 {args.servers} 台，间隔 {args.interval}s，'
          f'假服务端起停 {args.fake_cycles} 次')
    first = None
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
                if first is None:
                    first = (threads, objects)
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
        print(f'  结束采样：线程 {threads:>3}，对象 {objects:>7}')
        elapsed = time.monotonic() - started
        print(f'用时 {elapsed:.1f} 秒')

        if first is not None:
            delta_threads = threads - first[0]
            growth = (objects - first[1]) / max(1, first[1])
            print(f'\n结论：线程 {first[0]} → {threads}（{delta_threads:+d}），'
                  f'对象 {first[1]} → {objects}（{growth:+.1%}）')
            ok = delta_threads <= 0 and growth < 0.25
            print('✓ 没有观察到线程或内存持续增长' if ok
                  else '✗ 有增长趋势，需要进一步排查（看是哪个线程/对象在涨）')
            return 0 if ok else 1
        return 0
    finally:
        supervisor.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
