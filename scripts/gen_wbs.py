"""生成 docs/WBS.html —— 项目的 WBS 图与工作包字典。

数据全部来自仓库实况（ARCHITECTURE.md §14 需求对照表、§9 实施顺序、
各包文件路径、测试与评估基线），不是拍脑袋编的分解。

用法：.venv/Scripts/python.exe scripts/gen_wbs.py
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "WBS.html"

OK, WARN, TODO = "ok", "warn", "todo"
BADGE = {OK: ("已交付", "b-ok"), WARN: ("有限制", "b-warn"), TODO: ("未做", "b-todo")}

# 编号, 名称, 一句话定位, [(编号, 工作包, 交付物/落地位置, 覆盖需求, 状态)]
GROUPS = [
    ("1.0", "架构与工程", "项目基座。文档、配置、数据模型、测试工装都在这一支，其余七支都踩在它上面。", [
        ("1.1", "架构文档与需求锚定", "docs/ARCHITECTURE.md（1857 行）＋ F1–F5 逐条对照表", "全量", OK),
        ("1.2", "环境自检与依赖锁定", "scripts/check_env.py → docs/env_report.json；CPU 版 torch、HF 镜像抓取", "S0", OK),
        ("1.3", "三层配置体系", "default.yaml → models.yaml → 知识库级覆盖（kbconfig.py）", "F1.5", OK),
        ("1.4", "SQLite 数据模型", "schema.sql；代次 generation ＋ active_gen 原子切换", "F1.7", OK),
        ("1.5", "单元测试", "tests/test_core.py，172 条，含密钥、站点角色、模型清单", "—", OK),
        ("1.6", "UI 冒烟工装", "scripts/uismoke.py，34 项断言；切语言 8 锚点位移检测", "—", OK),
        ("1.7", "真机实测工装", "scripts/livecheck.py（CDP 打真接口）＋ scripts/shot.py（截图）", "—", OK),
    ]),
    ("2.0", "导入与治理", "把格式各异的文件变成可检索、可追溯、可回滚的 chunk。垃圾进就是垃圾出。", [
        ("2.1", "多格式解析", "ingest/loader.py：PDF/Word/Excel/PPT/MD/TXT/源码；无文本层显式报错不静默", "F1.1", OK),
        ("2.2", "清洗与分块策略", "ingest/cleaner.py ＋ chunker.py；按文档类型分档 chunk size", "F1.1", OK),
        ("2.3", "嵌入与索引写入", "index/embedder.py ＋ vectors.py ＋ sparse.py；bge-small-zh 512 维", "F2.1 / F2.2", OK),
        ("2.4", "重索引原子切换", "代次隔离，旧索引在新索引 ready 之前继续对外服务", "F1.7", OK),
        ("2.5", "URL / 目录导入", "单条 ＋ 批量 ≤50、SSRF 防护、域名白名单；目录递归 ≤500 文件", "F1.2 / F1.3", WARN),
        ("2.6", "chunk 预览与编辑", "GET/PATCH/split/merge/DELETE；改完只重算该条的分词/哈希/嵌入/FTS", "F1.4", OK),
        ("2.7", "多知识库隔离", "检索 SQL 与向量 mask 双路按 kb_id 过滤，防一路过滤一路漏检", "F1.5", OK),
    ]),
    ("3.0", "混合检索", "系统的质量天花板。上游不给对，下游再会写也只能编。", [
        ("3.1", "稠密向量检索", "bge-small-zh-v1.5，numpy 暴力点积，61 chunk 实测 17–36ms", "F2.1", OK),
        ("3.2", "jieba ＋ FTS5 稀疏路", "预分词后写入；实测 trigram 对 2 字查询命中为 0，故弃用默认分词器", "F2.2", OK),
        ("3.3", "归一化加权融合", "0.50 稠密 ＋ 0.35 稀疏，归一化后加权（RRF 在长尾上拖后腿，已替换）", "F2.3", OK),
        ("3.4", "精排与降级", "CrossEncoder 路径完整（超时降级／失败缓存），模型未下载 → rerank_unavailable", "F2.4", WARN),
        ("3.5", "元数据过滤 ＋ MMR", "source_types／时间范围／doc_ids，两路同一组条件；MMR 默认关闭", "F2.6 / F2.7", OK),
        ("3.6", "检索调试端点", "/api/search/debug：dense／sparse／融合／耗时／降级／拒答判定", "F2.8", OK),
    ]),
    ("4.0", "生成与引用", "让每句话都能被点开验证，或者干脆承认查不到。两件事都不做才是灾难。", [
        ("4.1", "上下文装填预算", "按相关度排序 ＋ token 预算裁剪，记录 context_used／dropped", "F3.1", OK),
        ("4.2", "强制引用与校验", "cite.py：引用编号可点回查；覆盖率与越界编号检测", "F3.2", OK),
        ("4.3", "检索证据分档", "evidence.py：high／medium／low 随 prompt 下发，让模型知道自己站在什么地上", "12.28", OK),
        ("4.4", "拒答双条件与标定", "绝对阈值 0.54 ＋ 分布区分度；误拒 0%，但阈值分不开「相似但无答案」", "F3.3", WARN),
        ("4.5", "SSE 流式四事件", "meta／delta／done／error，先推 citations 骨架再出字", "F3.4", OK),
        ("4.6", "多后端接入", "mock／ollama／vllm／openai／deepseek／bailian；参数不兼容退让、代理直连", "F3.5", OK),
    ]),
    ("5.0", "会话系统", "多轮追问不失焦，关掉窗口还能找回来。", [
        ("5.1", "多会话 CRUD", "GET/PATCH/DELETE /api/sessions ＋ 消息列表；Alt+点击重命名", "F4.1", OK),
        ("5.2", "多轮上下文预算", "最近 5 轮进 prompt，按 token 预算整轮丢弃（不切半句）", "F4.2", OK),
        ("5.3", "消息持久化", "message 表；message_id 随 done 事件下发，供反馈关联", "F4.1", OK),
        ("5.4", "反馈收集", "回答下方「有用／无用」＋ 原因自由文本", "F5.6", OK),
        ("5.5", "历史恢复", "后台走 GET /api/sessions/{id}/messages；前台仍以 localStorage 30 轮为准", "—", WARN),
    ]),
    ("6.0", "评估与观测", "没有基线的优化都是自我安慰。", [
        ("6.1", "评估集 22 条", "evals/cases.jsonl：20 可答 ＋ 2 应拒；用文档+关键词匹配，重分块不失效", "F5.1", OK),
        ("6.2", "Recall@K / MRR", "Recall@5 95.0%／MRR 0.7792／Hit@1 65%／p50 17ms；--save 写基线", "F5.2", OK),
        ("6.3", "拒答阈值标定", "网格扫描 0.20–0.80 × margin 0–0.12，误拒优先；样本不足明确警告", "F5.4", OK),
        ("6.4", "请求日志埋点", "query_log：耗时／token／模型／chunk 列表／降级；/api/logs 可按库与时间筛", "F5.5", OK),
        ("6.5", "忠实度判据", "目前只有引用覆盖率，没有逐句 LLM 判定", "F5.3", TODO),
    ]),
    ("7.0", "管理后台", "给自己看的操作台：知识库、参数、模型、调试面板。", [
        ("7.1", "四视图外壳", "首页／对话／知识库／模型，左栏常驻", "12.8", OK),
        ("7.2", "首页仪表盘", "资产、服务状态、模型状态、最近日志一屏", "12.7", OK),
        ("7.3", "对话视图", "单会话、上下文占用提示、文档预览、检索调试面板", "12.9 / 12.10", OK),
        ("7.4", "知识库视图", "信息／参数／内容／检索／会话 五栏；导入进度、自检、删库代价预演", "12.14／12.21／12.22／12.24", OK),
        ("7.5", "参数面板", "数值控件不用滑杆；库级参数改完自动重排，没有「跟随全局」这一层", "12.3／12.17／12.20", OK),
        ("7.6", "模型页与探测", "后端注册表、按系统给启动命令、探测；/models 通了不等于能调用", "12.6／12.19／12.23", OK),
        ("7.7", "云端密钥内存化", "secrets.py 进程级 dict，可选落盘 0600；model_dump 不含密钥", "12.30", OK),
    ]),
    ("8.0", "维语前台", "给终端用户看的：一个提问框，和一份带来源的答案。", [
        ("8.1", "站点与服务实例", "前台 8800／后台 8000；同进程复用 state，站点流程不与 /api/ask 共用", "12.26", OK),
        ("8.2", "翻译／推理双角色", "roles.py 解析两个模型；/api/info 与 /api/ask 走同一解析路径", "12.27", OK),
        ("8.3", "中维切换零位移", "html[dir] 恒 ltr，骨架用物理 left/right；8 锚点 1px 位移即断言失败", "12.32", OK),
        ("8.4", "视觉体系重做", "左栏＋居中欢迎＋圆角输入框；纸白／深青绿／砂金，纯 CSS 八角星徽记", "12.31", OK),
        ("8.5", "本机会话存档", "localStorage 30 轮，单条答案截 12000 字；写盘失败静默降级", "—", OK),
        ("8.6", "错误态与降级", "用户看一句话，原文进 title；砖橙气泡＋感叹号，不暴露堆栈", "12.32", OK),
    ]),
]

# 编号, 名称, 现状, 所属, 影响
GAPS = [
    ("F2.5", "查询改写", "配置项 retrieval.query_rewrite 已留，未实现", "检索",
     "多轮指代（它／这个）退回给模型自己扛，召回靠运气"),
    ("F4.3", "对话内查询改写", "未实现", "会话", "追问第 3 轮之后容易答非所问"),
    ("F4.4", "历史检索", "未实现", "会话", "只能按会话翻，不能跨会话搜「我以前问过什么」"),
    ("F5.3", "生成忠实度", "只有 cite.verify 的引用覆盖率，无逐句判据", "评估",
     "引用编号对得上 ≠ 那句话被资料支持，存在「引了但没用」的盲区"),
    ("F2.4", "精排模型未下载", "reranker 权重缺失，走 rerank_unavailable 降级", "检索",
     "召回质量停在融合层；补齐后 p95 会涨约 1.3s（CPU）"),
    ("F3.3", "拒答方法边界", "阈值已标定 0.54、误拒 0%，但 r02 top=0.599 高于多数正样本", "生成",
     "分数阈值在语义上分不开「相似但没答案」，需换判据而非调参"),
    ("F1.2", "sitemap 整站抓取", "单条＋批量 ≤50 已通，整站未做", "导入", "整站迁移知识库要手工分批"),
    ("F1.3", ".gitignore 解析", "只有扩展名白名单兜底", "导入", "导代码仓库会带进一堆噪音文件"),
    ("—", "前台历史只存本地", "后台有 /api/sessions/{id}/messages 且后台前端在用；前台没接", "会话",
     "换设备／清缓存后前台历史全丢，服务端其实有数据却用不上"),
]

# 代号, 名称, 时间, 验收, 状态
MILESTONES = [
    ("S0", "环境自检", "10-27", "输出 JSON 报告，FTS5 与中文分词项全绿"),
    ("S1", "竖切片", "10-27", "1 个 .md → 切分 → 嵌入 → 双路检索 → 带 [1] 的答案"),
    ("S2", "检索质量", "10-27", "20 条评估集＋Recall@5 基线，目标 ≥70%（实测 95.0%）"),
    ("S3", "加固", "10-27", "断网／断模型／坏文件三种故障不崩、有可读原因；SSE"),
    ("S4", "会话", "10-27", "多会话、多轮、持久化、反馈"),
    ("S5", "拒答标定", "10-27", "calibrate 脚本＋双条件判定，误拒 ≤10%（实测 0%）"),
    ("S6", "复杂导入", "10-28", "PDF／Office／URL／目录；10 篇抽 9 篇可读"),
    ("D3", "管理后台", "10-27 → 10-29", "四视图外壳＋参数面板＋模型页＋密钥内存化（§12.1–12.30）"),
    ("D4", "维语前台", "10-28 → 10-29", "独立站点 8800；1.7.0 骨架零镜像，34 项 UI 断言全过"),
]

RISKS = [
    ("CPU 精排把 P95 拖到 5s+", "高", "默认 base 档＋候选降到 10＋可关闭；当前直接降级"),
    ("拒答阈值未标定导致误拒", "高", "S5 已标定 0.54，未标定时界面明示"),
    ("评估集随分块策略失效", "中", "expected 用文档级＋关键词柔性匹配，不用 chunk_id"),
    ("PDF 抽取质量差（双栏／表格）", "高", "抽取器可替换＋导入后强制人工预览（2.6）"),
    ("jieba 切碎专有名词", "中", "用户词典＋自动沉淀术语"),
    ("bge 版本漂移导致拒答失准", "中", "模型名带版本号入库，换模型强制重标定"),
]

N_PKG = sum(len(g[3]) for g in GROUPS)
N_OK = sum(1 for g in GROUPS for p in g[3] if p[4] == OK)
N_WARN = sum(1 for g in GROUPS for p in g[3] if p[4] == WARN)
N_TODO = sum(1 for g in GROUPS for p in g[3] if p[4] == TODO)

CSS = """
*{box-sizing:border-box}
:root{
  --paper:#FBFAF7; --card:#FFFFFF; --ink:#23262A; --ink-2:#4E5459; --ink-3:#878D93;
  --brand:#0F6E56; --brand-soft:#E1F5EE; --gold:#B8863B; --gold-soft:#FAEEDA;
  --line:#E6E2D8; --line-2:#F1EEE6; --warn:#B45309; --todo:#8A8F94; --ok:#0F6E56;
}
@media (prefers-color-scheme:dark){
  :root{
    --paper:#141614; --card:#1B1E1B; --ink:#E8EBE7; --ink-2:#B4BAB5; --ink-3:#868C87;
    --brand:#5DCAA5; --brand-soft:#122A22; --gold:#E8C07A; --gold-soft:#2A2115;
    --line:#2C302C; --line-2:#232623; --warn:#F0A868; --todo:#8A908C; --ok:#5DCAA5;
  }
}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--paper); color:var(--ink);
  font:400 15px/1.72 system-ui,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1160px;margin:0 auto;padding:56px 28px 96px}
.narrow{max-width:820px}

/* ---------- 抬头 ---------- */
.eyebrow{
  font-size:12px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--brand);font-weight:500;margin-bottom:14px;
}
h1{
  font-size:34px;line-height:1.28;font-weight:500;margin:0 0 16px;
  letter-spacing:-.01em;
}
.lead{font-size:16px;color:var(--ink-2);max-width:720px;margin:0 0 8px}
.lead b{font-weight:500;color:var(--ink)}
.meta{
  margin-top:22px;padding-top:18px;border-top:1px solid var(--line);
  font-size:12.5px;color:var(--ink-3);display:flex;flex-wrap:wrap;gap:8px 22px;
}
.meta b{font-weight:500;color:var(--ink-2)}

/* ---------- 指标 ---------- */
.metrics{
  display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:12px;margin:34px 0 8px;
}
.m{background:var(--card);border:.5px solid var(--line);border-radius:12px;padding:16px 18px}
.m .k{font-size:12px;color:var(--ink-3);margin-bottom:6px}
.m .v{font-size:26px;font-weight:500;line-height:1.1;letter-spacing:-.02em}
.m .v small{font-size:13px;font-weight:400;color:var(--ink-3);margin-left:4px;letter-spacing:0}

/* ---------- 章节 ---------- */
section{margin-top:64px}
h2{
  font-size:20px;font-weight:500;margin:0 0 6px;
  display:flex;align-items:baseline;gap:10px;
}
h2 .num{
  font-size:12px;font-weight:400;color:var(--brand);
  font-variant-numeric:tabular-nums;letter-spacing:.06em;
}
.sub{color:var(--ink-2);font-size:14px;margin:0 0 24px;max-width:760px}

/* ---------- 图 ---------- */
.fig{background:var(--card);border:.5px solid var(--line);border-radius:16px;padding:26px 22px}
.fig svg{display:block;height:auto}
.legend{
  display:flex;flex-wrap:wrap;align-items:center;gap:6px 20px;
  margin-top:18px;padding-top:16px;border-top:1px solid var(--line-2);
  font-size:12.5px;color:var(--ink-3);
}
.legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.dot-ok{background:var(--ok)}.dot-warn{background:var(--gold)}

/* ---------- 字典 ---------- */
.tools{display:flex;gap:8px;margin-bottom:18px}
.tools button{
  appearance:none;font:inherit;font-size:13px;color:var(--ink-2);
  background:var(--card);border:.5px solid var(--line);border-radius:8px;
  padding:6px 14px;cursor:pointer;transition:border-color .14s,color .14s;
}
.tools button:hover{border-color:var(--brand);color:var(--brand)}
.grp{background:var(--card);border:.5px solid var(--line);border-radius:12px;margin-bottom:12px;overflow:hidden}
.grp>summary{
  list-style:none;cursor:pointer;padding:15px 20px;
  display:flex;align-items:center;gap:14px;
}
.grp>summary::-webkit-details-marker{display:none}
.grp>summary::before{
  content:"";width:6px;height:6px;border-right:1.5px solid var(--ink-3);
  border-bottom:1.5px solid var(--ink-3);transform:rotate(-45deg);
  transition:transform .16s;flex:none;margin-top:-3px;
}
.grp[open]>summary::before{transform:rotate(45deg);margin-top:0}
.grp>summary .code{
  font-variant-numeric:tabular-nums;font-size:13px;color:var(--brand);
  font-weight:500;flex:none;
}
.grp>summary .name{font-size:15.5px;font-weight:500;flex:none}
.grp>summary .cnt{font-size:12.5px;color:var(--ink-3);flex:none;margin-left:auto}
.grp>summary .st{flex:none}
.note{padding:0 20px 14px;font-size:13.5px;color:var(--ink-2)}

table{width:100%;border-collapse:collapse;font-size:13.5px}
thead th{
  text-align:left;font-weight:500;font-size:11.5px;letter-spacing:.06em;
  color:var(--ink-3);text-transform:uppercase;
  padding:0 12px 9px;border-bottom:1px solid var(--line);
}
tbody td{padding:11px 12px;border-bottom:1px solid var(--line-2);vertical-align:top}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:color-mix(in srgb,var(--brand) 4%,transparent)}
td.c{font-variant-numeric:tabular-nums;color:var(--brand);font-weight:500;white-space:nowrap}
td.w{font-weight:500}
td.d{color:var(--ink-2);font-size:13px}
td.r{font-size:12px;color:var(--ink-3);font-variant-numeric:tabular-nums;white-space:nowrap}
td.s{text-align:right;white-space:nowrap}
.tb-wrap{padding:0 20px 18px}

.b-ok,.b-warn,.b-todo{
  display:inline-block;font-size:11.5px;line-height:1.6;padding:2px 9px;
  border-radius:999px;border:.5px solid transparent;white-space:nowrap;
}
.b-ok{color:var(--ok);background:var(--brand-soft);border-color:color-mix(in srgb,var(--ok) 26%,transparent)}
.b-warn{color:var(--warn);background:var(--gold-soft);border-color:color-mix(in srgb,var(--warn) 26%,transparent)}
.b-todo{color:var(--todo);background:color-mix(in srgb,var(--todo) 10%,transparent);border-color:color-mix(in srgb,var(--todo) 24%,transparent)}

/* ---------- 缺口 ---------- */
.gap-tb{background:var(--card);border:.5px solid var(--line);border-radius:12px;overflow:hidden}
.gap-tb td.impact{color:var(--ink-2);font-size:13px}
.tag{
  font-size:11.5px;color:var(--ink-3);background:var(--line-2);
  padding:1px 7px;border-radius:5px;white-space:nowrap;
}

/* ---------- 里程碑 ---------- */
.ms{position:relative;padding-left:26px}
.ms::before{
  content:"";position:absolute;left:5px;top:8px;bottom:8px;
  width:1px;background:var(--line);
}
.ms-item{position:relative;padding:0 0 22px}
.ms-item::before{
  content:"";position:absolute;left:-25px;top:7px;width:9px;height:9px;
  border-radius:50%;background:var(--paper);border:1.5px solid var(--brand);
}
.ms-item:last-child{padding-bottom:0}
.ms-h{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.ms-h .c{font-size:13px;font-weight:500;color:var(--brand);font-variant-numeric:tabular-nums}
.ms-h .n{font-size:14.5px;font-weight:500}
.ms-h .t{font-size:12px;color:var(--ink-3);font-variant-numeric:tabular-nums;margin-left:auto}
.ms-p{font-size:13.5px;color:var(--ink-2);margin:3px 0 0}

.risk td.imp{font-size:12.5px;white-space:nowrap}
.risk td.imp.high{color:var(--warn)}
.risk td.imp.mid{color:var(--ink-3)}

footer{
  margin-top:72px;padding-top:22px;border-top:1px solid var(--line);
  font-size:12.5px;color:var(--ink-3);display:flex;flex-wrap:wrap;gap:6px 20px;
}

@media (max-width:720px){
  .wrap{padding:36px 18px 64px}
  h1{font-size:26px}
  /* 1120 宽的结构图塞进 390 屏 = 12.5px 字缩成 4px，等于没画。
     宁可横向滚动，也不能让图变成装饰。 */
  .fig{padding:16px 12px;overflow-x:auto}
  .fig svg{min-width:1000px}
  .grp>summary{padding:13px 14px;gap:10px;flex-wrap:wrap}
  .grp>summary .cnt{margin-left:0;width:100%}
  .tb-wrap{padding:0 6px 12px}
  thead th:nth-child(4),tbody td.c-req{display:none}
  td.d{font-size:12.5px}
}

@media print{
  body{background:#fff;font-size:11pt}
  .wrap{max-width:none;padding:0}
  .tools{display:none}
  .fig,.grp,.gap-tb{break-inside:avoid;box-shadow:none}
  section{break-before:auto;margin-top:32px}
  details{display:block}
  details>summary{list-style:none}
  a{color:inherit;text-decoration:none}
}
"""


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_svg() -> str:
    bw, bh, gapx = 240, 200, 30
    xs = [40 + i * (bw + gapx) for i in range(4)]
    cx = [x + bw / 2 for x in xs]
    y1, y2, la, lb = 112, 350, 96, 330

    p = [f'<svg viewBox="0 0 1120 570" width="100%" role="img" '
         f'aria-labelledby="wbsT wbsD">',
         '<title id="wbsT">sky_agent WBS 工作分解结构</title>',
         '<desc id="wbsD">根节点为项目，下分八个一级交付物，共四十九个工作包。'
         '绿色为已交付，琥珀色为含遗留项。</desc>',
         '<style>',
         '.bx rect{fill:#E1F5EE;stroke:#0F6E56;stroke-width:.5}',
         '.wx rect{fill:#FAEEDA;stroke:#BA7517;stroke-width:.5}',
         '.bx .gh{fill:#085041}.bx .gs{fill:#0F6E56}',
         '.wx .gh{fill:#633806}.wx .gs{fill:#854F0B}',
         '.gh{font-size:14px;font-weight:500}.gs{font-size:12.5px}',
         '.ed{stroke:#0F6E56;fill:none;stroke-width:.5}',
         '.rt{fill:none;stroke:#0F6E56;stroke-width:.5}',
         '@media(prefers-color-scheme:dark){',
         '.bx rect{fill:#04342C;stroke:#5DCAA5}',
         '.wx rect{fill:#412402;stroke:#EF9F27}',
         '.bx .gh{fill:#9FE1CB}.bx .gs{fill:#5DCAA5}',
         '.wx .gh{fill:#FAC775}.wx .gs{fill:#EF9F27}',
         '.ed,.rt{stroke:#5DCAA5}',
         '}',
         '</style>',
         '<rect class="rt" x="385" y="16" width="360" height="56" rx="10"/>',
         '<text x="560" y="40" text-anchor="middle" '
         'style="font-size:15px;font-weight:500;fill:currentColor">'
         'sky_agent · 本地 RAG 知识库</text>',
         f'<text x="565" y="58" text-anchor="middle" '
         f'style="font-size:12px;fill:currentColor;opacity:.62">'
         f'L1 项目 ｜ 8 个一级交付物 ｜ {N_PKG} 个工作包</text>',
         f'<path class="ed" d="M565 72 V{lb}"/>']

    for ly, y0 in ((la, y1), (lb, y2)):
        p.append(f'<path class="ed" d="M{cx[0]} {ly} H{cx[-1]}"/>')
        for c in cx:
            p.append(f'<path class="ed" d="M{c} {ly} V{y0}"/>')

    for gi, (num, name, _s, pkgs) in enumerate(GROUPS):
        row, col = divmod(gi, 4)
        x, y = xs[col], (y1 if row == 0 else y2)
        cls = "wx" if any(pk[4] != OK for pk in pkgs) else "bx"
        p.append(f'<g class="{cls}"><rect x="{x}" y="{y}" width="{bw}" height="{bh}" rx="10"/>')
        p.append(f'<text class="gh" x="{x + 16}" y="{y + 30}">{num} {name}</text>')
        for pi, pk in enumerate(pkgs):
            p.append(f'<text class="gs" x="{x + 16}" y="{y + 54 + pi * 21}">'
                     f'{pk[0]} {esc(pk[1])}</text>')
        p.append('</g>')

    p.append('</svg>')
    return "\n".join(p)


def badge(st: str) -> str:
    txt, cls = BADGE[st]
    return f'<span class="{cls}">{txt}</span>'


def render() -> str:
    h: list[str] = [
        '<!doctype html><html lang="zh-CN"><head>',
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>sky_agent WBS · 工作分解结构</title>',
        '<meta name="description" content="sky_agent 本地 RAG 知识库的 WBS 图与工作包字典">',
        f'<style>{CSS}</style></head><body><div class="wrap">',
        '<div class="narrow">',
        '<div class="eyebrow">Work Breakdown Structure</div>',
        '<h1>sky_agent 工作分解结构</h1>',
        '<p class="lead">按<b>交付物</b>分解，不按阶段分解。三层：项目 → 八个一级交付物 → '
        f'{N_PKG} 个工作包。每个工作包都能落到仓库里的一个文件或一个端点，'
        '每个都标了它覆盖哪条需求。</p>',
        '<p class="lead">工作包是最底层可估算单元，再往下拆就是在数代码行了，没有管理意义。'
        '未实现的 4 条需求同样挂在树上——不留在灰色地带，是 WBS 的 100% 原则。</p>',
        '<div class="meta">',
        '<span><b>数据来源</b> ARCHITECTURE.md §9／§14、各包文件路径、测试与评估基线</span>',
        '<span><b>统计口径</b> 以仓库当前状态为准，非计划值</span>',
        '</div></div>',
        '<div class="metrics">',
        f'<div class="m"><div class="k">一级交付物</div><div class="v">8</div></div>',
        f'<div class="m"><div class="k">工作包</div><div class="v">{N_PKG}'
        f'<small>{N_WARN + N_TODO} 项有缺口</small></div></div>',
        '<div class="m"><div class="k">需求条目</div><div class="v">32<small>24 / 4 / 4</small></div></div>',
        '<div class="m"><div class="k">测试</div><div class="v">172<small>条全绿</small></div></div>',
        '<div class="m"><div class="k">Recall@5</div><div class="v">95.0<small>%</small></div></div>',
        f'<div class="m"><div class="k">代码规模</div><div class="v">21k<small>行</small></div></div>',
        '</div>',
        '<section><h2><span class="num">01</span>WBS 图</h2>',
        '<p class="sub">八个分支按「底座 → 内容 → 检索 → 生成 → 会话 → 评估 → 后台 → 前台」'
        '的依赖顺序排列，前一支是后一支的输入。</p>',
        '<div class="fig">', build_svg(),
        '<div class="legend">',
        '<span><i class="dot-ok"></i>已交付：工作包全部到位</span>',
        '<span><i class="dot-warn"></i>有遗留：至少一个工作包未完成或受限</span>',
        f'<span style="margin-left:auto">{N_OK} 已交付 · {N_WARN} 有限制 · {N_TODO} 未做</span>',
        '</div></div></section>',
    ]

    # ---- 字典
    h.append('<section><h2><span class="num">02</span>WBS 字典</h2>')
    h.append('<p class="sub">每个工作包的交付物写在「落地位置」列，'
             '点开分组可看到覆盖的需求编号。这些路径就是审查时的入口。</p>')
    h.append('<div class="tools"><button onclick="setAll(true)">全部展开</button>'
             '<button onclick="setAll(false)">全部折叠</button></div>')

    for num, name, note, pkgs in GROUPS:
        n_ok = sum(1 for p in pkgs if p[4] == OK)
        if n_ok == len(pkgs):
            st = badge(OK)
        elif n_ok == 0:
            st = badge(TODO)
        else:
            st = badge(WARN)
        h.append(f'<details class="grp" open><summary><span class="code">{num}</span>'
                 f'<span class="name">{name}</span>'
                 f'<span class="cnt">{len(pkgs)} 个工作包 · {n_ok} 完成</span>'
                 f'<span class="st">{st}</span></summary>')
        h.append(f'<p class="note">{note}</p>')
        h.append('<div class="tb-wrap"><table><thead><tr>'
                 '<th style="width:9%">编号</th><th style="width:23%">工作包</th>'
                 '<th>交付物 / 落地位置</th>'
                 '<th style="width:13%" class="c-req">覆盖需求</th>'
                 '<th style="width:11%;text-align:right">状态</th>'
                 '</tr></thead><tbody>')
        for code, pname, deliv, req, stt in pkgs:
            h.append(f'<tr><td class="c">{code}</td><td class="w">{esc(pname)}</td>'
                     f'<td class="d">{esc(deliv)}</td>'
                     f'<td class="r c-req">{esc(req)}</td>'
                     f'<td class="s">{badge(stt)}</td></tr>')
        h.append('</tbody></table></div></details>')

    # ---- 缺口
    h.append('<section><h2><span class="num">03</span>范围缺口</h2>')
    h.append('<p class="sub">九项明确知道没做完或做不透的地方。'
             '列出来不是自我批评，是让下一个决定有依据——哪些能忍，哪些必须补。</p>')
    h.append('<div class="gap-tb"><div class="tb-wrap" style="padding-top:18px">'
             '<table><thead><tr><th style="width:7%">编号</th>'
             '<th style="width:17%">缺口</th><th style="width:30%">现状</th>'
             '<th style="width:9%">所属</th><th style="width:37%">影响</th>'
             '</tr></thead><tbody>')
    for code, name, state, area, impact in GAPS:
        h.append(f'<tr><td class="c">{code}</td><td class="w">{esc(name)}</td>'
                 f'<td class="d">{esc(state)}</td>'
                 f'<td><span class="tag">{area}</span></td>'
                 f'<td class="impact">{esc(impact)}</td></tr>')
    h.append('</tbody></table></div></div></section>')

    # ---- 里程碑
    h.append('<section><h2><span class="num">04</span>里程碑</h2>')
    h.append('<p class="sub">S0–S6 是竖切片阶段，D3／D4 是两个交付界面。'
             '日期来自项目记录，不是排期——这座塔是三天垒起来的。</p>')
    h.append('<div class="ms">')
    for code, name, t, accept in MILESTONES:
        h.append(f'<div class="ms-item"><div class="ms-h"><span class="c">{code}</span>'
                 f'<span class="n">{esc(name)}</span><span class="t">{esc(t)}</span></div>'
                 f'<p class="ms-p">{esc(accept)}</p></div>')
    h.append('</div></section>')

    # ---- 风险
    h.append('<section><h2><span class="num">05</span>风险清单</h2>')
    h.append('<p class="sub">只留会真的咬人的。已经发生过的标为高——'
             '概率不再是估计，是记录。</p>')
    h.append('<div class="gap-tb risk"><div class="tb-wrap" style="padding-top:18px">'
             '<table><thead><tr><th>风险</th><th style="width:10%">概率</th>'
             '<th>缓解</th></tr></thead><tbody>')
    for risk, prob, mit in RISKS:
        cls = "high" if prob == "高" else "mid"
        h.append(f'<tr><td class="w">{esc(risk)}</td>'
                 f'<td class="imp {cls}">{prob}</td>'
                 f'<td class="d">{esc(mit)}</td></tr>')
    h.append('</tbody></table></div></div></section>')

    h.append('<footer><span>由 scripts/gen_wbs.py 生成</span>'
             '<span>数据随仓库更新，重跑即可刷新</span>'
             '<span>口径：仓库当前状态</span></footer>')
    h.append('</div>'
             '<script>function setAll(v){document.querySelectorAll("details.grp")'
             '.forEach(function(d){d.open=v})}</script>'
             '</div></body></html>')
    return "\n".join(h)


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(), encoding="utf-8")
    print(f"写出 {OUT.relative_to(ROOT)}  "
          f"（{N_PKG} 工作包：{N_OK} 已交付 / {N_WARN} 有限制 / {N_TODO} 未做）")
