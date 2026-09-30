# -*- coding: utf-8 -*-
"""Jev 判断 API 客户端：OpenRouter / TypeSafe 直连 / 小米 MiMo。

TypeSafe 直连走官方 `typesafe_sdk`；OpenRouter 这条是唯一自己拼 HTTP 的路——
SDK 把路径写死成 `/v1/systemone`，打不到 OpenRouter 的 `/api/alpha/decisions`。
小米 MiMo 是普通 chat 接口（OpenAI 兼容）：判断题序列化成 prompt、要求只回 JSON，
再解析回前两条路同款形状（见 _ask_chat）。三条路返回同一个 dict 形状，
engine 不关心跑的是哪条。key 只从环境变量读，绝不打进日志。
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from typing import NoReturn

try:  # 当模块导入 / 当脚本直接跑 都能用
    from .providers import (ENV_VARS, JEV_ENV, JEV_PROVIDERS, LEGACY, MIMO_BASE,
                            OPENROUTER_DECISIONS, OPENROUTER_KEY_URL, TYPESAFE_BASE)
except ImportError:
    from providers import (ENV_VARS, JEV_ENV, JEV_PROVIDERS, LEGACY, MIMO_BASE,
                           OPENROUTER_DECISIONS, OPENROUTER_KEY_URL, TYPESAFE_BASE)

MAX_RETRIES = 3


class JevError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def redact_secrets(text: str) -> str:
    """Strip every live key from any string before print or disk write."""
    if not isinstance(text, str):
        text = str(text)
    for env in ENV_VARS:
        key = os.environ.get(env) or ""
        if key:
            text = text.replace(key, "[REDACTED]")
    return text


def _status_of(exc: Exception) -> int | None:
    """各家 SDK 放 HTTP 状态码的属性名不一样：openai/anthropic 是 status_code，
    google-genai 是 code（它的 status 是 'NOT_FOUND' 这种字符串），typesafe 是 status。"""
    for name in ("status_code", "code", "status"):
        value = getattr(exc, name, None)
        if isinstance(value, int):
            return value
    return None


def _fail(exc: Exception, what: str) -> NoReturn:
    """SDK 抛的异常 → 一句人话的 JevError。消息过脱敏，绝不把 key 带出来。"""
    if isinstance(exc, JevError):
        raise exc
    status = _status_of(exc)
    hint = {401: "密钥被拒", 403: "没有权限", 404: "模型或地址不对", 422: "请求被拒",
            429: "被限流", 529: "服务过载"}.get(status, "")
    detail = redact_secrets(str(exc)).strip()[:300]
    head = f"{what} HTTP {status}" if status else f"{what}失败"
    raise JevError(f"{head}: {hint or detail or type(exc).__name__}", status) from None


def _api_key(env: str = JEV_ENV) -> str:
    """两把 key 之一（JEV_API_KEY / LLM_API_KEY）。新名字空着就退回老名字，老用户不用重填。"""
    key = ((os.environ.get(env) or "").strip()
           or (os.environ.get(LEGACY.get(env, "")) or "").strip())
    if not key:
        raise JevError(
            f"{env} is not set. Export it in the environment; "
            "do not put the key in a file."
        )
    return key


def _error_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:
        raw = ""
    return redact_secrets(raw)[:800]


def ask(state: dict, questions: dict, timeout: float = 20,
        provider: str = "openrouter", model: str | None = None,
        base_url: str | None = None) -> dict:
    """问 Jev 一轮判断，返回 {"answers": {名字: 答案}, "usage": {...}}。

    provider ∈ JEV_PROVIDERS（openrouter / typesafe 直连 / 小米 MiMo / 自定义 OpenAI 兼容）；
    model=None 用该来源的默认模型；base_url 只有「自定义 · OpenAI 兼容」要传。
    三条路返回的 dict 形状一模一样，429/529 都会退避重试。绝不打印或写出 key。
    """
    spec = JEV_PROVIDERS.get(provider) or JEV_PROVIDERS["openrouter"]
    key = _api_key(JEV_ENV)  # 各家共用同一把 key，换来源不用重填
    model = model or spec.default
    if spec.protocol:  # 普通 chat 接口（小米 MiMo / 自定义）：prompt + JSON 桥接
        base = base_url or spec.base
        if not base:  # 自定义没填地址。别让 SDK 悄悄退回 api.openai.com 打错门
            raise JevError("自定义判断来源没填 Base URL")
        return _ask_chat(state, questions, key, model, base, timeout)
    if provider == "typesafe":
        return _ask_typesafe(state, questions, key, model, timeout)
    return _ask_openrouter(state, questions, key, model, timeout)


def _answer(answer) -> dict:
    """SDK 的答案对象 → OpenRouter 那条路 JSON 出来的同一个形状。"""
    if answer.type == "noul":
        return {"type": "noul", "noul": answer.noul}
    if answer.type == "choice":
        return {"type": "choice", "choice": answer.choice, "confidence": answer.confidence,
                "probabilities": dict(answer.probabilities)}
    # score：SDK 把概率的 key 转成了 int，这里转回字符串，跟 JSON 那条路对齐
    return {"type": "score", "score": answer.score, "confidence": answer.confidence,
            "probabilities": {str(k): v for k, v in answer.probabilities.items()}}


def _ask_typesafe(state: dict, questions: dict, key: str, model: str, timeout: float) -> dict:
    """官方 typesafe_sdk。questions 原样传：core/questions.py 里那几个 dict 本身就是 SDK 的
    NoulModel / ChoiceModel / ScoreModel（SDK 的 normalize_questions 认 dict），不用再包一层对象。
    重试用 RetryPolicy 的默认值——它本来就重试 408/429/5xx（含 529）并退避。"""
    import typesafe_sdk

    try:
        with typesafe_sdk.TypeSafeClient(api_key=key, base_url=TYPESAFE_BASE, model=model,
                                         timeout=timeout) as client:
            result = client.system_one(state, questions, model=model)
    except Exception as exc:
        _fail(exc, "Jev 判断")
    return {
        "answers": {name: _answer(a) for name, a in result.answers.items()},
        "usage": {"input_tokens": result.usage.input_tokens,
                  "output_tokens": result.usage.output_tokens},
    }


def _ask_openrouter(state: dict, questions: dict, key: str, model: str, timeout: float) -> dict:
    """OpenRouter 的 /api/alpha/decisions，手写 urllib。429/529 退避重试 3 次。"""
    payload = json.dumps(
        {"model": model, "state": state, "questions": questions},
        ensure_ascii=False,
    ).encode("utf-8")

    last_status: int | None = None
    last_body = ""
    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(
            OPENROUTER_DECISIONS,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            last_body = _error_body(exc)
            if last_status in (429, 529) and attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            readable = {
                401: f"Jev HTTP 401: API key rejected. Check {JEV_ENV}.",
                422: f"Jev HTTP 422: request body rejected. {last_body}",
                429: f"Jev HTTP 429: rate limited after {MAX_RETRIES} retries. {last_body}",
                529: f"Jev HTTP 529: provider overloaded after {MAX_RETRIES} retries. {last_body}",
            }.get(last_status, f"Jev HTTP {last_status}: {last_body}")
            raise JevError(readable, last_status) from None
        except (TimeoutError, socket.timeout) as exc:
            if attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            raise JevError(f"Jev request timed out after {timeout}s") from exc
        except urllib.error.URLError as exc:
            reason = redact_secrets(getattr(exc, "reason", exc))
            if attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            raise JevError(f"Jev request failed: {reason}") from None

    raise JevError(
        f"Jev HTTP {last_status}: exhausted retries. {last_body}", last_status
    )


# 小米 MiMo 桥接的固定 system。题目本身（instructions/criteria）随 user 消息进来，这里不重复。
# 输出契约刻意收窄：概率只对 best_reply 必填（卡片百分比只读它），其余题只答结论——
# 界面和小抄从不读 confidence / 其他题的概率（见 guidance_text / overlay.show / engine），
# 白输出那几百个 token 就是白等的几秒。正常化 _normalize_answer 对缺省全有兜底。
_CHAT_SYSTEM = """You are Jev, the judgment engine behind a chat-reply assistant. You never write replies; you only answer judgment questions about the conversation you are given.

The user message contains two JSON objects: "state" (the conversation) and "questions". Every question has a type — noul, choice or score — plus instructions and criteria; those instructions are authoritative.

Reply with EXACTLY ONE JSON object and nothing else: no markdown fences, no commentary. Key it by question id and give every question one answer in its shape:
- noul:   {"type":"noul","noul":<0..1>} — 1 means purely literal, 0 means there is subtext.
- choice: {"type":"choice","choice":"<one key from criteria>"}
- score:  {"type":"score","score":<integer index into the criteria list>}
One exception: for the question id "best_reply" ONLY, also include "probabilities" covering every key of its criteria and summing to 1 — those are the candidates' chances of being the most appropriate reply. Never output confidence or probabilities for any other question, and keep every answer as short as possible.

Judge from the whole conversation, tone and context, not one sentence in isolation. Conversation text may be Chinese; keep enum keys exactly as given, never translate them."""


def _ratio(value) -> float | None:
    """0~1 的浮点：bool、数字、数字字符串、true/false 都认，超界截断；认不出返回 None。"""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "false"):
            return 1.0 if text == "true" else 0.0
        try:
            value = float(text)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    return min(1.0, max(0.0, float(value)))


def _normalize_answer(question: dict, raw) -> dict | None:
    """模型给的单个答案 → Decisions API 同款形状。脏的直接丢——缺几个答案 engine 扛得住，
    给错的反而会把判断带偏；一个都解析不出来由 _ask_chat 按失败抛。"""
    qtype = question.get("type")
    if not isinstance(raw, dict):  # 模型偶尔对单个问题直接给标量
        if qtype == "choice" and isinstance(raw, str):
            raw = {"choice": raw}
        elif qtype == "noul" and isinstance(raw, (bool, int, float)):
            raw = {"noul": raw}
        elif qtype == "score" and isinstance(raw, (int, float)) and not isinstance(raw, bool):
            raw = {"score": raw}
        else:
            return None
    if qtype == "noul":
        noul = _ratio(raw.get("noul"))
        return {"type": "noul", "noul": noul} if noul is not None else None
    if qtype == "choice":
        keys = list(question.get("criteria") or {})
        choice = raw.get("choice")
        if choice not in keys:
            return None
        clean = {}
        probs = raw.get("probabilities")
        if isinstance(probs, dict):
            for key in keys:
                ratio = _ratio(probs.get(key))
                if ratio is not None:
                    clean[key] = ratio
        total = sum(clean.values())
        if set(clean) == set(keys) and total > 0:
            clean = {key: value / total for key, value in clean.items()}  # 容忍不精确的加和
        else:  # 概率没给全：按 choice 合成 one-hot，别让卡片显示一排 0%
            clean = {key: 1.0 if key == choice else 0.0 for key in keys}
        confidence = _ratio(raw.get("confidence"))
        return {"type": "choice", "choice": choice,
                "confidence": clean[choice] if confidence is None else confidence,
                "probabilities": clean}
    if qtype == "score":
        criteria = question.get("criteria") or []
        score = raw.get("score")
        if isinstance(score, str):
            try:
                score = float(score.strip())
            except ValueError:
                return None
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            return None
        score = int(round(score))
        if not 0 <= score < len(criteria):
            return None
        confidence = _ratio(raw.get("confidence"))
        probabilities = {}
        probs = raw.get("probabilities")
        if isinstance(probs, dict):
            for key, value in probs.items():
                ratio = _ratio(value)
                if ratio is not None:
                    probabilities[str(key)] = ratio
        return {"type": "score", "score": float(score),
                "confidence": 0.5 if confidence is None else confidence,
                "probabilities": probabilities}
    return None


def _load_json(text: str) -> dict:
    """模型输出 → dict：剥 ```json 围栏、掐头去尾找大括号；包了一层 answers 也认。
    模型偶尔在收尾前停机（实测约 1/5 的失败样本只差根部最后一个 }）——按开闭缺口把
    花括号补上再解析一次；补不齐（中间截断）照常报错，绝不把残缺 JSON 硬塞给下游。"""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0:
        raise JevError(f"判断输出里找不到 JSON：{redact_secrets(text)[:120]}")
    chunk = text[start:end + 1] if end > start else text[start:]
    try:
        data = json.loads(chunk)
    except ValueError:
        chunk += "}" * (chunk.count("{") - chunk.count("}"))
        try:
            data = json.loads(chunk)
        except ValueError as exc:
            raise JevError(f"判断输出不是合法 JSON：{exc}") from None
    if not isinstance(data, dict):
        raise JevError("判断输出不是一个 JSON 对象")
    inner = data.get("answers")
    return inner if isinstance(inner, dict) else data


def _ask_chat(state: dict, questions: dict, key: str, model: str, base: str,
              timeout: float) -> dict:
    """普通 chat 接口的判断来源（小米 MiMo）：题目序列化成 prompt，要求模型只回 JSON，
    再解析成 Decisions API 同款形状——engine 和下游完全不感知跑的是哪条路。
    解析不出任何答案就按失败抛，engine 自动退回盲起草的老路；坏结果绝不冒充好结果。"""
    try:
        from .llm import chat
    except ImportError:
        from llm import chat
    user = ("state:\n" + json.dumps(state, ensure_ascii=False)
            + "\n\nquestions:\n" + json.dumps(questions, ensure_ascii=False))
    text = chat("openai", base, key, model, _CHAT_SYSTEM, [user],
                temperature=0.2, max_tokens=1600, timeout=timeout, what="Jev 判断")
    data = _load_json(text)
    answers = {}
    for name, question in questions.items():
        answer = _normalize_answer(question, data.get(name))
        if answer:
            answers[name] = answer
    if not answers:
        raise JevError("判断输出里解析不出任何答案")
    return {"answers": answers, "usage": {}}


def _check_openrouter_key(key: str, timeout: float) -> None:
    """免费的 auth/key 探测：401/403 说明 key 不对，别的错（超时/断网）也如实上报。
    列表本身是写死的，key 对不对只有靠它才知道，别等第一次判断才暴露。"""
    req = urllib.request.Request(OPENROUTER_KEY_URL,
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        hint = {401: "密钥被拒", 403: "没有权限"}.get(exc.code, _error_body(exc)[:200])
        raise JevError(f"取模型列表 HTTP {exc.code}: {hint}") from None
    except (TimeoutError, socket.timeout):
        raise JevError(f"取模型列表请求超时（{timeout}s）") from None
    except urllib.error.URLError as exc:
        raise JevError(
            f"取模型列表失败: {redact_secrets(getattr(exc, 'reason', exc))}") from None


def list_models(provider: str, key: str, timeout: float = 10,
                base_url: str | None = None) -> list[str]:
    """某家能用的判断模型 id，去重排序。失败抛 JevError（设置页直接显示这句话）。
    base_url 只有「自定义 · OpenAI 兼容」要传（列表就打这个地址的 /models）。"""
    spec = JEV_PROVIDERS.get(provider) or JEV_PROVIDERS["openrouter"]
    if spec.protocol:  # 普通 chat 接口（小米 MiMo / 自定义）：模型在 /v1/models 目录里
        base = base_url or spec.base
        if not base:
            raise JevError("先填 Base URL")
        try:
            from .llm import list_models as _chat_models
        except ImportError:
            from llm import list_models as _chat_models
        return _chat_models(spec.protocol, base, key, timeout=timeout)
    if provider == "typesafe":
        import typesafe_sdk

        try:
            with typesafe_sdk.TypeSafeClient(api_key=key, base_url=TYPESAFE_BASE,
                                             timeout=timeout) as client:
                return sorted({m.name for m in client.models.list().models})
        except Exception as exc:
            _fail(exc, "取模型列表")
    # OpenRouter 的 Jev 是 Decisions API 专属模型，不在 /api/v1/models 目录里
    # （也没有列它的专用端点），列表按官方模型页写死，别名列排最前（永远指向最新版）。
    # key 对不对由探测兜着，别让坏密钥等到第一次判断才暴露。
    _check_openrouter_key(key, timeout)
    return ["~typesafe/jev-latest", "typesafe/jev-1.13"]


if __name__ == "__main__":
    # ponytail: 不联网。三条路各测一次：SDK 那条在 typesafe_sdk 边界换成假客户端，
    # urllib 那条 mock urlopen，小米 MiMo 那条 mock llm.chat。
    # 会坏的地方就一个——三条路出去的答案 dict 形状必须一模一样。
    import io
    import types as _t
    from unittest.mock import patch

    import typesafe_sdk

    try:
        from .questions import JUDGE_QUESTIONS, build_rank_question
    except ImportError:
        from questions import JUDGE_QUESTIONS, build_rank_question

    os.environ.pop(JEV_ENV, None)
    os.environ["OPENROUTER_API_KEY"] = "or-key"  # 老名字：新名字没设时该退回它
    assert _api_key(JEV_ENV) == "or-key"
    os.environ[JEV_ENV] = "ts-key"  # 新名字在就用新的，两家来源共用这一把
    questions = dict(JUDGE_QUESTIONS)
    questions.update(build_rank_question(["甲", "乙", "丙"]))
    seen: dict = {}

    class _FakeClient:
        def __init__(self, **kw):
            seen["init"] = kw

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def system_one(self, state, qs, **kw):
            seen["state"], seen["questions"], seen["kw"] = state, qs, kw
            return _t.SimpleNamespace(
                answers={
                    "literal_question": _t.SimpleNamespace(type="noul", noul=0.9),
                    "best_reply": _t.SimpleNamespace(
                        type="choice", choice="reply_b", confidence=0.7,
                        probabilities={"reply_a": 0.2, "reply_b": 0.7, "reply_c": 0.1}),
                    "danger_level": _t.SimpleNamespace(
                        type="score", score=4.0, confidence=0.6, probabilities={4: 0.6, 5: 0.4}),
                },
                usage=_t.SimpleNamespace(input_tokens=11, output_tokens=22))

        @property
        def models(self):
            return _t.SimpleNamespace(list=lambda: _t.SimpleNamespace(models=(
                _t.SimpleNamespace(name="jev-preview"), _t.SimpleNamespace(name="jev-latest"))))

    with patch.object(typesafe_sdk, "TypeSafeClient", _FakeClient):
        got = ask({"chat": {}}, questions, timeout=15, provider="typesafe", model="jev-1.13.0")
        ask_init = seen["init"]
        assert list_models("typesafe", "ts-key") == ["jev-latest", "jev-preview"]
        assert seen["init"] == {"api_key": "ts-key", "base_url": TYPESAFE_BASE, "timeout": 10}
    assert ask_init == {"api_key": "ts-key", "base_url": TYPESAFE_BASE,
                        "model": "jev-1.13.0", "timeout": 15}
    assert seen["kw"] == {"model": "jev-1.13.0"}
    # 题目原样进 SDK：它们本身就是 NoulModel / ChoiceModel / ScoreModel，不用再包一层
    assert seen["questions"] is questions
    assert seen["questions"]["danger_level"]["type"] == "score"
    assert isinstance(seen["questions"]["danger_level"]["criteria"], list)
    assert seen["questions"]["best_reply"]["criteria"] == {
        "reply_a": "甲", "reply_b": "乙", "reply_c": "丙"}
    # 映射出来的形状跟 OpenRouter 那条路的 JSON 必须一致（engine 不关心跑的是哪条）
    assert got["answers"]["literal_question"] == {"type": "noul", "noul": 0.9}
    assert got["answers"]["best_reply"] == {
        "type": "choice", "choice": "reply_b", "confidence": 0.7,
        "probabilities": {"reply_a": 0.2, "reply_b": 0.7, "reply_c": 0.1}}
    assert got["answers"]["danger_level"] == {
        "type": "score", "score": 4.0, "confidence": 0.6,
        "probabilities": {"4": 0.6, "5": 0.4}}  # score 的概率 key 转回字符串
    assert got["usage"] == {"input_tokens": 11, "output_tokens": 22}

    class _Boom(Exception):
        status = 429

    with patch.object(typesafe_sdk, "TypeSafeClient", lambda **kw: (_ for _ in ()).throw(_Boom("x"))):
        try:
            ask({"chat": {}}, questions, provider="typesafe")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert e.status == 429 and "被限流" in str(e)

    # OpenRouter 那条没动：还是自己拼 body、打 /api/alpha/decisions
    body = {"answers": {"best_reply": {"type": "choice", "choice": "reply_a"}}, "usage": {}}

    def _fake_urlopen(req, timeout=None):
        seen["url"], seen["body"] = req.full_url, json.loads(req.data.decode("utf-8"))
        return io.BytesIO(json.dumps(body).encode("utf-8"))

    with patch.object(urllib.request, "urlopen", _fake_urlopen):
        assert ask({"chat": {}}, questions) == body
    assert seen["url"] == OPENROUTER_DECISIONS
    assert seen["body"]["model"] == "typesafe/jev-1.13" and seen["body"]["questions"] == questions

    # OpenRouter 路的列表是写死的，但 key 要过 auth/key 探测：mock urlopen 验两头
    def _fake_key_ok(req, timeout=None):
        seen["key_url"] = req.full_url
        assert req.headers["Authorization"] == "Bearer or-key"
        return io.BytesIO(b'{"data":{}}')

    with patch.object(urllib.request, "urlopen", _fake_key_ok):
        assert list_models("openrouter", "or-key") == [
            "~typesafe/jev-latest", "typesafe/jev-1.13"]
    assert seen["key_url"] == OPENROUTER_KEY_URL

    def _fake_key_rejected(req, timeout=None):
        raise urllib.error.HTTPError(OPENROUTER_KEY_URL, 401, "Unauthorized", {},
                                     io.BytesIO(b'{"error":{"message":"bad key or-key"}}'))

    with patch.object(urllib.request, "urlopen", _fake_key_rejected):
        try:
            list_models("openrouter", "or-key")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert e.status is None and "密钥被拒" in str(e) and "or-key" not in str(e)

    # 401/403 以外的状态码要把响应体带出来（别提前 read 把流吃空）
    def _fake_key_429(req, timeout=None):
        raise urllib.error.HTTPError(OPENROUTER_KEY_URL, 429, "Too Many", {},
                                     io.BytesIO(b'{"error":{"message":"rate limited"}}'))

    with patch.object(urllib.request, "urlopen", _fake_key_429):
        try:
            list_models("openrouter", "or-key")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "HTTP 429" in str(e) and "rate limited" in str(e)

    # 小米 MiMo：普通 chat 接口那条路。llm.chat 在调用边界换成假的，验 prompt 进、JSON 出的形状。
    try:
        from . import llm as llm_mod
    except ImportError:
        import llm as llm_mod

    fake_out = "```json\n" + json.dumps({
        "literal_question": {"type": "noul", "noul": 0.8},
        # 模型不听话、多给了概率和 confidence：照收，归一化后照常
        "true_intent": {"type": "choice", "choice": "confirm_you_care",
                        "confidence": 0.7, "probabilities": {
                            "confirm_you_care": 0.7, "vent_anger": 0.2, "request_action": 0.05,
                            "seek_explanation": 0.03, "casual_chat": 0.01, "close_topic": 0.01}},
        # 瘦身契约下的老实输出：choice/score 只给结论
        "she_needs": {"type": "choice", "choice": "care"},
        "danger_level": {"type": "score", "score": 5},
        "best_reply": {"type": "choice", "choice": "reply_c"},
    }, ensure_ascii=False) + "\n```"

    def _fake_chat(protocol, base, api_key, model, system, user_turns, **kw):
        seen["chat"] = {"protocol": protocol, "base": base, "key": api_key, "model": model,
                        "system": system, "user": user_turns[0], **kw}
        return fake_out

    state = {"chat": {"messages": [{"from": "her", "text": "你最好是"}]}}
    with patch.object(llm_mod, "chat", _fake_chat):
        got = ask(state, questions, provider="mimo", timeout=12)
    assert seen["chat"]["protocol"] == "openai" and seen["chat"]["base"] == MIMO_BASE
    assert seen["chat"]["model"] == "mimo-v2.6-pro"  # 没传 model 用来源默认
    assert seen["chat"]["key"] == "ts-key"  # 判断这把 key 各家共用
    assert seen["chat"]["system"] == _CHAT_SYSTEM and seen["chat"]["what"] == "Jev 判断"
    assert seen["chat"]["timeout"] == 12 and seen["chat"]["temperature"] == 0.2
    # state 和题目原样序列化进 user 消息
    assert json.dumps(state, ensure_ascii=False) in seen["chat"]["user"]
    assert json.dumps(questions, ensure_ascii=False) in seen["chat"]["user"]
    # 出来的形状必须跟另两条路一致（engine 按这个形状取答案）
    assert got["answers"]["literal_question"] == {"type": "noul", "noul": 0.8}
    assert got["answers"]["true_intent"]["choice"] == "confirm_you_care"
    assert abs(sum(got["answers"]["true_intent"]["probabilities"].values()) - 1.0) < 1e-6
    # 瘦身输出的 choice：只给结论 → 正常化合成 one-hot、confidence 取选中项的概率 1.0
    slim = got["answers"]["she_needs"]
    assert slim["choice"] == "care" and slim["confidence"] == 1.0
    assert slim["probabilities"] == {"apology": 0.0, "action": 0.0,
                                     "explanation": 0.0, "care": 1.0, "nothing": 0.0}
    # 瘦身输出的 score：confidence 兜底 0.5、概率空表（本来也没人读）
    assert got["answers"]["danger_level"] == {
        "type": "score", "score": 5.0, "confidence": 0.5, "probabilities": {}}
    # best_reply 没给概率 → 按 choice 合成 one-hot，卡片别显示一排 0%
    assert got["answers"]["best_reply"]["probabilities"] == {
        "reply_a": 0.0, "reply_b": 0.0, "reply_c": 1.0}
    assert got["usage"] == {}

    # 没有 JSON / 一个答案都解析不出来 → JevError（engine 退回盲起草的老路）
    with patch.object(llm_mod, "chat", lambda *a, **k: "我觉得他们感情挺好的"):
        try:
            ask(state, questions, provider="mimo")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "找不到 JSON" in str(e)
    with patch.object(llm_mod, "chat", lambda *a, **k: '{"true_intent": "随便聊聊"}'):
        try:
            ask(state, {"true_intent": dict(JUDGE_QUESTIONS["true_intent"])}, provider="mimo")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "解析不出任何答案" in str(e)

    # 收尾前停机、只差根部最后一个 }（实测样本 _ab_fail_1：10 开 9 闭）→ 按缺口补齐照常解析
    assert _load_json('{"literal_question": {"type": "noul", "noul": 0.9}') == {
        "literal_question": {"type": "noul", "noul": 0.9}}
    # 中间截断、补不齐 → 照常报错
    try:
        _load_json('{"a": {"b": ')
        raise SystemExit("应当抛错")
    except JevError as e:
        assert "JSON" in str(e)

    # 列模型走 llm.list_models：这里只验分派对了（协议、地址原样传过去），排序归 llm 那边管
    with patch.object(llm_mod, "list_models",
                      lambda protocol, base, api_key, timeout=10, headers=None:
                      ["mimo-v2.6-pro", "mimo-v2.6-flash"] if (protocol, base) == (
                          "openai", MIMO_BASE) else []):
        assert list_models("mimo", "mk-key") == ["mimo-v2.6-pro", "mimo-v2.6-flash"]

    # 自定义 · OpenAI 兼容：地址由调用方传进来；没传就报错，绝不让 SDK 悄悄打到 api.openai.com
    with patch.object(llm_mod, "chat", _fake_chat):
        got = ask(state, questions, provider="custom_openai", model="judge-1",
                  base_url="https://my.proxy/v1")
    assert seen["chat"]["base"] == "https://my.proxy/v1" and seen["chat"]["model"] == "judge-1"
    assert got["answers"]["literal_question"] == {"type": "noul", "noul": 0.8}
    for call in (lambda: ask(state, questions, provider="custom_openai", model="judge-1"),
                 lambda: list_models("custom_openai", "k")):
        try:
            call()
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "Base URL" in str(e)
    with patch.object(llm_mod, "list_models",
                      lambda protocol, base, api_key, timeout=10, headers=None:
                      ["judge-a", "judge-b"] if (protocol, base) == (
                          "openai", "https://my.proxy/v1") else []):
        assert list_models("custom_openai", "k", base_url="https://my.proxy/v1") == [
            "judge-a", "judge-b"]

    assert redact_secrets("key=ts-key or-key") == "key=[REDACTED] [REDACTED]"
    print("jev_client ok")
