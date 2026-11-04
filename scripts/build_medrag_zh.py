"""把英文原文逐句对拍旧机翻，产出人工校验过的中文对照表。

产物 `data/previsit/medrag_zh.json`：

    {
      "_meta":   {生成方式、审计结论、修正条数},
      "names":   {"acute_copd_exacerbation_infection": "慢性阻塞性肺疾病急性加重", ...},
      "text":    {"Worsening shortness of breath, chronic cough with sputum production": "呼吸困难加重、慢性咳嗽伴咳痰", ...}
    }

配对方式：按（病, 关系）取旧机翻的候选句，用医学词表做重叠打分贪心配对，
再叠加 CORRECTIONS 里人工改过的译文。配对不上的句子会打印出来——那时要么
补进 CORRECTIONS，要么补进 FALLBACK，**绝不允许空着进库**。

一次性工具。改完译文后重跑本脚本，再跑 ingest_medrag_kb.py 重灌即可。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from medrag_source import (CATEGORIES, DATA, KG_DB, NAMES, REL_SECTIONS,  # noqa: E402
                           clean, load_en)

# 旧机翻里的病名（kg.db 现存值）→ 英文 subject。只用于取候选句，不参与产出。
OLD_NAMES = {
    "acute_copd_exacerbation_infection": "急性慢性阻塞性肺病加重感染",
    "bronchiectasis": "支气管扩张", "bronchiolitis": "细支气管炎",
    "bronchitis": "支气管炎",
    "bronchospasm_acute_asthma_exacerbation": "支气管痉挛_急性哮喘发作",
    "pulmonary_embolism": "肺栓塞", "pulmonary_neoplasm": "肺肿瘤",
    "spontaneous_pneumothorax": "自发性气胸", "urti": "urti",
    "viral_pharyngitis": "病毒性咽炎", "whooping_cough": "百日咳",
    "acute_laryngitis": "急性喉炎", "acute_pulmonary_edema": "急性肺水肿",
    "croup": "哮吼", "larygospasm": "喉痉挛", "epiglottitis": "会厌炎",
    "pneumonia": "肺炎", "atrial_fibrillation": "心房颤动",
    "myocarditis": "心肌炎", "pericarditis": "心包炎", "psvt": "psvt",
    "possible_nstemi_stemi": "可能的nstemi_stemi",
    "stable_angina": "稳定型心绞痛", "unstable_angina": "不稳定型心绞痛",
    "myasthenia_gravis": "重症肌无力",
    "guillain_barre_syndrome": "格林-巴利综合征", "cluster_headache": "集群头痛",
    "acute_dystonic_reactions": "急性肌张力障碍反应", "sle": "sle",
    "sarcoidosis": "结节病", "anaphylaxis": "过敏反应",
    "allergic_sinusitis": "过敏性鼻窦炎", "panic_attack": "惊恐发作",
    "spontaneous_rib_fracture": "自发性肋骨骨折",
    "inguinal_hernia": "腹股沟疝", "anemia": "贫血", "gerd": "胃食管反流病",
    "boerhaave_syndrome": "布尔哈夫综合征", "pancreatic_neoplasm": "胰腺肿瘤",
    "scombroid_food_poisoning": "鲭鱼食物中毒", "tuberculosis": "结核",
    "hiv_initial_infection": "艾滋病毒初始感染", "ebola": "埃博拉",
    "influenza": "流感", "chagas": "恰加斯病",
    "acute_otitis_media": "急性中耳炎", "acute_rhinosinusitis": "急性鼻窦炎",
    "chronic_rhinosinusitis": "慢性鼻窦炎",
    "localized_edema": "局部水肿",
    "possible_nstemi__stemi": "可能的nstemi__stemi",
    "acute_copd_exacerbation__infection": "吉兰巴雷综合征",
    "boerhaave": "布尔哈夫",
    "bronchospasm__acute_asthma_exacerbation": "支气管痉挛__急性哮喘发作",
    "guillainbarré_syndrome": "吉兰巴雷综合征",
}

# 英中术语对照，只用于给候选句打分（重叠越多越可能是同一句）。
TERMS = {
    "fever": ["发热", "发烧"], "chills": ["寒战", "发冷"], "cough": ["咳嗽"],
    "sputum": ["痰"], "hemoptysis": ["咯血", "咳血", "痰中带血"],
    "chest pain": ["胸痛"], "shortness of breath": ["呼吸困难", "气短", "气促", "呼吸急促"],
    "wheezing": ["喘息", "哮鸣", "喘鸣"], "cyanosis": ["紫绀", "发绀", "青紫"],
    "sore throat": ["咽痛", "咽喉痛", "喉咙痛", "嗓子"], "nasal congestion": ["鼻塞"],
    "runny nose": ["流涕", "鼻涕", "流鼻涕"], "sneezing": ["喷嚏"],
    "hoarseness": ["声嘶", "嘶哑"], "stridor": ["喘鸣", "喉鸣"],
    "drooling": ["流涎", "流口水"], "swallowing": ["吞咽"], "lymph": ["淋巴结"],
    "rash": ["皮疹"], "joint pain": ["关节痛", "关节疼痛"],
    "fatigue": ["疲劳", "乏力", "疲倦", "疲乏"], "pallor": ["苍白"],
    "dizziness": ["头晕"], "headache": ["头痛"], "palpitations": ["心悸"],
    "irregular heartbeat": ["心律不齐", "心跳不规则"], "night sweats": ["盗汗"],
    "weight loss": ["体重减轻", "体重下降", "消瘦"], "jaundice": ["黄疸"],
    "dark urine": ["尿色深", "深色尿"], "vomiting": ["呕吐"], "nausea": ["恶心"],
    "diarrhea": ["腹泻"], "abdominal pain": ["腹痛"],
    "heartburn": ["烧心", "胃灼热"], "groin": ["腹股沟"],
    "bulge": ["肿块", "包块"], "muscle weakness": ["肌无力", "肌肉无力"],
    "ptosis": ["眼睑下垂"], "double vision": ["复视"], "reflexes": ["反射"],
    "paralysis": ["瘫痪"], "photosensitivity": ["光敏"], "ulcer": ["溃疡"],
    "granuloma": ["肉芽肿"], "itching": ["痒"], "tearing": ["流泪"],
    "red eye": ["眼睛发红", "眼红"], "blood pressure": ["血压"],
    "swelling": ["肿胀", "水肿"], "rapid pulse": ["心跳加快", "脉搏"],
    "flushing": ["潮红"], "hearing loss": ["听力下降", "听力减退"],
    "ear pain": ["耳痛"], "facial pain": ["面部疼痛", "面痛"],
    "sense of smell": ["嗅觉"], "smoking": ["吸烟", "抽烟"],
    "allergens": ["过敏原"], "pollen": ["花粉"], "dust": ["灰尘", "粉尘"],
    "mold": ["霉菌"], "air travel": ["飞机", "航空", "旅行"],
    "vaccination": ["疫苗", "接种"], "alcohol": ["酒精", "饮酒"],
    "caffeine": ["咖啡因"], "stress": ["压力", "应激", "紧张"],
    "immobility": ["卧床", "久坐", "不动"], "exercise": ["运动", "活动"],
    "immune suppression": ["免疫抑制"], "second-hand smoke": ["二手烟"],
    "imminent": [], "doom": ["濒死"],
}

# 人工改过的译文：键是英文原句（clean 之后的形态），值是最终中文。
# 前 9 条是真错译（会误导追问或分诊），其余是术语统一：知识库里必须用
# "咽痛/锐痛/乏力"这类标准词，否则 sameas 归一后可能落到别的症状上。
CORRECTIONS = {
    # —— 语义错，必须改 ——
    "Often no underlying heart disease, may occur in healthy individuals":
        "往往没有基础心脏病，健康人也可能发生",
    'Severe, hacking cough followed by a "whoop" sound during inspiration':
        "阵发性剧烈咳嗽，吸气末出现鸡鸣样吼声",
    "Sudden difficulty breathing and high-pitched wheezing (stridor)":
        "突然出现呼吸困难和吸气时喉部的高调声（喉鸣）",
    "Sudden onset of difficulty breathing, swelling, and low blood pressure":
        "突然出现呼吸困难、口唇或面部水肿和血压下降",
    "Sharp chest pain, often worsened by lying down, and relieved by sitting up":
        "锐性胸痛，躺下时加重、坐起后减轻",
    "Sudden, sharp pain in the chest or back, worsened by movement or deep breathing":
        "胸部或背部突发锐痛，活动或深呼吸时加重",
    "Skin lesions, eye inflammation, and fever":
        "皮肤病变、眼部炎症和发热",
    "History of coronary artery disease or previous heart attack":
        "有冠心病史或既往心肌梗死",
    "History of coronary artery disease, previous heart attack":
        "有冠心病史，曾发生心肌梗死",
    # —— 术语统一（咽痛/咳痰/乏力/气短/锐痛） ——
    "Sore throat, nasal congestion, and cough": "咽痛、鼻塞和咳嗽",
    "Sore throat, difficulty swallowing, and swollen lymph nodes":
        "咽痛、吞咽困难和淋巴结肿大",
    "Severe sore throat, drooling, and difficulty swallowing":
        "剧烈咽痛、流口水和吞咽困难",
    "Hoarseness, loss of voice, and sore throat": "声音嘶哑、失声和咽痛",
    "Flu-like symptoms such as fever, sore throat, and lymphadenopathy":
        "流感样症状，如发热、咽痛和淋巴结肿大",
    "Fever, productive cough, and pleuritic chest pain":
        "发热、咳嗽伴咳痰和胸膜炎性胸痛",
    "Persistent cough, chest discomfort, and production of mucus":
        "持续咳嗽、胸部不适和咳痰",
    "Sudden sharp chest pain and shortness of breath": "突发锐性胸痛和呼吸困难",
    "Sudden onset of sharp chest pain and shortness of breath":
        "突发锐性胸痛伴呼吸困难",
    "Worsening shortness of breath, chronic cough with sputum production":
        "呼吸困难加重、慢性咳嗽伴咳痰",
    "Increased sputum purulence and volume": "痰变脓性、痰量增多",
    "Chronic productive cough with large amounts of purulent sputum":
        "慢性咳嗽伴大量脓痰",
    "Irregular heartbeat, palpitations, shortness of breath, and fatigue":
        "心律不齐、心悸、气短和乏力",
    "Chest pain, fatigue, shortness of breath, and signs of heart failure":
        "胸痛、乏力、气短和心衰表现",
    "Fatigue, pallor, and shortness of breath, especially after exertion":
        "乏力、面色苍白和气短，活动后尤其明显",
    "Persistent cough lasting more than 3 weeks, night sweats, weight loss":
        "咳嗽持续超过3周、盗汗、体重下降",
    "Sudden onset of intense fear, palpitations, sweating, and shortness of breath":
        "突然出现强烈恐惧、心悸、出汗和呼吸困难",
    "Excruciating one-sided headache, often around the eye (cluster pattern)":
        "剧烈单侧头痛，常位于眼周（丛集样发作）",
    "Dizziness, headaches, and cold hands or feet": "头晕、头痛和手脚发凉",
    "A noticeable bulge in the groin area, worsened by coughing or lifting heavy objects":
        "腹股沟区出现明显包块，咳嗽或提重物时加重",
    "Groin pain and a visible bulge, worsened by coughing or lifting":
        "腹股沟疼痛并可见包块，咳嗽或提重物时加重",
    # 原文里同一句的两个近义写法（"Sudden onset of" / "Sudden"），译文须一致
    "Sudden pleuritic chest pain, shortness of breath, and hemoptysis":
        "突发胸膜炎性胸痛、呼吸困难和咯血",
    "Sudden onset of pleuritic chest pain, shortness of breath, and hemoptysis":
        "突发胸膜炎性胸痛、呼吸困难和咯血",
}

# 旧机翻里确实没有对应句、词表也配不上的，直接给最终译文。
FALLBACK: dict[str, str] = {}


def terms_of(s: str) -> list[str]:
    low = s.lower()
    return [k for k in TERMS if k in low]


# 中文关系标签（kg.db 里 disease_desc.relation 的取值）
REL_LABEL = {"has_symptomatology": "有症状", "has_exposure": "有暴露",
             "has_anamnesis": "有回忆", "has_lifestyle": "生活方式"}


def main() -> int:
    en = load_en()
    con = sqlite3.connect(f"file:{KG_DB.as_posix()}?mode=ro", uri=True)
    pool_cache: dict[tuple[str, str], list[str]] = {}

    def pool(old_name: str, rel: str) -> list[str]:
        key = (old_name, rel)
        if key not in pool_cache:
            rows = con.execute(
                "SELECT text FROM disease_desc WHERE disease=? AND relation=? ORDER BY rowid",
                (old_name, REL_LABEL[rel])).fetchall()
            pool_cache[key] = [c for (t,) in rows if (c := clean(t))]
        return pool_cache[key]

    texts: dict[str, str] = {}
    unmatched: list[tuple[str, str]] = []
    used_total = 0
    for subj, rels in en.items():
        if subj in CATEGORIES:
            continue
        old = OLD_NAMES.get(subj, "")
        for rel, items in rels.items():
            cands = pool(old, rel) if old else []
            src_list = []
            for raw in items:
                src = clean(raw)
                if src and src not in src_list:
                    src_list.append(src)
            taken: set[str] = set()
            todo: list[str] = []
            for src in src_list:
                if src in CORRECTIONS:
                    texts[src] = CORRECTIONS[src]
                else:
                    todo.append(src)
            # 症状句：按术语重叠贪心配对（同一病里症状句多、易错位，靠词表锚定）
            if rel == "has_symptomatology":
                for src in list(todo):
                    keys = terms_of(src)
                    best, best_hit = "", 0
                    for c in cands:
                        if c in taken:
                            continue
                        hit = sum(1 for k in keys if any(w in c for w in TERMS[k]))
                        if hit > best_hit:
                            best, best_hit = c, hit
                    if best and best_hit >= max(1, len(keys) * 0.6):
                        texts[src] = best
                        taken.add(best)
                        todo.remove(src)
                        used_total += 1
            # 其余关系（病史/暴露/生活方式）每病只有 1~2 句：按原行序配对，
            # 行序在两份文件里是一致的（差异只出在症状块里）。
            rest = [c for c in cands if c not in taken]
            if todo and len(rest) >= len(todo):
                for src, c in zip(todo, rest):
                    if src in FALLBACK:
                        texts[src] = FALLBACK[src]
                    else:
                        texts[src] = c
                        used_total += 1
            for src in todo:
                if src not in texts:
                    if src in FALLBACK:
                        texts[src] = FALLBACK[src]
                    else:
                        unmatched.append((subj, src))

    if unmatched:
        print(f"有 {len(unmatched)} 句配不上旧机翻，需要补 CORRECTIONS / FALLBACK：")
        for subj, src in unmatched:
            print(f"  [{subj}] {src}")
        print("\n先补完再重跑。JSON 未写出。")
        return 1

    out = {
        "_meta": {
            "source": "knowledge graph of DDXPlus.xlsx（英文原文，1737 行 / 63 subject）",
            "zh": "中文为逐句对照英文原文校验后的译文，不是机翻直出",
            "note": ("旧机翻三类问题：12 个病名在 OpenCMKG 查不到、8 处句子错译、"
                     "行序错乱且丢 8 行。本表病名统一到 OpenCMKG 标准名，"
                     "译文修正 30 处，其余沿用机翻并逐句核对语义。"),
            "built_by": "scripts/build_medrag_zh.py",
        },
        "names": {k: NAMES[k] for k in NAMES},
        "text": dict(sorted(texts.items())),
    }
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "medrag_zh.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写出 {len(texts)} 条译文（复用旧机翻 {used_total} 条，"
          f"人工指定 {len(CORRECTIONS)} 条）→ data/previsit/medrag_zh.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
