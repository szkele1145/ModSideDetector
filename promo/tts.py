# -*- coding: utf-8 -*-
"""从 ModSideDetector-宣传片.html 的 SCRIPT 提取字幕，用 edge-tts 批量生成配音。

   · 文件名 = 镜头序号（01.mp3 .. NN.mp3），字幕为空的镜头跳过
   · **使用 edge-tts 默认语速**（不传 rate / pitch / volume 参数）
   · 用法: py -3 tts.py            复用已有文件，只补缺失
           py -3 tts.py --force    清空 audio/male、audio/female 后全量重建
                                   （改过文案 / 删过镜头后**必须**用它，否则旧编号文件会串音）
   · 需要联网；失败时先设代理：
       $env:HTTPS_PROXY="http://127.0.0.1:65532"; $env:HTTP_PROXY="http://127.0.0.1:65532"
"""
import asyncio
import json
import os
import re
import shutil
import sys

import edge_tts

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(HERE, "ModSideDetector-宣传片.html")

VOICES = {
    "male": "zh-CN-YunxiNeural",      # 男声，年轻自然
    "female": "zh-CN-XiaoxiaoNeural",  # 女声
}
RATE = "default"          # 明确记录：默认语速，不做任何加减速


def load_script():
    """从 HTML 里抓取 {d:1234, sub:'...'} 序列，返回 [(镜头号, 时长ms, 字幕)]。"""
    src = open(HTML, encoding="utf-8").read()
    body = src.split("const SCRIPT = [", 1)[1].split("\n];", 1)[0]
    shots = []
    idx = 0
    for m in re.finditer(r"\{d:(\d+),\s*sub:'([^']*)'", body):
        idx += 1
        shots.append({"n": idx, "d": int(m.group(1)), "sub": m.group(2)})
    return shots


SEM = asyncio.Semaphore(3)          # 并发太高会被服务端拒绝


async def synth(text, voice, out):
    """edge-tts 默认语速合成（不传 rate）。"""
    async with SEM:
        for attempt in range(4):
            try:
                await edge_tts.Communicate(text, voice).save(out)   # 无 rate 参数
                if os.path.getsize(out) > 2000:
                    return True
                raise RuntimeError("输出过小")
            except Exception as e:
                if attempt == 3:
                    print(f"  ✗ 失败 {os.path.basename(out)}: {type(e).__name__}: {e}")
                    return False
                print(f"  重试 {os.path.basename(out)} ({attempt + 1}/3): {type(e).__name__}")
                await asyncio.sleep(1.5 * (attempt + 1))
    return False


async def main():
    force = "--force" in sys.argv
    shots = load_script()
    voiced = [s for s in shots if s["sub"]]
    print(f"解析到 {len(shots)} 个镜头（其中有旁白 {len(voiced)} 个），"
          f"总时长 {sum(s['d'] for s in shots)} ms；语速：默认"
          + ("；模式：--force 全量重建" if force else ""))

    tasks = []
    manifest = {}
    for key, voice in VOICES.items():
        d = os.path.join(HERE, "audio", key)
        if force and os.path.isdir(d):
            shutil.rmtree(d)                  # 文案/镜头号变过 → 必须清空，否则旧编号文件串音
        os.makedirs(d, exist_ok=True)
        for s in voiced:
            name = f"{s['n']:02d}.mp3"
            out = os.path.join(d, name)
            if not force and os.path.exists(out) and os.path.getsize(out) > 2000:
                pass                       # 已生成过就复用，重跑只刷新 manifest
            else:
                tasks.append(synth(s["sub"], voice, out))
            manifest.setdefault(key, []).append({
                "n": s["n"], "start": sum(x["d"] for x in shots[:s["n"] - 1]),
                "d": s["d"], "file": name, "text": s["sub"],
            })

    ok = await asyncio.gather(*tasks) if tasks else []
    failed = [t for t, o in zip(tasks, ok) if not o]
    for key in VOICES:
        files = sorted(f for f in os.listdir(os.path.join(HERE, "audio", key)) if f.endswith(".mp3"))
        stale = [f for f in files if int(f[:2]) not in {s["n"] for s in voiced}]
        print(f"  audio/{key}/  {len(manifest[key])} 个旁白 / 目录内 {len(files)} 个 mp3"
              + (f"  ⚠ 无对应镜头的残留：{stale}" if stale else "  ✔ 无残留"))
    if failed:
        print(f"  ⚠ {len(failed)} 段合成失败")

    with open(os.path.join(HERE, "audio", "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"rate": RATE, "voices": VOICES, "shots": manifest}, f,
                  ensure_ascii=False, indent=2)
    print("已写出 audio/manifest.json")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
