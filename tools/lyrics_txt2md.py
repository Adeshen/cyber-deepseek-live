"""纯歌词文本 → lyrics_align.py 认的歌词 md（逐行对照表 + 成曲顺序）。

为什么需要: `parse_lyric_md()` 只认一种结构——
  **成曲顺序**：`S1,S2,...`  +  `### S1 [段落名]` 下面的 `| 字数 | 原词 | **新词** |` 表。
手写新歌时只想贴一段歌词，所以这里把"贴进来的歌词"自动转成那个结构。

规则:
  · `[Tag]` 行            → 段落标记(只用于分节, **不进歌词、不显示**)
  · 其他非空行            → 歌词行(按出现顺序)
  · 字数定额              → 该行「汉字 + 字母数字」的字符数(与 ASR 字流口径一致, 标点不算)
  · 成曲顺序              → S1,S2,…(每个段落一次, 即"就按我写的顺序唱")

用法:
  python tools/lyrics_txt2md.py "<歌词.txt>" "<输出.md>"
  python tools/lyrics_txt2md.py --dir "<目录>"      # 批量: 目录下每个 .txt 生成同名 .md
"""
import pathlib
import re
import sys

CJK = r"\u4e00-\u9fff\u3400-\u4dbf"


def count_chars(line: str) -> int:
    """字数定额: 只数汉字/字母/数字(ASR 输出没有标点, 口径要一致)。"""
    return len(re.findall(rf"[{CJK}A-Za-z0-9]", line))


def convert(txt_path: pathlib.Path, md_path: pathlib.Path) -> tuple[int, int]:
    text = txt_path.read_text(encoding="utf-8-sig", errors="replace")
    sections: list[tuple[str, str, list[str]]] = []      # (id, tag, lines)
    cur_tag, cur_lines = "正文", []
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue
        m = re.match(r"^[\[【](.+?)[\]】]\s*$", line)
        if m:                                            # 段落标记 → 只分节
            if cur_lines:
                sections.append((f"S{len(sections)+1}", cur_tag, cur_lines))
            cur_tag, cur_lines = m.group(1).strip(), []
            continue
        cur_lines.append(line)
    if cur_lines:
        sections.append((f"S{len(sections)+1}", cur_tag, cur_lines))
    if not sections:
        raise SystemExit(f"没解析出歌词行: {txt_path}")

    out = [f"# {txt_path.stem}", "",
           f"**成曲顺序**：`{','.join(s[0] for s in sections)}`", ""]
    for sid, tag, lines in sections:
        out.append(f"### {sid} [{tag}]")
        out.append("| 字数 | 原词 | **新词** |")
        out.append("|---|---|---|")
        for ln in lines:
            out.append(f"| {count_chars(ln)} | {ln} | **{ln}** |")
        out.append("")
    md_path.write_text("\n".join(out), encoding="utf-8")
    total = sum(count_chars(l) for _, _, ls in sections for l in ls)
    n_lines = sum(len(ls) for _, _, ls in sections)
    return n_lines, total


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--dir" in sys.argv:
        d = pathlib.Path(args[0] if args else ".")
        for t in sorted(d.glob("*.txt")):
            n, q = convert(t, d / (t.stem + ".md"))
            print(f"  {t.name} → {t.stem}.md  ({n} 行 / 配额 {q} 字)")
        return 0
    if len(args) < 2:
        print(__doc__)
        return 2
    n, q = convert(pathlib.Path(args[0]), pathlib.Path(args[1]))
    print(f"已生成 {args[1]}: {n} 行 / 配额 {q} 字")
    return 0


if __name__ == "__main__":
    sys.exit(main())
