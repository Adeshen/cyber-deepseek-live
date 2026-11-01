"""网易云 LRC → 本项目的歌词缓存(lyrics-cache/<歌名>.lrc.json)。

为什么: whisper 打轴对"AI 翻唱 + 重编曲"经常只有 ~40% 锚点(逐行插值 → 跟唱对不上)。
用户把成品传上网易云后，官方轴是**真时间轴**（实测与音频吻合到 1 秒内），直接用它最准。

用法:
  python tools/lrc_to_cache.py --id 3329948601 --song "audio/我自凡尘过AI版（Rimix王朝1982）.wav"
  python tools/lrc_to_cache.py --lrc x.lrc --song "audio/xxx.wav" --md "F:\\...\\xxx.md"
    --md  给出了用户自己的歌词 md 时, 按"顺序对齐"把**用户的用字/空格**套到官方时间轴上
          (官方轴与 md 都按演唱顺序, 用 difflib 单调对齐; 官方有而 md 没有的行=制作名单等, 用官方原文)
    --drop-first N  丢掉前 N 行(如制作名单)
    --dry           只打印结果不写文件
"""
import argparse
import difflib
import json
import pathlib
import re
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "lyrics-cache"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"


def fetch_lrc(song_id: str) -> str:
    url = f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&kv=1&tv=-1"
    req = urllib.request.Request(url, headers={"Referer": "https://music.163.com/", "User-Agent": UA})
    data = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
    if not (data.get("lrc") or {}).get("lyric"):
        raise SystemExit(f"这首歌没有歌词轴 (nolyric={data.get('nolyric')} pureMusic={data.get('pureMusic')})")
    return data["lrc"]["lyric"]


def parse_lrc(text: str) -> list[tuple[int, str]]:
    out = []
    for ln in text.splitlines():
        m = re.match(r"^\[(\d+):(\d+)(?:[.:](\d+))?\](.*)$", ln.strip())
        if not m:
            continue
        ms = int(m.group(1)) * 60000 + int(m.group(2)) * 1000 + int((m.group(3) or "0").ljust(3, "0")[:3])
        txt = m.group(4).strip()
        if txt:
            out.append((ms, txt))
    return out


def norm(s: str) -> str:
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s or "")


def count_chars(s: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbfA-Za-z0-9]", s or ""))


def load_md_lines(md_path: pathlib.Path) -> list[str]:
    sys.path.insert(0, str(ROOT / "tools"))
    from lyrics_align import parse_lyric_md
    return [l["text"] for l in parse_lyric_md(md_path)["lines"]]


def merge_texts(lrc_lines: list[tuple[int, str]], md_lines: list[str]) -> list[dict]:
    """按演唱顺序单调对齐(官方轴 vs 用户草稿):
      · 字完全一致 → 用 md 的写法(空格更好看)
      · 用字不一致 → **以官方(上架版本)为准**(用户 2026-10-10 拍板:
        "AI 生成时的错字以上架版本为准", 例如 非俗非仙→非鱼非鹤、飞蓬→飞鹰)
      · 官方独有的行(制作名单等) → 官方原文
      · md 独有的行(官方轴无对应行位) → 丢弃(音频没唱, 硬塞时间反而错)
    """
    if not md_lines:
        return [{"start": ms, "text": t} for ms, t in lrc_lines]
    a = [norm(t) for _, t in lrc_lines]
    b = [norm(t) for t in md_lines]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                out.append({"start": lrc_lines[i1 + k][0], "text": md_lines[j1 + k]})
        elif tag == "replace":
            n = min(i2 - i1, j2 - j1)
            for k in range(n):                          # 用字不一致 → 官方为准
                out.append({"start": lrc_lines[i1 + k][0], "text": lrc_lines[i1 + k][1]})
            for k in range(i1 + n, i2):                 # 官方多出来的
                out.append({"start": lrc_lines[k][0], "text": lrc_lines[k][1]})
        elif tag == "delete":
            for k in range(i1, i2):
                out.append({"start": lrc_lines[k][0], "text": lrc_lines[k][1]})
    out.sort(key=lambda x: x["start"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default="", help="网易云歌曲 id")
    ap.add_argument("--lrc", default="", help="本地 .lrc 文件")
    ap.add_argument("--song", required=True, help="音频文件名或路径(用于命名缓存)")
    ap.add_argument("--md", default="", help="可选: 用户自己的歌词 md(用其用字)")
    ap.add_argument("--duration-ms", type=int, default=0)
    ap.add_argument("--drop-first", type=int, default=0, help="丢掉前 N 行(制作名单)")
    ap.add_argument("--default-tail-ms", type=int, default=6000, help="最后一行的保持时长")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    raw = pathlib.Path(args.lrc).read_text(encoding="utf-8-sig") if args.lrc else fetch_lrc(args.id)
    lrc_lines = parse_lrc(raw)
    if args.drop_first:
        lrc_lines = lrc_lines[args.drop_first:]
    if not lrc_lines:
        raise SystemExit("解析出的歌词行为 0")

    md_lines = load_md_lines(pathlib.Path(args.md)) if args.md else []
    merged = merge_texts(lrc_lines, md_lines)

    stem = pathlib.Path(args.song).stem
    dur = args.duration_ms
    cache_path = CACHE_DIR / f"{stem}.lrc.json"
    if not dur and cache_path.exists():
        try:
            dur = int(json.loads(cache_path.read_text(encoding="utf-8")).get("duration_ms") or 0)
        except Exception:
            dur = 0
    if not dur:
        dur = max(merged[-1]["start"] + args.default_tail_ms, 1)

    lines = []
    for k, it in enumerate(merged):
        end = merged[k + 1]["start"] if k + 1 < len(merged) else min(it["start"] + args.default_tail_ms, dur)
        lines.append({"text": it["text"], "start": it["start"],
                      "end": max(end, it["start"] + 800),
                      "quota": count_chars(it["text"]), "anchor": True})
    out = {"song": stem, "lyrics_md": (f"netease:{args.id}" if args.id else pathlib.Path(args.lrc).name),
           "duration_ms": dur, "asr_chars": None,
           "quota_total": sum(l["quota"] for l in lines),
           "method": ("netease-lrc" if args.id else "lrc-file") + ("+md-text" if md_lines else ""),
           "created": time.strftime("%Y-%m-%d %H:%M:%S"), "lines": lines}

    print(f"{stem}: {len(lines)} 行 · {lines[0]['start']/1000:.1f}s → {lines[-1]['start']/1000:.1f}s "
          f"(时长 {dur/1000:.0f}s) · 方法 {out['method']}")
    for l in lines[:6]:
        print(f"   {l['start']/1000:6.1f}s  {l['text']}")
    print("   ...")
    for l in lines[-3:]:
        print(f"   {l['start']/1000:6.1f}s  {l['text']}")
    if args.dry:
        print("(--dry 未写文件)")
        return 0
    CACHE_DIR.mkdir(exist_ok=True)
    cache_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写入 {cache_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
