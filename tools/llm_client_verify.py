# 验证桥的 LLMClient 新链路(deepseek-flash + thinking=disabled)
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import llm as llmmod  # noqa: E402
from config import load  # noqa: E402


async def main():
    c = load()["llm"]
    cli = llmmod.LLMClient(lambda: c)
    prompt = ("【直播间弹幕·抖音】观众「星河入梦」说：小雅小雅，来一首你的招牌歌呗！\n"
              "（用主播的口吻自然接话：可以叫他的名字，一到两句就够，不要复述他说的话）")
    t0 = time.perf_counter()
    out = []
    async for d in cli.stream(prompt):
        out.append(d)
    print(f"model={c['model']} thinking={c.get('thinking')} 耗时 {time.perf_counter()-t0:.2f}s")
    print("回复:", "".join(out))


asyncio.run(main())
