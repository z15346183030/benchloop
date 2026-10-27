"""去重，用 list 做成员判断 —— 平方复杂度，故意写成慢的那一版。

用法：python examples/dedupe_list.py [N]
"""

import sys


def dedupe(items):
    seen = []
    out = []
    for item in items:
        if item not in seen:
            seen.append(item)
            out.append(item)
    return out


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6000
    # 每个值出现两次，保证有活干：列表里查一遍是线性的，总共就是 N²。
    data = [(i * 7919) % (n // 2) for i in range(n)]
    print(len(dedupe(data)))


if __name__ == "__main__":
    main()
