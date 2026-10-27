"""去重，用 set 做成员判断 —— 线性复杂度，和 dedupe_list.py 干同一件事。

用法：python examples/dedupe_set.py [N]
"""

import sys


def dedupe(items):
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6000
    data = [(i * 7919) % (n // 2) for i in range(n)]
    print(len(dedupe(data)))


if __name__ == "__main__":
    main()
