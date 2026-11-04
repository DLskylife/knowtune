"""知识图谱查询层（scripts/build_previsit_kg.py 产出的 kg.db）。

只回答预问诊真正需要的三个问题，别的都不做：

1. `ros()`——**听到咳嗽之后，下一句该问什么**。在三元组里查"含咳嗽的疾病
   还携带哪些症状"，按覆盖率排序。这是数据驱动的"系统回顾"，不是我手写
   的问题表，每一条都能说出依据（在 N 个含咳嗽的疾病里，M 个有呼吸困
   难）。
2. `dept_votes()`——**该去哪个科**。症状命中疾病 → 疾病的科室关系投票。
   只作为手写 triage_rules 没命中时的兜底，不抢规则的位。
3. `probes()`——**口语说法归一**。患者说"咳得厉害"，词表里是"咳嗽"。

库不存在/没建/查询出错一律返回空，**绝不抛异常**——图谱是增强项，它挂了
问诊必须照常问下去，这正是引擎"任意一步失败都降级继续"的同一条规矩。
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import Any, Iterable

DEFAULT_DB = "data/previsit/kg.db"

# 这些词**禁止被别名改写**。sameas 是从口语语料里挖出来的，它把"吐血"
# 同时挂给了"呕血"和"便血"——一旦把患者说的"吐血"改成"便血"，上消化道
# 出血就被指向了下消化道，急诊闸门也会跟着判错方向。危险症状一律原样
# 保留，交给引擎的急诊规则去处理，图谱不掺和。
PROTECTED = frozenset({
    "吐血", "呕血", "咯血", "咳血", "黑便", "便血", "昏迷", "抽搐", "惊厥",
    "意识不清", "意识模糊", "呼吸困难", "窒息", "胸痛", "剧烈头痛", "晕厥",
    "大出血", "自杀", "服毒",
})

# OpenCMKG 自己接错的别名，必须屏蔽。这不是"翻译不准"，是**指错症状**：
#   sameas 里写着 ("耳鸣", "喉鸣") —— 把喉鸣当耳鸣的口语说法。于是
#   "突然出现呼吸困难和喉鸣" 归一成"耳鸣"，追问链路会去问患者耳朵响不响。
# 上游 1.8 万条别名里这类污染是常态，逐个改库不现实，热路径上否决即可。
ALIAS_VETO: dict[str, frozenset[str]] = {
    "喉鸣": frozenset({"耳鸣", "鼻音"}),
    "鼻音": frozenset({"喉鸣声"}),
}

# **不能主动问**的症状。两类：

# 一、患者没法回答的（真昏迷了打不了字）；
# 二、化验/体征术语（"有没有蛋白尿""有没有反跳痛"）——那是医生查体和
#     化验单的语言，问患者等于让他猜。预问诊只问**患者自己能感觉到**的
#     东西，这条线画清了，追问才不会变成审问。
#
# 晕厥不在名单里：晕倒过是患者能答、且必须问的。
ASK_BLOCK_KW = (
    # 一、意识/生命体征层面
    "昏迷", "休克", "意识障碍", "意识模糊", "昏睡", "瘫痪", "死亡",
    "骤停", "衰竭", "植物状态", "睁眼昏迷", "谵妄",
    # 二、化验指标
    "蛋白尿", "尿蛋白", "血尿", "白细胞", "红细胞", "血小板", "血红蛋白",
    "抗体", "抗原", "转氨酶", "胆红素", "肌酐", "电解质", "血脂", "血糖值",
    # 三、查体体征（患者自己查不出来）
    "压痛", "反跳痛", "叩击痛", "叩痛", "啰音", "杂音", "肝掌", "蜘蛛痣",
    "杵状指", "颈强直", "病理反射", "肌张力", "淋巴结肿大", "皮疹形态",
)

# 症状 vs 既往病的分界：**靠医学常识，不靠统计**。
#
# 试过按 df 分（"作为症状被 >=200 个疾病携带就算症状"），当场翻车：
# 腹痛 1001 是症状、高血压 72 是病史，可**咳嗽只有 56**——它被拆成了
# 慢性咳嗽/咳嗽变异性哮喘等细分名，df 反而低，于是"咳嗽"被判成既往病，
# 患者的主诉直接被踢出症状池。统计在这件事上不可靠。
#
# 词表是临床常识，短、稳定、可维护：这些是**病**，不是患者此刻的感受。
CHRONIC_CONDITIONS = frozenset({
    "高血压", "糖尿病", "冠心病", "心梗", "心肌梗死", "心绞痛", "心衰",
    "心力衰竭", "房颤", "心律失常", "脑梗", "脑出血", "脑卒中", "中风",
    "哮喘", "慢阻肺", "肺气肿", "肺结核", "肺心病", "支气管扩张",
    "肝炎", "乙肝", "丙肝", "肝硬化", "脂肪肝", "胆囊炎", "胆结石",
    "肾炎", "肾病", "肾衰竭", "尿毒症", "肾结石", "前列腺增生",
    "甲亢", "甲减", "甲状腺功能亢进", "甲状腺功能减退", "甲状腺结节",
    "贫血", "白血病", "血小板减少", "血友病",
    "肿瘤", "癌症", "恶性肿瘤", "息肉", "囊肿",
    "癫痫", "帕金森", "阿尔茨海默", "痴呆", "抑郁", "焦虑", "精神分裂",
    "高血脂", "高脂血症", "痛风", "骨质疏松", "类风湿", "红斑狼疮",
    "艾滋病", "梅毒", "乙肝携带", "结核",
})


class KG:
    def __init__(self, db_path: str | Path | None = DEFAULT_DB, *, enabled: bool = True) -> None:
        self.path = Path(db_path) if db_path else None
        self.enabled = bool(enabled)
        self._con: sqlite3.Connection | None = None
        self._vocab_cache: dict[str, int] | None = None
        self._disease_cache: dict[str, int] | None = None
        self.error: str | None = None

    # ---------------------------------------------------------------- 连接
    @property
    def ok(self) -> bool:
        """库在不在、能不能查。查一次就记住结论，别在热路径上反复开文件。"""
        if not self.enabled or self.path is None:
            return False
        if self._con is not None:
            return True
        if not self.path.exists():
            self.error = f"知识图谱库不存在：{self.path}"
            return False
        try:
            # check_same_thread=False：FastAPI 的同步端点跑在线程池里，同一个
            # 引擎会被不同线程调用（参数面板保存后还会在另一个线程里 close）。
            # 连接是只读的，而本机 SQLite 是 serialized 模式（threadsafety=3），
            # 跨线程共用一个只读连接是安全的；不放开这一条，第二句话就会报
            # "SQLite objects created in a thread can only be used in that same
            # thread"，图谱静默失效——比慢更难发现。
            self._con = sqlite3.connect(
                f"file:{self.path.as_posix()}?mode=ro", uri=True,
                check_same_thread=False)
            self._con.execute("SELECT 1 FROM symptom LIMIT 1")
        except sqlite3.Error as exc:
            self._con = None
            self.error = f"知识图谱库打不开：{exc}"
            return False
        return True

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            except sqlite3.Error:
                # 连接可能建在另一个线程上（参数面板保存 → 换配置 → 关旧连接）。
                # 关不掉就交给 GC，不能让"清理旧资源"变成一次 500。
                pass
            self._con = None

    # ---------------------------------------------------------------- 归一
    def probes(self, term: str, limit: int = 8) -> list[str]:
        """把一句话里的说法映射成词表里的标准症状名（包含匹配，长词优先）。"""
        if not self.ok or not term:
            return []
        try:
            rows = self._con.execute(
                "SELECT symptom FROM symptom WHERE instr(?, symptom) > 0 "
                "ORDER BY length(symptom) DESC LIMIT ?", (str(term), limit)).fetchall()
        except sqlite3.Error as exc:
            self.error = f"图谱查询失败：{exc}"
            return []
        return [r[0] for r in rows]

    # ---------------------------------------------------------------- 词表
    def _vocab(self) -> dict[str, int]:
        """症状词表 → 该症状被多少病携带（df）。只读一次，常驻内存。

        df 是后面两步的共用底座：归一要挑"更常见"的那个说法，疾病锁定要
        按 DF 判断一个症状有没有区分度。两处都别自己去查库。
        """
        if self._vocab_cache is not None:
            return self._vocab_cache
        if not self.ok:
            return {}
        try:
            self._vocab_cache = dict(self._con.execute(
                "SELECT symptom, n_disease FROM symptom").fetchall())
        except sqlite3.Error as exc:
            self.error = f"图谱查询失败：{exc}"
            self._vocab_cache = {}
        return self._vocab_cache

    # ---------------------------------------------------------------- 同义归一
    def normalize(self, text: str, limit: int = 12) -> list[dict[str, Any]]:
        """把患者口语换成词表里的标准词：\"拉稀\"→\"稀便\"、\"肚子疼\"→\"腹痛\"。

        走 `sameas` 表（OpenCMKG 的一万八千条口语别名），三条硬规矩，每条
        都是踩过坑才加的：

        1. **落点必须在症状词表里**。sameas 的规范端不一定是词表用词——
           \"发烧\"→\"发热\"，可词表里根本没有\"发热\"（叫\"发烧\"），照搬
           会让下游一条疾病都查不到。规范端不在词表就退回口语端。
        2. **区间不重叠，长词优先**。\"肚子疼\"里含\"肚子\"，两个都归一会
           凭空多出一个\"腹胀\"——患者没说胀，假阳性会污染整个疾病打分。
        3. **同区间多个候选时取 df 大的**。\"拉稀\"既能归到\"稀便\"（25 个病）
           也可能归到高频说法，选常见的那个，罕见说法会把罕见病顶上来。

        返回 `[{"raw","std","df","start","end"}]`，按出现位置排序。
        """
        if not self.ok or not (text or "").strip():
            return []
        t = str(text)
        vocab = self._vocab()
        if not vocab:
            return []
        try:
            rows = self._con.execute(
                "SELECT b, a FROM sameas WHERE instr(?, b) > 0", (t,)).fetchall()
        except sqlite3.Error as exc:
            self.error = f"图谱查询失败：{exc}"
            return []

        # 同一区间可能有多条映射，先归并成一个区间一个落点（取 df 最大）
        best: dict[tuple[int, int], tuple[str, str, int]] = {}
        for raw, std in rows:
            raw, std = str(raw or "").strip(), str(std or "").strip()
            if len(raw) < 2:            # 单字别名会把无关句子改烂
                continue
            start = t.find(raw)
            if start < 0:
                continue
            pick = ""
            for cand in (std, raw):     # 规矩 1：落点必须落在词表里
                if cand in vocab:
                    pick = cand
                    break
            if not pick:
                continue
            if pick in ALIAS_VETO.get(raw, ()):   # 上游接错的别名，不认
                continue
            if raw in PROTECTED:
                # 危险症状不被改写，原话留给急诊闸门判。词表里没有对应词
                # 就**整条放弃**，绝不退回到别名给的落点——"吐血"在 sameas
                # 里同时挂着"呕血"和"便血"，取错一个就是把上消化道出血
                # 指向下消化道，这不是质量问题，是安全问题。
                if raw not in vocab:
                    continue
                pick = raw
            key = (start, start + len(raw))
            df = int(vocab[pick])
            if key not in best or df > best[key][2]:   # 规矩 3
                best[key] = (raw, pick, df)
        # 患者原话里本来就含标准词（\"腹痛两天\"）也算命中
        for sym, df in vocab.items():
            if len(sym) < 2:
                continue
            start = t.find(sym)
            if start < 0:
                continue
            key = (start, start + len(sym))
            if key not in best:
                best[key] = (sym, sym, int(df))

        # 规矩 2：长度降序占位，重叠的一律丢
        items = sorted(best.items(), key=lambda kv: (-(kv[0][1] - kv[0][0]), kv[0][0]))
        used: list[tuple[int, int]] = []
        out: list[dict[str, Any]] = []
        for (start, end), (raw, std, df) in items:
            if any(not (end <= s2 or start >= e2) for s2, e2 in used):
                continue
            used.append((start, end))
            out.append({"raw": raw, "std": std, "df": df, "start": start, "end": end})
            if len(out) >= limit:
                break
        out.sort(key=lambda x: x["start"])
        return out

    # ---------------------------------------------------------------- 疾病锁定
    def candidate_diseases(self, symptoms: list[str], limit: int = 5,
                           min_df: int = 30) -> list[dict[str, Any]]:
        """路 A：已确认症状 → 候选疾病（本地 SQLite，OpenCMKG 8685 个病）。

        打分不是简单的\"命中几个症状\"——\"乏力\"命中 776 个病，\"咯血\"只命中
        几十个，信息量差一个数量级。所以按 **IDF 加权**：

            score = Σ min(log(总病数/df), 4.0) × (0.6 + 0.4 × 覆盖率)

        两个刻意的限制：

        - **IDF 封顶 4.0**：不封顶的话\"稀便\"（25 个病）权重 5.85，能把
          \"肾胚胎瘤\"这种罕见病顶到\"肠胃炎\"前面——实测就是这么错的。
        - **df < min_df 的症状不参与锁定**：只被几十个病携带的说法匹配噪声
          太大，它该用于追问，不该用于圈病。

        这是**排序线索，不是诊断**：结果只用于决定\"还能问什么\"和科室倾向，
        绝不原样告诉患者\"你得了 X\"。
        """
        if not self.ok or not symptoms:
            return []
        syms = [s for s in dict.fromkeys(symptoms) if s]
        if not syms:
            return []
        vocab = self._vocab()
        syms = [s for s in syms if int(vocab.get(s, 0)) >= min_df]
        if not syms:
            return []
        ph = ",".join("?" * len(syms))
        try:
            total = self._con.execute(
                "SELECT COUNT(DISTINCT disease) FROM disease_symptom").fetchone()[0] or 1
            rows = self._con.execute(
                f"""SELECT disease, COUNT(*) AS hits
                    FROM disease_symptom WHERE symptom IN ({ph})
                    GROUP BY disease ORDER BY hits DESC LIMIT ?""",
                (*syms, limit * 8)).fetchall()
        except sqlite3.Error as exc:
            self.error = f"图谱查询失败：{exc}"
            return []
        if not rows:
            return []
        names = [r[0] for r in rows]
        pn = ",".join("?" * len(names))
        try:
            n_sym = dict(self._con.execute(
                f"SELECT disease, COUNT(*) FROM disease_symptom WHERE disease IN ({pn}) "
                f"GROUP BY disease", names).fetchall())
            depts: dict[str, str] = {}
            for d, dept in self._con.execute(
                    f"SELECT disease, dept FROM disease_dept WHERE disease IN ({pn})", names):
                depts.setdefault(str(d), str(dept))
        except sqlite3.Error as exc:
            self.error = f"图谱查询失败：{exc}"
            return []
        idf = {s: min(math.log((total + 1) / (int(vocab.get(s, 1)) + 1)), 4.0) for s in syms}
        out: list[dict[str, Any]] = []
        for name, hits in rows:
            w = sum(idf[s] for s in syms)
            cover = hits / max(1, int(n_sym.get(name, hits)))
            out.append({
                "disease": str(name),
                "hits": int(hits),
                "matched": len(syms),
                "coverage": round(cover, 3),
                "score": round(w * (0.6 + 0.4 * cover), 3),
                "dept": depts.get(str(name), ""),
            })
        out.sort(key=lambda d: (-d["score"], -d["hits"], d["disease"]))
        return out[:limit]

    def symptom_vocab(self) -> dict[str, int]:
        """症状词表 {症状: 携带该症状的病数}。给引擎判断"这个词算不算症状、
        够不够常见"，一次取全再判断，别在热路径上反复查库。"""
        return dict(self._vocab())

    def disease_names(self) -> set[str]:
        """病名集合。用来把"高血压"这种**既是病名又被挂成症状**的词分流。

        OpenCMKG 本身分不清：`高血压` 在 symptom 表里（df=72），同时又有 7 个
        疾病的病名就叫"高血压"。把它当症状算进线索，候选病和科室投票都会被
        带歪（"血压高"该算病史，不是主诉症状）。"""
        return set(self._dis_degree())

    def _dis_degree(self) -> dict[str, int]:
        """{病名: 它有几种症状}。缓存在内存，别在热路径上反复查。"""
        if self._disease_cache is not None:
            return self._disease_cache
        if not self.ok:
            return {}
        try:
            self._disease_cache = dict(self._con.execute(
                "SELECT disease, COUNT(*) FROM disease_symptom GROUP BY disease"))
        except sqlite3.Error:
            self._disease_cache = {}
        return self._disease_cache

    def classify_terms(self, terms: list[str]) -> tuple[list[str], list[str]]:
        """把一个词表分成 (症状, 既往病)。

        判据是**医学常识**（CHRONIC_CONDITIONS），不是"在不在病名表里"：
        OpenCMKG 里 `腹痛` 同时是 1001 个疾病的症状和 9 个疾病的名字，光看
        病名表会把腹痛判成病；而 `高血压` 是 72 个疾病的症状、7 个疾病的名字，
        它确实该算病史。

        顺序也是刻意的：**先看病史词表，再看症状词表**。"高血压"两张表里
        都有，必须先判成病史；"咳嗽"只在症状表里（病名表里也有几个叫咳嗽的
        病，但不该因此被踢出主诉）。

        两边都不在词的直接丢——那不是医学词。
        """
        vocab = self._vocab()
        diseases = self._dis_degree()
        syms: list[str] = []
        conds: list[str] = []
        for t in dict.fromkeys(str(x) for x in terms if x):
            if t in CHRONIC_CONDITIONS or any(k in t for k in CHRONIC_CONDITIONS):
                conds.append(t)
            elif t in vocab:
                syms.append(t)
            elif t in diseases:
                conds.append(t)
        return syms, conds

    def askable(self, term: str) -> bool:
        """这个症状能不能主动拿去问患者（昏迷/休克类不行）。"""
        t = str(term or "")
        if not t:
            return False
        return not any(k in t for k in ASK_BLOCK_KW)

    # ---------------------------------------------------------------- 系统回顾
    def ros(self, seeds: list[str], exclude: Iterable[str] = (),
            limit: int = 20, min_ratio: float = 0.05) -> list[dict[str, Any]]:
        """给定已确认症状，返回还该问的伴随症状（按跨疾病覆盖率排序）。

        seeds 是"已经确认有的症状"，不是主诉原话——主诉要先过 `probes()`
        归一，否则"咳得厉害"在库里一条都命中不了。
        """
        if not self.ok or not seeds:
            return []
        seeds = [s for s in dict.fromkeys(seeds) if s]
        skip = set(exclude) | set(seeds)
        ph = ",".join("?" * len(seeds))
        try:
            base = self._con.execute(
                f"SELECT COUNT(DISTINCT disease) FROM disease_symptom "
                f"WHERE symptom IN ({ph})", seeds).fetchone()[0]
            if not base:
                return []
            rows = self._con.execute(
                f"""SELECT ds2.symptom AS sym, COUNT(DISTINCT ds1.disease) AS co
                    FROM disease_symptom ds1
                    JOIN disease_symptom ds2 ON ds1.disease = ds2.disease
                    WHERE ds1.symptom IN ({ph}) AND ds2.symptom NOT IN ({ph})
                    GROUP BY ds2.symptom ORDER BY co DESC LIMIT ?""",
                (*seeds, *seeds, limit * 4)).fetchall()
        except sqlite3.Error as exc:
            self.error = f"图谱查询失败：{exc}"
            return []
        out = []
        for sym, co in rows:
            if sym in skip:
                continue
            ratio = co / base
            if ratio < min_ratio:
                continue
            out.append({"symptom": sym, "ratio": round(ratio, 3), "co": co, "base": base})
            if len(out) >= limit:
                break
        return out

    # ---------------------------------------------------------------- 分诊投票
    def dept_votes(self, symptoms: list[str], limit: int = 5) -> list[dict[str, Any]]:
        """症状 → 疾病 → 科室的加权投票。只给兜底用，规则命中时不用它。"""
        if not self.ok or not symptoms:
            return []
        ph = ",".join("?" * len(symptoms))
        try:
            rows = self._con.execute(
                f"""SELECT d.dept AS dept, COUNT(DISTINCT ds.disease) AS n
                    FROM disease_symptom ds
                    JOIN disease_dept d ON d.disease = ds.disease
                    WHERE ds.symptom IN ({ph})
                    GROUP BY d.dept ORDER BY n DESC LIMIT ?""",
                (*symptoms, limit)).fetchall()
        except sqlite3.Error as exc:
            self.error = f"图谱查询失败：{exc}"
            return []
        total = sum(r[1] for r in rows) or 1
        return [{"dept": r[0], "n": r[1], "ratio": round(r[1] / total, 3)} for r in rows]
