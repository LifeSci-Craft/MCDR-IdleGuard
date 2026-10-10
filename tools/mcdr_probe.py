"""在无 tty 的环境里拉起 MCDR、按脚本发送命令、收集输出。

用法（示例）：
    python tools/mcdr_probe.py <MCDR工作目录> --wait 8 --send "!!ig status" --sleep 3 --timeout 30

参数：
    workdir         MCDR 实例目录（含 config.yml）
    --send CMD      依次发送的命令（可重复）
    --wait N        首次发送前的等待秒数（默认 8）
    --sleep N       每条命令之间的间隔秒数（默认 2）
    --timeout N     MCDR 最长运行秒数（默认 25）
    --grep PATTERN  只打印匹配该正则的输出行
    --out FILE      输出文件（默认 <workdir>/run_out.txt）
    --mcdr EXE      用哪个 MCDR 可执行文件（默认本项目 venv 里的那个；跨版本矩阵会换）
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

VENV_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            '.workbuddy', 'env', 'venv', 'Scripts')
MCDR_EXE = os.path.join(VENV_SCRIPTS, 'mcdreforged.exe')


def kill_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)],
                       capture_output=True, check=False)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('workdir')
    parser.add_argument('--send', action='append', default=[])
    parser.add_argument('--wait', type=float, default=8.0)
    parser.add_argument('--sleep', type=float, default=2.0)
    parser.add_argument('--timeout', type=float, default=25.0)
    parser.add_argument('--grep', default=None)
    parser.add_argument('--out', default=None)
    parser.add_argument('--extra-args', default='--no-server-start')
    parser.add_argument('--mcdr', default=MCDR_EXE)
    args = parser.parse_args()

    workdir = os.path.abspath(args.workdir)
    out_path = args.out or os.path.join(workdir, 'run_out.txt')
    out_file = open(out_path, 'w', encoding='utf-8', errors='replace')

    if args.extra_args:
        # Git Bash 下 python 不能直接解析 shell 语法，这里做一次简单切分
        extra = args.extra_args.split()
    else:
        extra = []

    process = subprocess.Popen(
        [args.mcdr, 'start', *extra],
        cwd=workdir, stdin=subprocess.PIPE, stdout=out_file, stderr=subprocess.STDOUT,
        text=True, encoding='utf-8', errors='replace', bufsize=1,
    )
    started = time.monotonic()
    try:
        time.sleep(args.wait)
        for command in args.send:
            if process.poll() is not None:
                break
            print(f'[probe] 发送命令: {command}', file=sys.stderr)
            try:
                process.stdin.write(command + '\n')
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                break
            time.sleep(args.sleep)
        remaining = args.timeout - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)
    finally:
        kill_tree(process)
        try:
            process.stdin.close()
        except OSError:
            pass
        out_file.close()

    print(f'[probe] 输出已写入 {out_path}', file=sys.stderr)
    pattern = re.compile(args.grep) if args.grep else None
    with open(out_path, 'r', encoding='utf-8', errors='replace') as handle:
        for line in handle:
            line = line.rstrip('\n')
            if pattern is None or pattern.search(line):
                print(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
