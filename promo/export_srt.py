# -*- coding: utf-8 -*-
"""从 ModSideDetector-宣传片.html 的 SCRIPT 导出字幕 dist/字幕.srt
用法：py -3 export_srt.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(HERE, "ModSideDetector-宣传片.html")
OUT = os.path.join(HERE, "dist", "字幕.srt")


def ts(ms):
    ms = int(round(ms))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main():
    src = open(HTML, encoding="utf-8").read()
    body = src.split("const SCRIPT = [", 1)[1].split("\n];", 1)[0]
    shots = [(int(m.group(1)), m.group(2))
             for m in re.finditer(r"\{d:(\d+),\s*sub:'([^']*)'", body)]

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    lines, t = [], 0
    n = 0
    for d, sub in shots:
        if sub:
            n += 1
            # 字幕比画面略晚出现（与 HTML 内 100ms 淡入一致），结束后留 200ms
            lines.append(f"{n}\n{ts(t + 100)} --> {ts(t + d - 150)}\n{sub}\n")
        t += d

    open(OUT, "w", encoding="utf-8").write("\n".join(lines))
    print(f"已写出 {OUT}（{n} 条字幕，总时长 {t} ms = {t/1000:.2f}s）")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
