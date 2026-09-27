# -*- coding: utf-8 -*-
"""Per-chat-app profile: which window to capture, which OCR engine, which bubble is mine.

Everything that used to be hard-coded for WeChat lives here, so adding an app is one entry.
The pixel rules stay where they were proven (see ocr.who_said); this file only names them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np


def _wechat_me(bg: np.ndarray) -> bool:
    """WeChat's own bubble is green: G clearly above both R and B."""
    return bool(bg[1] > bg[0] + 40 and bg[1] > bg[2] + 40)


def _kakao_me(bg: np.ndarray) -> bool:
    """KakaoTalk's own bubble is #FEE500: R and G high, B low. Measured on a live window;
    the chat ground is #BACEE0 and the other side is #FFFFFF, so both stay clear of this."""
    return bool(bg[0] > 200 and bg[1] > 180 and bg[2] < 120)


@dataclass(frozen=True)
class ChatApp:
    key: str
    label: str
    exes: tuple[str, ...]        # process names, lowercase
    main_title: str              # preferred top-level window title; "" = pick the biggest window
    skip_titles: tuple[str, ...]  # windows that are never a conversation (roster, toasts)
    ocr: str                     # "rapidocr" (zh/en) or "windows" (Windows.Media.Ocr, ko/ja/...)
    join: str                    # how OCR fragments inside one bubble are glued
    is_me: Callable[[np.ndarray], bool]


WECHAT = ChatApp("wechat", "微信", ("weixin.exe", "wechat.exe"), "微信", (),
                 "rapidocr", "", _wechat_me)
# KakaoTalk opens one window per conversation, so the roster ("카카오톡") is never the target;
# the biggest remaining window is the chat that is actually being read.
KAKAOTALK = ChatApp("kakaotalk", "카카오톡", ("kakaotalk.exe",), "", ("카카오톡", ""),
                    "windows", " ", _kakao_me)

APPS = {a.key: a for a in (WECHAT, KAKAOTALK)}
DEFAULT = WECHAT


def by_exe(exe: str) -> ChatApp | None:
    return next((a for a in APPS.values() if exe in a.exes), None)


def get(key: str | None) -> ChatApp:
    return APPS.get(key or "", DEFAULT)


if __name__ == "__main__":  # 自测：颜色规则用实测像素锁住，改错了当场炸
    assert by_exe("kakaotalk.exe") is KAKAOTALK and by_exe("weixin.exe") is WECHAT
    assert by_exe("chrome.exe") is None and get(None) is DEFAULT and get("kakaotalk") is KAKAOTALK
    kakao_bubble, kakao_other, kakao_ground = (254, 229, 0), (255, 255, 255), (186, 206, 224)
    assert KAKAOTALK.is_me(np.array(kakao_bubble))
    assert not KAKAOTALK.is_me(np.array(kakao_other))
    assert not KAKAOTALK.is_me(np.array(kakao_ground))
    assert WECHAT.is_me(np.array((149, 236, 105))) and not WECHAT.is_me(np.array(kakao_bubble))
    print("chatapps ok")
