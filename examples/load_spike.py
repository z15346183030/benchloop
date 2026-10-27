"""在后台制造一段 CPU 负载，用来演示「机器状态随时间变化」。

用法：python examples/load_spike.py <等待秒数> <持续秒数>

它先睡一会儿，然后开几个进程把 CPU 占满，到点自己退出。
配合 benchloop 可以看出一件事：机器变慢的时候，是只慢了一条命令，还是两条一起慢。
"""

import multiprocessing
import sys
import time


def spin(seconds):
    end = time.perf_counter() + seconds
    total = 0
    while time.perf_counter() < end:
        total += 1


def main():
    delay = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    hold = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
    workers = multiprocessing.cpu_count()
    time.sleep(delay)
    procs = [multiprocessing.Process(target=spin, args=(hold,)) for _ in range(workers)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()


if __name__ == "__main__":
    main()
