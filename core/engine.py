# -*- coding: utf-8 -*-
"""整条链的唯一入口：对话 → Jev 判断 → 带着判断起草 3 条 → Jev 排序 → 结构化结果。

平台无关。SSE 消费者、悬浮窗、命令行 demo 都只调 analyze()。
"""
from __future__ import annotations

import time

try:
    from .draft import draft_candidates
    from .jev_client import JevError, ask
    from .questions import JUDGE_QUESTIONS, build_rank_question, build_state, guidance_text
except ImportError:
    from draft import draft_candidates
    from jev_client import JevError, ask
    from questions import JUDGE_QUESTIONS, build_rank_question, build_state, guidance_text

_REPLY_IDX = {"reply_a": 0, "reply_b": 1, "reply_c": 2}


def _fail_reason(exc: Exception) -> str:
    """JevError → 可入日志的安全分类：只留错误类型/HTTP 状态码，**绝不透传异常原文**
    （原文可能混着对话或模型输出，KICKOFF 的不落日志约束）。判断/排序失败的原因行走这里。"""
    status = getattr(exc, "status", None)
    if status:
        return {401: "密钥被拒", 403: "没有权限", 404: "模型或地址不对", 422: "请求被拒",
                429: "被限流", 529: "服务过载"}.get(status, f"HTTP {status}")
    msg = str(exc)
    lower = msg.lower()
    if "JSON" in msg or "解析不出" in msg:
        return "输出解析失败"
    if "timed out" in lower or "timeout" in lower or "超时" in msg:
        return "超时"
    if "connection" in lower or "request failed" in lower or "网络" in msg:
        return "网络错误"
    if "Base URL" in msg:
        return "缺 Base URL"
    if "not set" in lower or "密钥" in msg:
        return "缺密钥"
    return "未知错误"


def _add_usage(total: dict, one: dict | None) -> None:
    """两次 Jev 调用的 usage 相加（tokens、cost）；非数字的字段后来的盖掉前面的。"""
    for k, v in (one or {}).items():
        total[k] = total.get(k, 0) + v if isinstance(v, (int, float)) else v


def analyze(messages: list, relationship: str, model: str | None = None,
            timeout: float = 30, context: int = 10, provider: str = "deepseek",
            base_url: str | None = None, reply_to: str | None = None, style: str = "",
            thinking: bool = False, jev_provider: str = "openrouter",
            jev_model: str | None = None, jev_base_url: str | None = None,
            on_stage=None, self_rank: bool = False) -> dict:
    """messages: [(from, text)] from ∈ {her, me}，最新一条在最后；
    群聊里可以带第三项 name（说这句话的人），单聊不带。
    context: 起草和判断各看最近多少条消息（用户设置里的「参考上下文」）。
    provider: 起草走哪家（core.providers.DRAFT_PROVIDERS），base_url 只有自定义来源要传。
    jev_provider / jev_model: 判断和排序走哪家、哪个模型（core.providers.JEV_PROVIDERS）。
    jev_base_url: 判断的自定义 OpenAI 兼容来源要传的地址，其余来源不用。
    reply_to: 群聊里指定回复给谁；None = 正常回复。
    style: 用户自己描述的说话风格，只影响起草。
    thinking: 起草时是否开思考模式，只影响起草，默认关。
    model / jev_model = None 用该来源的默认模型。
    on_stage: 可选的进度回调，在调用方线程里同步执行
        on_stage(name, phase, seconds=None, ok=True, reason=None)，
        name ∈ {judge, draft, rank}、phase ∈ {start, end}；end 带这段的耗时（秒），
        ok=False 表示这段失败（判断失败会走盲起草的降级，排序失败按第一条推荐），
        reason 是 _fail_reason 的安全分类（如「超时」「HTTP 429」「输出解析失败」），
        只有类型和状态码、绝不含对话或模型输出。
        回调里抛异常会被吞掉——进度提示是旁路，不能拖垮分析本身。
    self_rank: 方案②——起草时自评胜出概率（设置里的「排序 · 起草自评」），跳过独立排序调用，
        稳定省一次往返；自评概率不可用（模型没给/给不全）时自动退回独立排序。
        判断失败时仍会补问一次判断题（给判断摘要用），那一轮不带排序题。

    返回 {candidates, best_index, best_reply, scores, answers, usage, reply_to, stages}。
    scores 是每条候选的胜出概率（0~1）：独立排序取自 best_reply.probabilities，
    自评模式取自起草返回的概率；取不到记 0.0。
    stages 是按发生顺序的分段耗时 [{name, seconds, ok}, …]，给界面显示「哪段慢」用的。
    只有对方最新说话时才有意义调它——是不是该触发由调用方判断（看 latest_from）。

    三段式（issue #4）：先让 Jev 答 7 道判断题，把判断当小抄喂给起草，最后排序。
    判断那次挂了就退回老路：盲起草 + 判断和排序一次问完，行为跟以前一样（self_rank
    开着时合问只带判断题，排序由起草自评）。usage 是各次调用之和。
    """
    state = build_state(messages, relationship, keep=context, reply_to=reply_to)
    usage: dict = {}
    answers: dict = {}
    stages: list = []

    def _emit(name, phase, seconds=None, ok=True, reason=None):
        if on_stage:
            try:
                on_stage(name, phase, seconds, ok, reason)
            except Exception:
                pass  # 进度回调是外部代码，它挂了分析照跑

    def _finish(name, t0, ok=True, reason: str | None = None) -> float:
        seconds = round(time.perf_counter() - t0, 1)
        entry = {"name": name, "seconds": seconds, "ok": ok}
        if not ok and reason:
            entry["reason"] = reason
        stages.append(entry)
        _emit(name, "end", seconds, ok, reason)
        return seconds

    judged = False
    fail_reason = None
    _emit("judge", "start")
    t0 = time.perf_counter()
    try:
        first = ask(state, dict(JUDGE_QUESTIONS), timeout=timeout,
                    provider=jev_provider, model=jev_model, base_url=jev_base_url)
        answers = first.get("answers") or {}
        _add_usage(usage, first.get("usage"))
        judged = True
    except JevError as exc:
        fail_reason = _fail_reason(exc)  # 只留分类；原文可能带内容，绝不打日志（下面同理）
    _finish("judge", t0, ok=judged, reason=fail_reason)

    _emit("draft", "start")
    t0 = time.perf_counter()
    candidates, draft_scores = draft_candidates(
        messages, relationship, provider=provider, model=model,
        base_url=base_url, timeout=timeout, keep=context,
        reply_to=reply_to, style=style, thinking=thinking,
        guidance=guidance_text(answers) if judged else None, self_rank=self_rank)
    _finish("draft", t0)
    if not candidates:  # 注入过滤可以把起草结果全扔掉；接着取 [0] 会 IndexError
        raise JevError("起草结果没有可用候选回复")
    # ②自评可用 = 开关开着 + 概率齐全对齐 + 至少两条（一条没什么可排的）
    ranked_by_draft = (self_rank and draft_scores is not None
                       and len(draft_scores) == len(candidates) and len(candidates) >= 2)

    questions = {} if judged else dict(JUDGE_QUESTIONS)
    if len(candidates) >= 2 and not ranked_by_draft:
        # 起草只给了 1 条就没什么可排的；自评可用就不再发独立排序题
        questions.update(build_rank_question(candidates))
    if questions:
        stage = "rank" if "best_reply" in questions else "judge"  # 自评模式下这轮只补判断题
        _emit(stage, "start")
        t0 = time.perf_counter()
        rank_ok, rank_reason = True, None
        try:
            second = ask(state, questions, timeout=timeout,
                         provider=jev_provider, model=jev_model, base_url=jev_base_url)
        except JevError as exc:
            if not judged:  # 这轮是判断失败后的补问/老路合问，挂了就是挂了
                raise
            second = {}  # 判断还在，只是没排上序：下面按第一条推荐
            rank_ok, rank_reason = False, _fail_reason(exc)
        answers = {**answers, **(second.get("answers") or {})}
        _add_usage(usage, second.get("usage"))
        _finish(stage, t0, ok=rank_ok, reason=rank_reason)

    if ranked_by_draft:
        scores = [0.0, 0.0, 0.0]
        for i, s in enumerate(draft_scores):
            if i < 3:
                scores[i] = float(s)
        # 自评的推荐 = 概率最高；同分时靠前的赢（界面还会按概率再排一次序）
        best_index = max(range(len(candidates)), key=lambda i: (scores[i], -i))
    else:
        best_key = (answers.get("best_reply") or {}).get("choice")
        best_index = _REPLY_IDX.get(best_key, 0)  # 解析不出就退第一条
        if best_index >= len(candidates):
            best_index = 0

        probabilities = (answers.get("best_reply") or {}).get("probabilities") or {}
        scores = [0.0, 0.0, 0.0]
        for key, idx in _REPLY_IDX.items():
            try:
                scores[idx] = float(probabilities.get(key, 0.0))
            except (TypeError, ValueError):
                scores[idx] = 0.0  # 脏数据一律按 0 处理

    return {
        "candidates": candidates,
        "best_index": best_index,
        "best_reply": candidates[best_index],
        "scores": scores,
        "answers": answers,
        "usage": usage,
        "reply_to": reply_to,
        "stages": stages,
    }


if __name__ == "__main__":
    # 候选被过滤光时要抛 JevError，不能在取第一条时 IndexError。
    from unittest.mock import Mock, patch

    with patch("__main__.ask", return_value={"answers": {}, "usage": {}}), \
         patch("__main__.draft_candidates", return_value=([], None)):
        try:
            analyze([("her", "hello")], "friends")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "没有可用候选" in str(e)

    # 失败原因的安全分类：只留类型/状态码，异常原文（可能含对话/模型输出）绝不透传
    assert _fail_reason(JevError("x", status=429)) == "被限流"
    assert _fail_reason(JevError("Jev HTTP 401: bad", status=401)) == "密钥被拒"
    assert _fail_reason(JevError("Jev HTTP 500: boom", status=500)) == "HTTP 500"
    assert _fail_reason(JevError("判断输出不是合法 JSON：Expecting...")) == "输出解析失败"
    assert _fail_reason(JevError("Jev request timed out after 30s")) == "超时"
    assert _fail_reason(JevError("Connection error occurred")) == "网络错误"
    assert _fail_reason(JevError("自定义判断来源没填 Base URL")) == "缺 Base URL"
    assert _fail_reason(JevError("对方发来的秘密内容混在报错里")) == "未知错误"  # 不认识就闭嘴

    # 分段进度与耗时（独立排序路径）：事件按 judge → draft → rank 的 start/end 成对出现，
    # stages 跟事件对得上；回调里抛异常也不能拖垮分析本身。
    rank_result = {"answers": {"best_reply": {
        "type": "choice", "choice": "reply_b",
        "probabilities": {"reply_a": 0.5, "reply_b": 0.3, "reply_c": 0.2}}},
        "usage": {"input_tokens": 3}}
    events = []

    def _boom(*_a):
        raise RuntimeError("回调坏了")

    with patch("__main__.ask", return_value=rank_result), \
         patch("__main__.draft_candidates", return_value=(["甲", "乙", "丙"], None)):
        r = analyze([("her", "hello")], "friends", on_stage=lambda *a: events.append(a))
        assert [e[:2] for e in events] == [
            ("judge", "start"), ("judge", "end"), ("draft", "start"),
            ("draft", "end"), ("rank", "start"), ("rank", "end")]
        assert all(e[3] is True for e in events if e[1] == "end")
        assert [s["name"] for s in r["stages"]] == ["judge", "draft", "rank"]
        assert all(isinstance(s["seconds"], (int, float)) and s["ok"]
                   for s in r["stages"])
        ends = {e[0]: e[2] for e in events if e[1] == "end"}
        assert all(ends[s["name"]] == s["seconds"] for s in r["stages"])
        # 只给 1 条候选：没有排序那段，事件也不该有 rank
        events.clear()
        with patch("__main__.draft_candidates", return_value=(["只有一条"], None)):
            r1 = analyze([("her", "hello")], "friends", on_stage=lambda *a: events.append(a))
        assert [e[0] for e in events if e[1] == "start"] == ["judge", "draft"]
        assert [s["name"] for s in r1["stages"]] == ["judge", "draft"]
        # 回调抛异常：吞掉，结果照常
        analyze([("her", "hello")], "friends", on_stage=_boom)

    # 独立排序的答案确实被消费：choice=reply_b → best_index 1，概率按 key 取
    assert r["best_index"] == 1 and r["scores"] == [0.5, 0.3, 0.2]

    # ② 自评可用：只发一次 ask（judge），没有 rank 段；分数与推荐全来自起草
    ask_mock = Mock(return_value={"answers": {}, "usage": {}})
    with patch("__main__.ask", ask_mock), \
         patch("__main__.draft_candidates",
               return_value=(["甲", "乙", "丙"], [0.2, 0.6, 0.2])):
        r2 = analyze([("her", "hello")], "friends", self_rank=True)
    assert ask_mock.call_count == 1
    assert r2["best_index"] == 1 and r2["scores"] == [0.2, 0.6, 0.2]
    assert [s["name"] for s in r2["stages"]] == ["judge", "draft"]

    # ② 但模型没给概率 → 自动退回独立排序（ask 两次，第二次带 best_reply）
    ask_mock = Mock(side_effect=[{"answers": {}, "usage": {}}, rank_result])
    with patch("__main__.ask", ask_mock), \
         patch("__main__.draft_candidates", return_value=(["甲", "乙", "丙"], None)):
        r3 = analyze([("her", "hello")], "friends", self_rank=True)
    assert ask_mock.call_count == 2
    assert "best_reply" in ask_mock.call_args_list[1].args[1]
    assert r3["scores"] == [0.5, 0.3, 0.2]

    # ② + 判断失败：补问一轮只带 7 道判断题（没有 best_reply），分数仍来自自评；
    # 失败原因（安全分类）同时进 stages[0].reason 和 on_stage 的第 5 个参数
    judge7 = {"answers": {"true_intent": {"type": "choice", "choice": "casual_chat"}},
              "usage": {}}
    ask_mock = Mock(side_effect=[JevError("Jev request timed out after 30s"), judge7])
    events = []
    with patch("__main__.ask", ask_mock), \
         patch("__main__.draft_candidates",
               return_value=(["甲", "乙", "丙"], [0.5, 0.3, 0.2])):
        r4 = analyze([("her", "hello")], "friends", self_rank=True,
                     on_stage=lambda *a: events.append(a))
    assert ask_mock.call_count == 2
    second_questions = ask_mock.call_args_list[1].args[1]
    assert "best_reply" not in second_questions
    assert set(JUDGE_QUESTIONS) <= set(second_questions)  # 7 道判断题都在，给摘要用
    assert r4["answers"]["true_intent"]["choice"] == "casual_chat"  # 摘要没断
    assert r4["scores"] == [0.5, 0.3, 0.2]
    assert r4["stages"][0]["reason"] == "超时"  # 分类进了 stages
    assert [(e[0], e[1]) for e in events] == [
        ("judge", "start"), ("judge", "end"), ("draft", "start"),
        ("draft", "end"), ("judge", "start"), ("judge", "end")]
    assert events[1][3] is False and events[1][4] == "超时"  # end 事件带 reason
    assert events[5][3] is True

    print("engine ok")
