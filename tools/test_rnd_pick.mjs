// 🎲 拟人化「随机表演」加权抽取的离线验证(不连网/不花钱)。
// 做法: 从 stage/index.html 里把 **真实的 CMD_GROUPS 顺序** 和 **RND 对象源码** 抽出来,
//       用最小桩(stub)在本进程里跑 2 万次抽样, 检查"越靠前被选中概率越高"是否真的成立。
// 跑法(项目根目录): node tools/test_rnd_pick.mjs
import { readFileSync } from "fs";

const PAGE = "stage/index.html";
const html = readFileSync(PAGE, "utf8");

// ⚠️ 页面源码是 CRLF 换行 → 正则必须用 \r?\n 容错, 否则锚点匹配不到(踩过一次)
const groupsSrc = html.match(/const CMD_GROUPS = (\[[\s\S]*?\r?\n\])\s*;/);
const rndSrc = html.match(/const RND = (\{[\s\S]*?\r?\n\})\s*;\s*\r?\nRND\.load\(\);/);
if (!groupsSrc || !rndSrc) {
  console.error("✗ 没能从页面里抽出 CMD_GROUPS / RND —— 源码结构变了, 请更新本测试的匹配式");
  process.exit(1);
}

// 最小桩: RND 的 pick/weight 只用 pool/weight/last, 其余(storage/dom)给空实现即可
const localStorageStub = { getItem: () => null, setItem: () => {} };
const CMD_GROUPS = eval(groupsSrc[1]);                       // eslint-disable-line no-eval
const RND = eval("(" + rndSrc[1] + ")");                     // eslint-disable-line no-eval

const grp = CMD_GROUPS.find(([t]) => String(t).indexOf("🎭") === 0);
if (!grp) { console.error("✗ 页面里没有「🎭 拟人化动作」组"); process.exit(1); }
RND.pool = grp[1].map(c => (Array.isArray(c) ? c[1] : c));

let fail = 0;
const chk = (name, cond, detail = "") => {
  if (cond) console.log(`  [ok] ${name}`);
  else { fail++; console.log(`  [X]  ${name}  ${detail}`); }
};

console.log(`== 池子 = 「${grp[0]}」 共 ${RND.pool.length} 条(顺序即优先级) ==`);
const N = 20000;

for (const bias of ["strong", "soft", "flat"]) {
  RND.bias = bias; RND.last = "";
  const cnt = new Map();
  for (let i = 0; i < N; i++) { const c = RND.pick(); cnt.set(c, (cnt.get(c) || 0) + 1); }
  const rows = RND.pool.map((c, i) => ({ rank: i + 1, cmd: c, n: cnt.get(c) || 0 }))
                       .sort((a, b) => a.rank - b.rank);
  const top = [...rows].sort((a, b) => b.n - a.n).slice(0, 6);
  const sum = rows.reduce((s, r) => s + r.n, 0);
  console.log(`\n-- bias=${bias} (${RND.biasName()}) · 抽样 ${N} 次 --`);
  console.log("  最常出的 6 条: " + top.map(r => `#${r.rank} ${r.cmd} ${(r.n / sum * 100).toFixed(1)}%`).join(" | "));
  const first = rows[0].n / sum, lastR = rows[rows.length - 1].n / sum;
  if (bias === "strong" || bias === "soft") {
    chk(`${bias}: 第 1 名显著高于最后一名`, first > lastR * 2,
        `第1名 ${(first * 100).toFixed(1)}% vs 最后 ${(lastR * 100).toFixed(1)}%`);
    // 单调性(强档按排名应单调不增; 允许抽样噪声 → 只看前 1/3 的平均是否高于后 1/3)
    const k = Math.floor(rows.length / 3);
    const frontAvg = rows.slice(0, k).reduce((s, r) => s + r.n, 0) / k;
    const backAvg = rows.slice(-k).reduce((s, r) => s + r.n, 0) / k;
    chk(`${bias}: 前 1/3 平均次数 > 后 1/3`, frontAvg > backAvg * 1.5,
        `${frontAvg.toFixed(0)} vs ${backAvg.toFixed(0)}`);
    const topIsFirst = top[0].rank === 1;
    chk(`${bias}: 抽中最多的就是第 1 条`, topIsFirst, `最多的是 #${top[0].rank} ${top[0].cmd}`);
  } else {
    const exp = sum / rows.length;
    const dev = Math.max(...rows.map(r => Math.abs(r.n - exp) / exp));
    // 63 项 × 2 万次: 单项相对标准差 ≈5.6%, 极值偏差 15~20% 属正常抽样噪声 → 阈值给 30%
    chk("flat: 每条概率接近均等(在抽样噪声内, 偏差 <30%)", dev < 0.30, `最大偏差 ${(dev * 100).toFixed(1)}%`);
  }
}

// 不连着做同一条: 连续两次抽到相同动作的比例应远低于"独立均匀"的期望
RND.bias = "strong"; RND.last = "";
let same = 0, prev = "";
for (let i = 0; i < N; i++) { const c = RND.pick(); if (c === prev) same++; prev = c; }
chk("绝不连着做同一动作(连续重复率 = 0)", same === 0, `${(same / N * 100).toFixed(2)}%`);

console.log(`\n结果: ${fail ? fail + " 项失败" : "全部通过"}`);
process.exit(fail ? 1 : 0);
