"""MedRAG（DDXPlus 62 病种精编）数据源：英文原文 + 人工校验的中文对照表。

**为什么不再读 kg.db 的 disease_desc**：那份表是整份 xlsx 的机器翻译产物，
逐句和英文原文对拍后有三类问题——

1. 病名错到查不到：`urti` / `psvt` / `sle` / `结核` / `过敏反应` /
   `可能的nstemi_stemi` 等 12 个名字在 OpenCMKG（`disease_symptom` /
   `disease_dept`）里一条记录都没有，等于这些病既取不到科室、也取不到症状，
   而它们恰好是预问诊最要紧的高危病（心梗、室上速、结核、过敏性休克）。
2. 句子漏译/错译：`psvt` 的"Often no underlying heart disease, may occur in
   healthy individuals"被译成"健康个体也可能发生心脏病"（凭空多出一个"心脏病"，
   语义反转）；百日咳的 whoop（鸡鸣样吼声）成了"呼哧"；心包炎的 sharp pain
   成了"剧烈疼痛"（丢了"锐痛"）。逐句对拍共查出 8 处。
3. 行序被打乱、丢了 8 行：英文 1737 行、中文 1729 行，且同一病内关系块的顺序
   不一致，无法逐行对齐，任何"按行号取译文"的做法都会串行。

所以现在的口径是：**英文原文是唯一事实来源，中文是仓库里的人工校验译文**
（`data/previsit/medrag_zh.json`）。缺译文直接报错，不静默回退到机翻——
宁可灌不进去，也不把"健康人也可能发生心脏病"这种句子喂给追问链路。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import OrderedDict, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "previsit"
ZH_JSON = DATA / "medrag_zh.json"
KG_DB = DATA / "kg.db"

# 英文原文优先用仓库内副本（45KB，锁版本），其次用户的下载目录。
XLSX_CANDIDATES = [
    DATA / "ddxplus_en.xlsx",
    Path.home() / "Downloads" / "knowledge graph of DDXPlus.xlsx",
]

# 关系 → (小节标题, 英文 key)。key 写进 heading，引擎按它分派用途：
# symptoms 用来追问、exposure/history 补既往史、lifestyle 只做背景。
# 刻意不含 has_therapy：给治疗建议是踩线，预问诊不碰。
REL_SECTIONS = [
    ("has_symptomatology", "症状确认", "symptoms"),
    ("has_exposure", "危险因素", "exposure"),
    ("has_anamnesis", "病史线索", "history"),
    ("has_lifestyle", "生活背景", "lifestyle"),
]

# 62 个 subject 里混着 9 个上级类目（呼吸系统、传染病、创伤和损伤相关疾病……），
# 它们不是病，给不出"该确认什么"，留在库里只会让召回返回一个等于没说的类目名。
CATEGORIES = {
    "respiratory_system", "cardiovascular_system", "gastrointestinal_system",
    "neurological_and_muscular_system", "hematological_disorders",
    "infectious_diseases", "autoimmune_and_immunological_diseases",
    "trauma_and_injury_related_conditions", "psychiatric_and_stress_related_disorders",
}

# 英文 subject → 中文标准病名。**不是直译，是查过 OpenCMKG 的**：优先选库里
# 有 `disease_symptom` / `disease_dept` 记录的标准名，这样科室和症状都能对上；
# 库里没有的（哮吼、鲭鱼中毒、急性HIV感染等）保留可读的临床名，科室走下面的
# SYSTEM_OF 兜底。
NAMES = {
    # 呼吸
    "acute_copd_exacerbation_infection": "慢性阻塞性肺疾病急性加重",
    "bronchiectasis": "支气管扩张",
    "bronchiolitis": "细支气管炎",
    "bronchitis": "支气管炎",
    "bronchospasm_acute_asthma_exacerbation": "支气管哮喘",
    "pulmonary_embolism": "肺栓塞",
    "pulmonary_neoplasm": "肺癌",
    "spontaneous_pneumothorax": "自发性气胸",
    "pneumonia": "肺炎",
    "tuberculosis": "肺结核",
    "influenza": "流行性感冒",
    "whooping_cough": "百日咳",
    "croup": "哮吼",
    # 心血管
    "atrial_fibrillation": "心房颤动",
    "myocarditis": "心肌炎",
    "pericarditis": "心包炎",
    "psvt": "阵发性室上性心动过速",
    "possible_nstemi_stemi": "急性冠脉综合征",
    "stable_angina": "稳定型心绞痛",
    "unstable_angina": "不稳定型心绞痛",
    "acute_pulmonary_edema": "急性肺水肿",
    # 神经与精神
    "myasthenia_gravis": "重症肌无力",
    "guillain_barre_syndrome": "吉兰-巴雷综合征",
    "cluster_headache": "丛集性头痛",
    "acute_dystonic_reactions": "急性肌张力障碍反应",
    "panic_attack": "惊恐发作",
    # 皮肤免疫与血液
    "sle": "系统性红斑狼疮",
    "sarcoidosis": "结节病",
    "anaphylaxis": "过敏性休克",
    "anemia": "贫血",
    "localized_edema": "局限性水肿",
    # 消化
    "gerd": "胃食管反流病",
    "boerhaave_syndrome": "食管破裂",
    "pancreatic_neoplasm": "胰腺癌",
    "inguinal_hernia": "腹股沟疝",
    "scombroid_food_poisoning": "鲭鱼中毒",
    "spontaneous_rib_fracture": "肋骨骨折",
    # 耳鼻喉与感染
    "urti": "急性上呼吸道感染",
    "viral_pharyngitis": "病毒性咽炎",
    "acute_laryngitis": "急性喉炎",
    "laryngospasm": "喉痉挛",
    "larygospasm": "喉痉挛",
    "epiglottitis": "会厌炎",
    "acute_otitis_media": "急性中耳炎",
    "acute_rhinosinusitis": "急性鼻窦炎",
    "chronic_rhinosinusitis": "慢性鼻窦炎",
    "allergic_sinusitis": "过敏性鼻炎",
    "ebola": "埃博拉病毒病",
    "chagas": "恰加斯病",
    "hiv_initial_infection": "急性HIV感染",
    # 原文里同一病的两种下划线写法（机翻/转义残留）
    "acute_copd_exacerbation__infection": "慢性阻塞性肺疾病急性加重",
    "bronchospasm__acute_asthma_exacerbation": "支气管哮喘",
    "possible_nstemi__stemi": "急性冠脉综合征",
    "boerhaave": "食管破裂",
    "guillainbarré_syndrome": "吉兰-巴雷综合征",
}

# 六个系统 = 六篇文档。划分依据是"预问诊里谁跟谁一起问"，不是科室树：
# 说胸痛，心血管和呼吸的追问几乎同一批（气短、出汗、放射痛）。
SYSTEMS = ["呼吸系统", "心血管系统", "消化系统",
           "神经与精神", "皮肤免疫与血液", "耳鼻喉与感染"]

# 科室 → 系统。科室来自 kg.db 的 disease_dept（OpenCMKG）。
DEPT_SYSTEM = {
    "呼吸内科": "呼吸系统", "心胸外科": "呼吸系统", "肿瘤内科": "呼吸系统",
    "心内科": "心血管系统", "心血管内科": "心血管系统",
    "消化内科": "消化系统", "普外科": "消化系统", "肛肠科": "消化系统",
    "肝胆外科": "消化系统", "肝病": "消化系统", "肝胆胰外科": "消化系统",
    "神经内科": "神经与精神", "神经外科": "神经与精神",
    "精神科": "神经与精神", "心理科": "神经与精神",
    "皮肤科": "皮肤免疫与血液", "风湿免疫科": "皮肤免疫与血液",
    "血液科": "皮肤免疫与血液", "性病科": "皮肤免疫与血液",
    "耳鼻喉科": "耳鼻喉与感染", "感染科": "耳鼻喉与感染",
    "传染科": "耳鼻喉与感染", "急诊科": "耳鼻喉与感染",
    "小儿内科": "呼吸系统", "儿科综合": "呼吸系统",
}

# 库里查不到科室的病，逐个指定（写死不可耻，一条一行，比猜准）。
SYSTEM_OF = {
    "慢性阻塞性肺疾病急性加重": "呼吸系统",
    "细支气管炎": "呼吸系统", "哮吼": "呼吸系统", "肺癌": "呼吸系统",
    "支气管哮喘": "呼吸系统", "肺结核": "呼吸系统", "流行性感冒": "呼吸系统",
    "急性冠脉综合征": "心血管系统", "阵发性室上性心动过速": "心血管系统",
    "急性肺水肿": "心血管系统",
    "食管破裂": "消化系统", "胰腺癌": "消化系统", "鲭鱼中毒": "消化系统",
    "丛集性头痛": "神经与精神", "急性肌张力障碍反应": "神经与精神",
    "惊恐发作": "神经与精神",
    "系统性红斑狼疮": "皮肤免疫与血液", "过敏性休克": "皮肤免疫与血液",
    "局限性水肿": "皮肤免疫与血液",
    "急性鼻窦炎": "耳鼻喉与感染", "慢性鼻窦炎": "耳鼻喉与感染",
    "急性HIV感染": "耳鼻喉与感染", "埃博拉病毒病": "耳鼻喉与感染",
    "恰加斯病": "耳鼻喉与感染", "急性上呼吸道感染": "耳鼻喉与感染",
    # 肋骨骨折挂在呼吸系统：预问诊里它和胸痛/气短同一批鉴别（胸膜炎、气胸、
    # 肺栓塞），归"骨外科"反而问不到点上。
    "肋骨骨折": "呼吸系统",
}

# 证据编码问答的残渣：DDXPlus 原始证据被拼成"问题？：答案"，机翻后成了
# "描述你的疼痛：灼痛"、"疼痛程度如何？：5"。它们不是症状描述，灌进库会污染
# 召回（患者说"胸口疼"，召回里躺着"疼痛程度如何？：5"，追问就变成审问）。
_RESIDUE_HEAD = re.compile(r"^(描述|形容|请描述|你感觉|您感觉|您就诊|您是否|"
                           r"你的疼痛|您的疼痛|疼痛|有没有|是否)")
_SCORE_TAIL = re.compile(r"[：:]\s*\d+(\.\d+)?\s*$")


def is_residue(text: str) -> bool:
    """这条记录是不是证据编码问答的残渣。True = 丢弃。"""
    t = str(text or "").strip()
    if not t or len(t) < 4:
        return True
    if "不适用" in t or t in ("TRUE", "FALSE") or t.isdigit():
        return True
    # 全角/半角冒号后跟答案，是问答对拼接的特征；纯描述句不带冒号。
    if "？：" in t or "?:" in t or "::" in t or "： " in t or ": " in t:
        return True
    if _SCORE_TAIL.search(t):
        return True
    if _RESIDUE_HEAD.match(t):
        return True
    if ("?" in t or "？" in t) and len(t) < 40:   # 短问句是提问，不是描述
        return True
    return False


def clean(text: str) -> str:
    """清洗一条描述值。空串 = 丢弃。"""
    t = str(text or "").strip().rstrip("；;")
    if is_residue(t):
        return ""
    return t.rstrip("。，, ")


def xlsx_path() -> Path:
    for p in XLSX_CANDIDATES:
        if p.exists():
            return p
    raise FileNotFoundError(
        "找不到 DDXPlus 英文原文；把它放到 data/previsit/ddxplus_en.xlsx")


def load_en(path: Path | None = None) -> OrderedDict[str, dict[str, list[str]]]:
    """读英文原文：subject → relation → [原句]。

    只保留四个关系（症状/暴露/回忆/生活方式），治疗与免疫学不进预问诊。
    """
    import openpyxl

    p = path or xlsx_path()
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    out: OrderedDict[str, dict[str, list[str]]] = OrderedDict()
    keep = {k for k, _, _ in REL_SECTIONS}
    for row in ws.iter_rows(min_row=2, values_only=True):
        subj, rel, obj = (str(x).strip() if x is not None else "" for x in row[:3])
        if not (subj and rel and obj) or rel not in keep:
            continue
        out.setdefault(subj, defaultdict(list))[rel].append(obj)
    return out


def load_zh(path: Path | None = None) -> dict:
    p = path or ZH_JSON
    if not p.exists():
        raise FileNotFoundError(f"{p} 不存在，先跑 scripts/build_medrag_zh.py")
    return json.loads(p.read_text(encoding="utf-8"))


def dept_map(db: Path | None = None) -> dict[str, str]:
    """病名 → 科室。库不在就返回空表（科室行就省略，不影响追问）。"""
    p = db or KG_DB
    if not p.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        rows = con.execute("SELECT disease, dept FROM disease_dept").fetchall()
        con.close()
    except sqlite3.Error:
        return {}
    out: dict[str, str] = {}
    for d, dept in rows:
        out.setdefault(str(d), str(dept))
    return out


def system_of(zh_name: str, depts: dict[str, str]) -> str:
    dept = depts.get(zh_name, "")
    if dept and dept in DEPT_SYSTEM:
        return DEPT_SYSTEM[dept]
    return SYSTEM_OF.get(zh_name, "")


def build_docs(db: Path | None = None) -> list[tuple[str, str]]:
    """按系统归并成 6 篇文档，每篇内部 `## 病名` 一块。

    一块 = 一个病的理由：检索命中即"这个系统里的哪个病"，症状归属不会串到
    隔壁病头上（早期按字数切时，一块横跨 3~5 个病，heading 只标第一个）。
    """
    en = load_en()
    zh = load_zh()
    names: dict[str, str] = zh["names"]
    texts: dict[str, str] = zh["text"]
    depts = dept_map(db)

    by_system: dict[str, list[str]] = defaultdict(list)
    used: dict[str, dict[str, list[str]]] = {}
    unnamed: list[str] = []
    no_system: list[str] = []
    no_symptom: list[str] = []
    for subj, rels in en.items():
        if subj in CATEGORIES:
            continue
        zh_name = names.get(subj) or NAMES.get(subj, "")
        if not zh_name:
            # 漏一个名字就等于一个病从库里消失，不能静默跳过
            unnamed.append(subj)
            continue
        bucket = used.setdefault(zh_name, defaultdict(list))
        for rel, items in rels.items():
            for it in items:
                c = clean(it)
                if c and c not in bucket[rel]:
                    bucket[rel].append(c)
        sysname = system_of(zh_name, depts)
        if not sysname:
            no_system.append(zh_name)
            continue
        if zh_name not in by_system[sysname]:
            by_system[sysname].append(zh_name)

    if unnamed:
        raise KeyError(f"这些 subject 没有中文名（scripts/medrag_source.py 的 NAMES）：{unnamed}")
    if no_system:
        raise KeyError(f"这些病归不到系统（补 SYSTEM_OF）：{no_system}")

    docs: list[tuple[str, str]] = []
    missing: list[str] = []
    for system in SYSTEMS:
        members = sorted(by_system.get(system) or [], key=lambda n: (n not in SYSTEM_OF, n))
        if not members:
            continue
        lines = [f"# MedRAG · {system}", ""]
        kept = 0
        for zh_name in members:
            rels = used[zh_name]
            syms = [texts[s] for s in rels.get("has_symptomatology", []) if s in texts]
            for s in rels.get("has_symptomatology", []):
                if s not in texts:
                    missing.append(s)
            if not syms:
                # 原文这病只有证据编码问答、没有症状描述（局限性水肿就是这个），
                # 进了库也只是一条等于没说的块，命中还占位置。跳过并记账。
                no_symptom.append(zh_name)
                continue
            # `## 病名` 是分块边界：一块 = 一个病，命中块的 heading_path 里带着病名
            lines.append(f"## {zh_name}")
            # 症状原文紧跟标题：语义召回主要靠这段跟患者原话对齐
            lines.append("表现：" + "；".join(syms))
            if depts.get(zh_name):
                lines.append(f"倾向科室：{depts[zh_name]}")
            lines.append("")

        # 病史线索/危险因素/生活背景**不进库**。它们不含症状，追问链路一个字都用
        # 不到（引擎只取 symptoms 小节），却因为句子短、措辞泛，在向量检索里
        # 抢分特别凶：实测"皮疹很痒"的 top8 里躺着"有严重过敏史""有频繁上呼吸道
        # 感染史"。原文仍留在 medrag_zh.json，要用随时能取。
        for rel, _label, _key in REL_SECTIONS:
            if rel == "has_symptomatology":
                continue
            for zh_name in members:
                for s in used[zh_name].get(rel, []):
                    if s not in texts:
                        missing.append(s)
        lines.append(f"<!-- source=medrag system={system} n={len(members)} -->")
        docs.append((f"MedRAG · {system}", "\n".join(lines)))

    if missing:
        raise KeyError("以下英文原句缺少校验过的译文，先补 data/previsit/"
                       f"medrag_zh.json：{missing[:5]}")
    if no_symptom:
        print(f"跳过 {len(no_symptom)} 个没有症状描述的病：{'、'.join(no_symptom)}")
    return docs


def main() -> int:
    docs = build_docs()
    for title, text in docs:
        heads = [ln for ln in text.splitlines() if ln.startswith("## ")]
        print(f"{title}: {len(heads)} 病, {len(text)} 字")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
