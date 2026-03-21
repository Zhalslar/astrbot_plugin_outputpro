from collections import OrderedDict, deque
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from astrbot.core.message.components import BaseMessageComponent
from astrbot.core.platform.astr_message_event import AstrMessageEvent


class GroupState(BaseModel):
    gid: str
    """群号"""
    bot_msgs: deque = Field(default_factory=lambda: deque(maxlen=5))
    """Bot消息缓存"""
    msg_queue: deque[str] = Field(default_factory=lambda: deque(maxlen=10))
    """用户消息缓存"""
    last_reply_mark_msg_id: str | None = None
    """最近一次追加 bot 插嘴标记对应的消息 ID（用于去重）"""
    name_to_qq: OrderedDict[str, str] = Field(default_factory=lambda: OrderedDict())
    """昵称 -> QQ"""


class StateManager:
    """内存状态管理"""

    _groups: dict[str, GroupState] = {}

    @classmethod
    def get_group(cls, gid: str) -> GroupState:
        if gid not in cls._groups:
            cls._groups[gid] = GroupState(gid=gid)
        return cls._groups[gid]


@dataclass
class OutContext:
    """输出消息上下文"""

    event: AstrMessageEvent
    chain: list[BaseMessageComponent]
    is_llm: bool
    plain: str
    gid: str
    uid: str
    bid: str
    group: GroupState
    timestamp: int



class StepName(str, Enum):
    SUMMARY = "summary"
    ERROR = "error"
    BLOCK = "block"
    AT = "at"
    CLEAN = "clean"
    REPLACE = "replace"
    TTS = "tts"
    T2I = "t2i"
    REPLY = "reply"
    FORWARD = "forward"
    RECALL = "recall"
    SPLIT = "split"


@dataclass(slots=True)
class StepResult:
    ok: bool = True
    """步骤是否成功"""
    abort: bool = False
    """是否需要中断处理"""
    msg: str | None = None
    """附加消息"""
    data: Any | None = None
    """携带的上下文信息"""
