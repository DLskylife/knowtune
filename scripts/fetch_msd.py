"""从《默沙东诊疗手册》大众版抓常见病正文，转 markdown 存进 data/corpus/现代医学。

为什么要这个脚本而不是手工贴：16 篇、每篇几千字，手贴必然漏段、混入导航。
trafilatura 的正文抽取在中文 MSD 页面上表现稳定，表格能保留成 markdown 表。

抓下来的每篇都写一段来源头（URL + 抓取时间 + 免责说明）。知识库里没有出处的
段落等于无法核对，临床上比没有更糟。
"""

from __future__ import annotations

import datetime as dt
import re
import sys
import urllib.request
from pathlib import Path

import trafilatura

OUT = Path(__file__).resolve().parents[1] / "data" / "corpus" / "现代医学"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# (输出文件名, 中文病名, URL)
TARGETS = [
    ("高血压（现代医学参考）.md", "高血压", "https://www.msdmanuals.cn/home/heart-and-blood-vessel-disorders/high-blood-pressure/high-blood-pressure"),
    ("2型糖尿病（现代医学参考）.md", "2 型糖尿病", "https://www.msdmanuals.cn/home/hormonal-and-metabolic-disorders/diabetes-mellitus-and-low-blood-sugar-hypoglycemia/type-2-diabetes-mellitus"),
    ("普通感冒（现代医学参考）.md", "普通感冒", "https://www.msdmanuals.cn/home/infections/respiratory-virus-infections/common-cold"),
    ("流行性感冒（现代医学参考）.md", "流行性感冒", "https://www.msdmanuals.cn/home/infections/respiratory-virus-infections/influenza-flu"),
    ("急性胃肠炎（现代医学参考）.md", "急性胃肠炎", "https://www.msdmanuals.cn/home/digestive-disorders/gastroenteritis/overview-of-gastroenteritis"),
    ("成人腹泻（现代医学参考）.md", "成人腹泻", "https://www.msdmanuals.cn/home/digestive-disorders/symptoms-of-digestive-disorders/diarrhea-in-adults"),
    ("胃食管反流病（现代医学参考）.md", "胃食管反流病", "https://www.msdmanuals.cn/home/digestive-disorders/esophageal-and-swallowing-disorders/gastroesophageal-reflux-disease-gerd"),
    ("失眠（现代医学参考）.md", "失眠与日间过度嗜睡", "https://www.msdmanuals.cn/home/brain-spinal-cord-and-nerve-disorders/sleep-disorders/insomnia-and-excessive-daytime-sleepiness-eds"),
    ("腰痛（现代医学参考）.md", "腰痛", "https://www.msdmanuals.cn/home/bone-joint-and-muscle-disorders/low-back-and-neck-pain/low-back-pain"),
    ("偏头痛（现代医学参考）.md", "偏头痛", "https://www.msdmanuals.cn/home/brain-spinal-cord-and-nerve-disorders/headaches/migraines"),
    ("痛风（现代医学参考）.md", "痛风", "https://www.msdmanuals.cn/home/bone-joint-and-muscle-disorders/gout-and-calcium-pyrophosphate-arthritis/gout"),
    ("特应性皮炎（现代医学参考）.md", "特应性皮炎（湿疹）", "https://www.msdmanuals.cn/home/skin-disorders/itching-and-dermatitis/atopic-dermatitis-eczema"),
    ("焦虑障碍（现代医学参考）.md", "焦虑障碍", "https://www.msdmanuals.cn/home/mental-health-disorders/anxiety-and-trauma-and-stressor-related-disorders/overview-of-anxiety-disorders"),
    ("哮喘（现代医学参考）.md", "哮喘", "https://www.msdmanuals.cn/home/lung-and-airway-disorders/asthma/asthma"),
    ("儿童发热（现代医学参考）.md", "儿童发热", "https://www.msdmanuals.cn/home/children-s-health-issues/symptoms-in-infants-and-children/fever-in-infants-and-children"),
    # 面向患者的 quick-facts 版：专业版这页正文本身只有 900 字，太薄，撑不起检索块。
    ("缺铁性贫血（现代医学参考）.md", "缺铁性贫血", "https://www.msdmanuals.cn/home/quick-facts-blood-disorders/anemia/iron-deficiency-anemia"),
    ("鼻炎（现代医学参考）.md", "鼻炎", "https://www.msdmanuals.cn/home/ear-nose-and-throat-disorders/nose-and-sinus-disorders/rhinitis"),
    ("便秘（现代医学参考）.md", "成人便秘", "https://www.msdmanuals.cn/home/digestive-disorders/symptoms-of-digestive-disorders/constipation-in-adults"),
    ("血脂异常（现代医学参考）.md", "血脂异常", "https://www.msdmanuals.cn/home/hormonal-and-metabolic-disorders/cholesterol-disorders/dyslipidemia"),
    ("消化不良（现代医学参考）.md", "消化不良", "https://www.msdmanuals.cn/home/digestive-disorders/symptoms-of-digestive-disorders/indigestion"),
]

HEADER = """# {title}（现代医学参考）\n\n"""
# 出处与免责放在**文末**独立成节，不放在正文前。原因很实在：分块是按标题切的，
# 正文前那几行 `> 来源…` 会并进第一个内容块，实测占掉该块 44% 的篇幅 —— 一个
# 「普通感冒有哪些症状」的向量里混着一串 URL，检索时它就被自己稀释了。
FOOTER = (
    "\n\n## 资料来源与使用说明\n\n"
    "《默沙东诊疗手册》大众版 · {url} · 抓取 {date}。"
    "本条为现代医学参考，不替代执业医师面诊，具体用药与剂量以临床医嘱为准；"
    "维吾尔医辨证论治见维医诊疗指南各篇。\n"
)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", errors="ignore")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    only = sys.argv[1] if len(sys.argv) > 1 else None
    today = dt.date.today().isoformat()
    ok = 0
    for name, title, url in TARGETS:
        if only and only not in name:
            continue
        try:
            html = fetch(url)
            md = trafilatura.extract(
                html, output_format="markdown", include_tables=True,
                include_comments=False, include_links=False, favor_precision=True,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {name}: {type(e).__name__} {e}")
            continue
        if not md or len(md) < 800:
            print(f"[THIN] {name}: 抽出 {len(md or '')} 字符，跳过")
            continue
        md = re.sub(r"\n{3,}", "\n\n", md).strip()
        text = (HEADER.format(title=title) + md
                + FOOTER.format(url=url, date=today))
        (OUT / name).write_text(text, encoding="utf-8", newline="\n")
        print(f"[OK] {name} — {len(md)} 字符")
        ok += 1
    print(f"\n完成 {ok} 篇 → {OUT}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
