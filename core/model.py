from collections import OrderedDict, deque

from attr import dataclass
from pydantic import BaseModel, Field

from astrbot.core.message.components import BaseMessageComponent
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.platform.astr_message_event import AstrMessageEvent


class GroupState(BaseModel):
    gid: str
    """群号"""
    bot_msgs: deque = Field(default_factory=lambda: deque(maxlen=5))
    """Bot消息缓存"""
    msg_queue: deque[str] = Field(default_factory=lambda: deque(maxlen=10))
    """用户消息缓存"""
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
    """上下文"""

    event: AstrMessageEvent
    chain: list[BaseMessageComponent]
    result: MessageEventResult
    plain: str
    gid: str
    uid: str
    bid: str
    group: GroupState
