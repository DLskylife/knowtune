"""把中文医疗知识图谱（triples/entities/xlsx）清洗成预问诊可用的查询库。

为什么需要这一层：原始数据是"疾病 → 症状"的粗粒度三元组，而追问需要的是
"听到咳嗽之后，下一句该问什么"。这中间的转换必须落成可查询的结构，且必须
先把脏数据挡在外面——原始文件里"驻站医""衣玉品"这类人名/角色混在症状位上，
频次比"腹痛"还高，直接按频次排序，问出来的第一句会是"你有没有驻站医"。

产出 `data/previsit/kg.db`：
  symptom          清洗后的症状词表 + 出现疾病数
  disease_symptom  疾病-症状（清洗后）
  disease_dept     疾病-科室（分诊投票用）
  disease_check    疾病-检查（只用于病历里的"到院检查"提示，不做建议）
  disease_accompany 疾病-并发症
  sameas           同义/别名对（口语 → 标准名）
  disease_desc     xlsx 那份精编描述（62 个 DDXPlus 病种的整句事实）

用法：
  python scripts/build_previsit_kg.py \
    --triples ~/Downloads/triples.txt --entities ~/Downloads/entities_dict.txt \
    --xlsx "~/Downloads/knowledge graph of DDXPlus (1).xlsx"
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 症状词形态门槛。判据只有一条：一个"症状"必须能被患者直接回答"有没有"，
# 所以它得带症状语素。人名（毓卓/闫鹏辉）、角色（驻站医）、科室（呼吸内科）
# 都不带，一条规则全挡掉——这比维护黑名单稳，也比信任 entities_dict 可靠
# （那份字典自己就把"驻站医"标成了 symptom）。
SYMPTOM_MORPHEMES = (
    "痛", "疼", "酸", "胀", "麻", "木", "痒", "肿", "硬", "块", "结",
    "热", "烧", "冷", "寒", "汗", "渴", "乏", "累", "困", "瘦", "胖",
    "咳", "嗽", "痰", "喘", "闷", "气", "呼吸", "悸", "慌", "晕", "眩",
    "吐", "呕", "泻", "秘", "便", "尿", "血", "疹", "斑", "疱", "溃",
    "聋", "鸣", "视", "模糊", "流", "堵", "塞", "嘶", "哑", "抖", "颤",
    "抽", "搐", "昏", "迷", "厥", "眠", "醒", "食", "欲", "恶", "反胃",
    "无", "减", "增", "降", "高", "低", "不", "差", "异常", "障", "难",
    "肿", "硬", "僵", "痉", "挛", "收缩", "脱", "裂", "分泌", "湿",
    # 体征类语素：发绀/黄疸/嗜睡/萎缩/衰竭这类词不带"痛热咳"字面，但同样是
    # 患者能回答"有没有"的观察项，漏掉它们等于把重症信号挡在门外。
    "绀", "疸", "苍", "萎", "缩", "衰", "竭", "痴", "呆", "嗜", "谵",
    "啰", "窒", "瘫", "盲", "睑", "郁", "躁", "幻", "畏", "亢", "紊",
)
# 白名单：语素规则判不出来、但确实是临床观察项的固定说法。
SIGN_TERMS = {
    "休克", "发绀", "紫绀", "黄疸", "腹水", "面色苍白", "皮肤苍白", "疲劳",
    "肌肉萎缩", "鳞屑", "心力衰竭", "肾功能衰竭", "呼吸衰竭", "心动过速",
    "心动过缓", "心律失常", "心脏杂音", "痴呆", "焦虑", "抑郁", "畏光",
    "反应迟钝", "啰音", "胸腔积液", "闭经", "角膜炎", "腹肌紧张", "紫癜",
    "里急后重", "猝死", "腱反射亢进", "腱反射消失", "传导阻滞", "谵妄",
    "胃肠道症状", "静脉曲张", "钙化", "多饮", "反复感染", "皮肤干燥",
    "风团", "上睑下垂", "截瘫", "偏盲", "葡萄膜炎", "腹膜刺激征", "脑积水",
    "角膜混浊", "眼球突出", "生长缓慢", "幻觉", "意识模糊", "定向障碍",
    "失语", "失认", "共济失调", "肌张力增高", "肌张力减低", "病理反射",
    "脑膜刺激征", "杵状指", "蜘蛛痣", "肝掌", "颈静脉怒张", "奇脉",
}
# 明显不是"患者能回答"的实体：服务/角色/机构。
NON_SYMPTOM_PAT = re.compile(r"(医|院|科|站|所|中心|门诊|病房|护士|挂号|医保)")


def clean_symptom(s: str) -> bool:
    s = s.strip()
    if not (2 <= len(s) <= 10):
        return False
    if NON_SYMPTOM_PAT.search(s):
        return False
    if s in SIGN_TERMS:
        return True
    return any(m in s for m in SYMPTOM_MORPHEMES)


def load_triples(path: Path):
    """按 csv 解析。不能按字符串 split(',')——疾病名里带逗号会让整行列错位。"""
    rows: list[tuple[str, str, str]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = next(csv.reader([line]))
            if len(parts) == 3 and all(p.strip() for p in parts):
                rows.append((parts[0].strip(), parts[1].strip(), parts[2].strip()))
    return rows


def load_entities(path: Path) -> dict[str, set[str]]:
    """entities_dict.txt 是 Python dict 字面量，但含非标准 token，ast 会挂。
    它只用来做同义扩展的**补充**，解析失败不影响主流程。"""
    text = path.read_text(encoding="utf-8")
    out: dict[str, set[str]] = {}
    for key in re.findall(r"'([a-zA-Z_]+)':\s*\[", text):
        m = re.search(r"'" + key + r"':\s*\[(.*?)\](?=,\s*'|\s*\})", text, re.S)
        if m:
            out[key] = set(re.findall(r"'([^']*)'", m.group(1)))
    return out


def load_xlsx(path: Path) -> list[tuple[str, str, str]]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0 or not row or not row[0]:
            continue
        subj, rel, obj = (str(row[0]).strip(), str(row[1] or "").strip(), str(row[2] or "").strip())
        if subj and rel and obj:
            rows.append((subj, rel, obj))
    return rows


def build(triples_path: Path, entities_path: Path | None, xlsx_path: Path | None, out: Path) -> dict:
    rows = load_triples(triples_path)
    ent = load_entities(entities_path) if entities_path and entities_path.exists() else {}

    sym_by_dis: dict[str, set[str]] = defaultdict(set)
    dept_by_dis: dict[str, set[str]] = defaultdict(set)
    check_by_dis: dict[str, set[str]] = defaultdict(set)
    acc_by_dis: dict[str, set[str]] = defaultdict(set)
    sameas: set[tuple[str, str]] = set()

    dropped = Counter()
    for h, r, t in rows:
        if r == "disease_has_symptom":
            if clean_symptom(t):
                sym_by_dis[h].add(t)
            else:
                dropped[t] += 1
        elif r == "disease_belong_department":
            dept_by_dis[h].add(t)
        elif r == "disease_need_check":
            check_by_dis[h].add(t)
        elif r == "disease_acompany_disease":
            acc_by_dis[h].add(t)
        elif r == "SameAs":
            sameas.add((h, t))

    # 只留真有症状的疾病：没有症状的疾病在"症状→疾病"打分里只会稀释分母。
    diseases = [d for d, s in sym_by_dis.items() if s]
    sym_freq = Counter()
    for d in diseases:
        sym_freq.update(sym_by_dis[d])

    desc = load_xlsx(xlsx_path) if xlsx_path and xlsx_path.exists() else []

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    con = sqlite3.connect(out)
    con.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE symptom(symptom TEXT PRIMARY KEY, n_disease INTEGER);
        CREATE TABLE disease_symptom(disease TEXT, symptom TEXT, PRIMARY KEY(disease, symptom));
        CREATE TABLE disease_dept(disease TEXT, dept TEXT, PRIMARY KEY(disease, dept));
        CREATE TABLE disease_check(disease TEXT, item TEXT, PRIMARY KEY(disease, item));
        CREATE TABLE disease_accompany(disease TEXT, other TEXT, PRIMARY KEY(disease, other));
        CREATE TABLE sameas(a TEXT, b TEXT, PRIMARY KEY(a, b));
        CREATE TABLE disease_desc(disease TEXT, relation TEXT, text TEXT, PRIMARY KEY(disease, relation, text));
        CREATE INDEX ix_ds_sym ON disease_symptom(symptom);
        CREATE INDEX ix_ds_dis ON disease_symptom(disease);
        CREATE INDEX ix_desc_dis ON disease_desc(disease);
    """)
    con.executemany("INSERT INTO symptom VALUES(?,?)", sym_freq.items())
    con.executemany("INSERT OR IGNORE INTO disease_symptom VALUES(?,?)",
                    [(d, s) for d in diseases for s in sym_by_dis[d]])
    con.executemany("INSERT OR IGNORE INTO disease_dept VALUES(?,?)",
                    [(d, x) for d, xs in dept_by_dis.items() for x in xs])
    con.executemany("INSERT OR IGNORE INTO disease_check VALUES(?,?)",
                    [(d, x) for d, xs in check_by_dis.items() for x in xs])
    con.executemany("INSERT OR IGNORE INTO disease_accompany VALUES(?,?)",
                    [(d, x) for d, xs in acc_by_dis.items() for x in xs])
    con.executemany("INSERT OR IGNORE INTO sameas VALUES(?,?)", sameas)
    con.executemany("INSERT OR IGNORE INTO disease_desc VALUES(?,?,?)", desc)
    con.commit()
    con.close()

    return {
        "triples": len(rows),
        "diseases_with_symptom": len(diseases),
        "symptoms": len(sym_freq),
        "disease_symptom": sum(len(v) for v in sym_by_dis.values()),
        "dept_links": sum(len(v) for v in dept_by_dis.values()),
        "check_links": sum(len(v) for v in check_by_dis.values()),
        "sameas": len(sameas),
        "desc_rows": len(desc),
        "dropped_top": dropped.most_common(15),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    home = Path.home()
    ap.add_argument("--triples", default=str(home / "Downloads/triples.txt"))
    ap.add_argument("--entities", default=str(home / "Downloads/entities_dict.txt"))
    ap.add_argument("--xlsx", default=str(home / "Downloads/knowledge graph of DDXPlus (1).xlsx"))
    ap.add_argument("--out", default=str(ROOT / "data/previsit/kg.db"))
    a = ap.parse_args()

    stats = build(Path(a.triples), Path(a.entities), Path(a.xlsx), Path(a.out))
    print(f"写入 {a.out}")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
