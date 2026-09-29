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


def _add_usage(total: dict, one: dict | None) -> None:
    """两次 Jev 调用的 usage 相加（tokens、cost）；非数字的字段后来的盖掉前面的。"""
    for k, v in (one or {}).items():
        total[k] = total.get(k, 0) + v if isinstance(v, (int, float)) else v


def analyze(messages: list, relationship: str, model: str | None = None,
            timeout: float = 30, context: int = 10, provider: str = "deepseek",
            base_url: str | None = None, reply_to: str | None = None, style: str = "",
            thinking: bool = False, jev_provider: str = "openrouter",
            jev_model: str | None = None, jev_base_url: str | None = None,
            on_stage=None) -> dict:
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
        on_stage(name, phase, seconds=None, ok=True)，
        name ∈ {judge, draft, rank}、phase ∈ {start, end}；end 带这段的耗时（秒），
        ok=False 表示这段失败（判断失败会走盲起草的降级，排序失败按第一条推荐）。
        回调里抛异常会被吞掉——进度提示是旁路，不能拖垮分析本身。

    返回 {candidates, best_index, best_reply, scores, answers, usage, reply_to, stages}。
    scores 是每条候选的胜出概率（0~1），取自 best_reply.probabilities，取不到记 0.0。
    stages 是按发生顺序的分段耗时 [{name, seconds, ok}, …]，给界面显示「哪段慢」用的。
    只有对方最新说话时才有意义调它——是不是该触发由调用方判断（看 latest_from）。

    三段式（issue #4）：先让 Jev 答 7 道判断题，把判断当小抄喂给起草，最后 Jev 只排序。
    判断那次挂了就退回老路：盲起草 + 判断和排序一次问完，行为跟以前一样。usage 是两次之和。
    """
    state = build_state(messages, relationship, keep=context, reply_to=reply_to)
    usage: dict = {}
    answers: dict = {}
    stages: list = []

    def _emit(name, phase, seconds=None, ok=True):
        if on_stage:
            try:
                on_stage(name, phase, seconds, ok)
            except Exception:
                pass  # 进度回调是外部代码，它挂了分析照跑

    def _finish(name, t0, ok=True) -> float:
        seconds = round(time.perf_counter() - t0, 1)
        stages.append({"name": name, "seconds": seconds, "ok": ok})
        _emit(name, "end", seconds, ok)
        return seconds

    judged = False
    _emit("judge", "start")
    t0 = time.perf_counter()
    try:
        first = ask(state, dict(JUDGE_QUESTIONS), timeout=timeout,
                    provider=jev_provider, model=jev_model, base_url=jev_base_url)
        answers = first.get("answers") or {}
        _add_usage(usage, first.get("usage"))
        judged = True
    except JevError:
        pass  # 退回盲起草 + 老的一次合问；错误不打日志（里面可能带请求内容）
    _finish("judge", t0, ok=judged)

    _emit("draft", "start")
    t0 = time.perf_counter()
    candidates = draft_candidates(messages, relationship, provider=provider, model=model,
                                  base_url=base_url, timeout=timeout, keep=context,
                                  reply_to=reply_to, style=style, thinking=thinking,
                                  guidance=guidance_text(answers) if judged else None)
    _finish("draft", t0)
    if not candidates:  # 注入过滤可以把起草结果全扔掉；接着取 [0] 会 IndexError
        raise JevError("起草结果没有可用候选回复")

    questions = {} if judged else dict(JUDGE_QUESTIONS)
    if len(candidates) >= 2:  # 起草只给了 1 条就没什么可排的，判断题照问
        questions.update(build_rank_question(candidates))
    if questions:
        _emit("rank", "start")
        t0 = time.perf_counter()
        rank_ok = True
        try:
            second = ask(state, questions, timeout=timeout,
                         provider=jev_provider, model=jev_model, base_url=jev_base_url)
        except JevError:
            if not judged:  # 老路只有这一次调用，挂了就是挂了
                raise
            second = {}  # 判断还在，只是没排上序：下面按第一条推荐
            rank_ok = False
        answers = {**answers, **(second.get("answers") or {})}
        _add_usage(usage, second.get("usage"))
        _finish("rank", t0, ok=rank_ok)

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
    from unittest.mock import patch

    with patch("__main__.ask", return_value={"answers": {}, "usage": {}}), \
         patch("__main__.draft_candidates", return_value=[]):
        try:
            analyze([("her", "hello")], "friends")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "没有可用候选" in str(e)

    # 分段进度与耗时：事件按 judge → draft → rank 的 start/end 成对出现，
    # stages 跟事件对得上；回调里抛异常也不能拖垮分析本身。
    rank_result = {"answers": {"best_reply": {
        "type": "choice", "choice": "reply_a",
        "probabilities": {"reply_a": 0.5, "reply_b": 0.3, "reply_c": 0.2}}},
        "usage": {"input_tokens": 3}}
    events = []

    def _boom(*_a):
        raise RuntimeError("回调坏了")

    with patch("__main__.ask", return_value=rank_result), \
         patch("__main__.draft_candidates", return_value=["甲", "乙", "丙"]):
        r = analyze([("her", "hello")], "friends", on_stage=lambda *a: events.append(a))
        assert [e[:2] for e in events] == [
            ("judge", "start"), ("judge", "end"), ("draft", "start"),
            ("draft", "end"), ("rank", "start"), ("rank", "end")]
        assert all(e[3] is True for e in events if e[1] == "end")
        assert [s["name"] for s in r["stages"]] == ["judge", "draft", "rank"]
        assert all(isinstance(s["seconds"], (int, float)) and s["ok"]
                   for s in r["stages"])
        # end 事件里的秒数和 stages 里是同一份
        ends = {e[0]: e[2] for e in events if e[1] == "end"}
        assert all(ends[s["name"]] == s["seconds"] for s in r["stages"])
        # 只给 1 条候选：没有排序那段，事件也不该有 rank
        events.clear()
        with patch("__main__.draft_candidates", return_value=["只有一条"]):
            r1 = analyze([("her", "hello")], "friends", on_stage=lambda *a: events.append(a))
        assert [e[0] for e in events if e[1] == "start"] == ["judge", "draft"]
        assert [s["name"] for s in r1["stages"]] == ["judge", "draft"]
        # 回调抛异常：吞掉，结果照常
        analyze([("her", "hello")], "friends", on_stage=_boom)

    print("engine ok")
