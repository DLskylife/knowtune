"""arena.py 的端到端自检：起一个假的 OpenAI 端点，跑完生成→打分→落盘→统计。

重点验证**盲测归属**：骰子掷到什么，分数就必须记到谁头上。这一环错了，
整份打分数据都是反的，而且肉眼看不出来。

顺带验证通用化：同一套代码换一份档案（维度、角色名、题库全变），落盘结构
必须跟着变——不然所谓"通用"只是界面换了个标题。

    evals/.venv/Scripts/python.exe evals/selftest.py
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arena      # noqa: E402
import backends   # noqa: E402
import casebook   # noqa: E402
import profiles   # noqa: E402
from common import load_cases  # noqa: E402

MAX_D = profiles.MAX_DIMS


class Fake(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        model = body.get("model", "")
        # 模型名直接回显，便于断言左右到底是谁
        text = f"{model} 的回复"
        out = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):  # 静音
        pass


def serve() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


def _flat(l_scores: list[int], r_scores: list[int]) -> list[int]:
    """把 4 维分数铺成界面的 12 个滑块值（后两槽留 3）。"""
    pad = [3] * (MAX_D - len(l_scores))
    return l_scores + pad + r_scores + pad


def main() -> int:
    srv, url = serve()

    # ① 预问诊档案：20 条题库、4 维、患者/医生
    prof = profiles.get("previsit")
    cases = load_cases(prof["cases"], prof["rules"])
    assert len(cases) == 20, len(cases)
    assert rec_checks(cases) >= 5, "红旗题的规则体检没有挂上"

    run_file = arena.RUNS / "_selftest.jsonl"
    run_file.unlink(missing_ok=True)
    arena.SESSION = arena.RUNS / "_selftest_session.json"
    arena.SESSION.unlink(missing_ok=True)

    st = arena.new_state(
        cases, run_file,
        arena.Endpoint("A-base", url, "model-A", timeout=10),
        arena.Endpoint("B-doctor", url, "model-B", timeout=10),
        prof,
    )
    arena.save_session(st)

    # ② 生成：第一题（多轮用例）
    st["idx"] = 0
    out = arena.do_generate_one(st)
    st = out[0]
    gen = st["gen"]["0"]
    assert gen["A"]["replies"][0]["text"] == "model-A 的回复", gen["A"]
    assert gen["B"]["replies"][0]["text"] == "model-B 的回复", gen["B"]

    # ③ 渲染：左右顺序必须由骰子决定，且界面里不出现模型名
    flip = st["flip"][0]
    out = arena.render(st)
    left_txt, right_txt = out[4], out[5]
    assert st["A"] not in left_txt + right_txt, "盲测泄漏：界面出现了模型名"
    if flip:
        assert left_txt.startswith("患者：") and "model-B" in left_txt, left_txt
        assert "model-A" in right_txt, right_txt
    else:
        assert "model-A" in left_txt, left_txt
        assert "model-B" in right_txt, right_txt

    # ④ 打分：左侧 5/4/3/2，右侧 1/1/1/1，判左侧胜
    out = arena.do_submit(st, left_txt, right_txt,
                          *_flat([5, 4, 3, 2], [1, 1, 1, 1]), "L", "左侧更专业")
    st = out[0]
    rec = st["results"][-1]
    left_side = "B" if flip else "A"
    right_side = "A" if flip else "B"
    assert rec["winner"] == left_side, (rec["winner"], left_side)
    assert rec[left_side]["score"] == {"acc": 5, "rel": 4, "safe": 3, "tone": 2}, rec
    assert rec[right_side]["score"] == {"acc": 1, "rel": 1, "safe": 1, "tone": 1}, rec
    assert rec[left_side]["label"] in ("A-base", "B-doctor")

    # ⑤ 落盘：jsonl 一行一条，字段完整
    lines = run_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1, lines
    disk = json.loads(lines[0])
    assert disk["case_id"] == cases[0]["id"]
    assert disk["winner"] == left_side
    assert disk["flipped"] is flip
    assert disk["task"] == "previsit", disk.get("task")
    assert disk["A"]["ms"] >= 0 and disk["B"]["ms"] >= 0

    # ⑥ 统计与 CSV
    md = arena.stats_markdown(st)
    assert "已评 **1**" in md, md
    assert "胜负" in md
    csv_path = arena.write_csv(st)
    assert csv_path and Path(csv_path).exists()
    head = Path(csv_path).read_text(encoding="utf-8-sig").splitlines()[0]
    assert "a_acc" in head and "b_acc" in head, head

    # ⑦ 跳过、回退、跳转：回到已评题要把分数读回来
    st["idx"] = 1
    out = arena.do_skip(st)
    assert out[0]["idx"] == 2
    out = arena.do_back(out[0])
    assert out[0]["idx"] == 1
    out = arena.do_back(out[0])
    assert out[0]["idx"] == 0
    L0, R0 = arena.ORDER.index("l0"), arena.ORDER.index("r0")
    assert out[L0] == 5 and out[R0] == 1, (out[L0], out[R0])

    out = arena.do_goto(out[0], 1)
    assert out[0]["idx"] == 0
    assert out[L0] == 5 and out[R0] == 1, (out[L0], out[R0])
    assert out[arena.ORDER.index("winner")] == "L"

    # ⑧ 题目下拉：评过的打 ✓
    labels = [lab for lab, _ in arena.case_choices(st)]
    assert labels[0].startswith("✓"), labels[:2]
    assert labels[1].startswith("\u3000"), labels[:2]
    assert len(labels) == 20

    # ⑨ 通用档案：维度、角色名、题库全换，落盘结构必须跟着换
    gprof = profiles.get("general")
    gcases = load_cases(gprof["cases"], gprof["rules"])
    assert len(gcases) >= 10, len(gcases)
    gfile = arena.RUNS / "_selftest_general.jsonl"
    gfile.unlink(missing_ok=True)
    gst = arena.new_state(gcases, gfile,
                          arena.Endpoint("A", url, "model-A", timeout=10),
                          arena.Endpoint("B", url, "model-B", timeout=10), gprof)
    gout = arena.do_generate_one(gst)
    gst = gout[0]
    gtext = gout[4]
    assert gtext.startswith("用户："), gtext[:40]
    assert "助手：" in gtext, gtext[:80]
    gout = arena.do_submit(gst, "", "", *_flat([5, 4, 3, 2], [1, 2, 3, 4]), "L", "")
    grec = gout[0]["results"][-1]
    assert set(grec["A"]["score"]) == {"acc", "full", "usable", "clear"}, grec["A"]["score"]
    assert grec["task"] == "general", grec["task"]
    lhead = Path(arena.write_csv(gout[0])).read_text(encoding="utf-8-sig").splitlines()[0]
    assert "a_full" in lhead and "a_usable" in lhead, lhead
    # 规则体检：g01 要求出现 8848，这里故意给一句空话
    g01 = next(c for c in gcases if c["id"] == "g01")
    assert profiles.violations(g01, "不知道") == ["题面检查"], profiles.violations(g01, "不知道")
    assert profiles.violations(g01, "8848.86 米") == []

    # ⑩ 失败端点不能炸页面
    bad = arena.Endpoint("bad", "http://127.0.0.1:9/v1", "x", timeout=2)
    res = arena.run_dialog(bad, ["你好"], "")
    assert res["error"], res
    assert res["replies"][0]["text"].startswith("⚠️"), res

    # ⑪ 后端配置读得出来、填得回去
    #
    # 这一条防的是"界面填上了，但填的是别的东西"：endpoint 的地址/模型来自
    # 下拉展开值，一旦 key 拼错（后端名里带 :: 之类），下拉选中显示正常，
    # 请求却打到旧端点——分数看着有，评的不是那个模型。
    bds = backends.entries()
    for ep in bds:
        assert ep["base_url"].startswith(("http://", "https://")), ep
        assert ep["model"], ep
        got = backends.fill(f"{ep['name']}::{ep['model']}")
        assert got, ep["name"]
        assert got["base_url"] == ep["base_url"] and got["model"] == ep["model"], got
    assert backends.fill("") is None
    assert backends.fill("不存在的后端::x") is None
    print(f"  后端配置：{len(bds)} 个（{backends.summary()}）")

    # ⑫ 题库三来源：手输 / 导入文件 / 档案自带
    #
    # 三条路必须收敛成同一个形状。差一个字段，规则体检就会在某条路上静默失效——
    # 分数照出、报告照导，只是那一批分从来没被检查过。
    tmp = arena.RUNS / "_selftest_cases"
    tmp.mkdir(exist_ok=True)
    old_custom = casebook.CUSTOM
    casebook.CUSTOM = tmp / "custom.jsonl"

    hand = casebook.parse_text(
        "# 事实\n珠峰海拔？ @必须:8848\n\n"
        "我在用 FastAPI。 || 它怎么限流？ @必须:限流,Redis @备注:看上下文\n",
        prof["rules"])
    assert len(hand) == 2, hand
    assert hand[0]["turns"] == ["珠峰海拔？"], hand[0]
    assert hand[0]["tag"] == "事实" and hand[0]["must_any"] == ["8848"], hand[0]
    assert hand[1]["turns"] == ["我在用 FastAPI。", "它怎么限流？"], hand[1]
    assert hand[1]["note"] == "看上下文", hand[1]
    assert {"id", "turns", "checks", "must_any", "must_not"} <= set(hand[0]), hand[0]

    (tmp / "q.csv").write_text(
        "题目,标签,必须,备注\n北京的首都是哪里,事实,北京,故意写反\n一加一等于几,,2,\n",
        encoding="utf-8-sig")
    csv_cases = casebook.parse_upload(tmp / "q.csv", prof["rules"])
    assert len(csv_cases) == 2, csv_cases
    assert csv_cases[0]["turns"] == ["北京的首都是哪里"], csv_cases[0]
    assert csv_cases[0]["must_any"] == ["北京"] and csv_cases[0]["tag"] == "事实"
    # 体检项得真的挂上：条数对了但 checks 是空的，等于这批分从没被检查过
    assert "题面检查" in profiles.violations(csv_cases[0], "不知道"), csv_cases[0]["checks"]
    assert "题面检查" not in profiles.violations(csv_cases[0], "北京"), csv_cases[0]["checks"]
    assert "题面检查" in profiles.violations(hand[0], "随便答"), hand[0]["checks"]

    (tmp / "q.txt").write_text("你好\n世界 || 再说一遍\n", encoding="utf-8")
    txt_cases = casebook.parse_upload(tmp / "q.txt", prof["rules"])
    assert [c["turns"] for c in txt_cases] == [["你好"], ["世界", "再说一遍"]], txt_cases

    # 往返：抄进输入框再粘回来，题目一条都不能少
    assert [c["turns"] for c in casebook.parse_text(casebook.dump_text(cases))] \
        == [c["turns"] for c in cases]

    resolved = casebook.resolve("自己输入", "", None, "第一题\n第二题", prof["rules"])
    assert len(resolved) == 2, resolved
    assert casebook.CUSTOM.exists(), "手输的题没落盘"
    assert len(casebook.parse_text(casebook.load_custom_text())) == 2, "回填丢题了"

    for blank in ("", "  \n\t "):
        try:
            casebook.parse_text(blank)
        except ValueError:
            pass
        else:
            raise AssertionError("空题库没报错")
    try:
        casebook.resolve("导入文件", "", None, "", prof["rules"])
    except ValueError as exc:
        assert "文件" in str(exc), exc
    else:
        raise AssertionError("没选文件也没报错")

    # 页签选中值是 label 字符串，但换版本也可能给索引；认不出来必须退回安全默认
    assert casebook.normalize_source("导入文件") == "导入文件"
    assert casebook.normalize_source(2) == "自己输入"
    assert casebook.normalize_source(None) == "档案自带"
    assert casebook.normalize_source("乱七八糟") == "档案自带"

    # 存档（存为档案）时题库要跟着走，不能只存一句指向旧文件的路径
    named = casebook.save_named(hand, "_selftest")
    assert (casebook.ROOT / named).exists(), named
    assert len(load_cases(named, prof["rules"])) == 2, named
    (casebook.ROOT / named).unlink()

    casebook.CUSTOM = old_custom
    shutil.rmtree(tmp, ignore_errors=True)

    srv.shutdown()
    for p in (run_file, arena.SESSION, gfile):
        p.unlink(missing_ok=True)
    print("自检全过：生成 / 盲测归属 / 动态维度 / 档案切换 / 规则体检 / 落盘 / 统计 / "
          "容错 / 后端配置 / 题库三来源")
    return 0


def rec_checks(cases: list[dict]) -> int:
    return sum(1 for c in cases if c.get("checks"))


if __name__ == "__main__":
    raise SystemExit(main())
