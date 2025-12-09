import random
import re
from collections import deque

import emoji
from pydantic import BaseModel, Field

from astrbot import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import (
    At,
    Face,
    Image,
    Plain,
    Reply,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent


class GroupState(BaseModel):
    gid: str
    bot_msgs: deque = Field(
        default_factory=lambda: deque(maxlen=5)
    )  # Bot消息缓存，共5条
    last_seen_mid: str = ""  # 群内最新消息的ID


class StateManager:
    """内存状态管理"""

    _groups: dict[str, GroupState] = {}

    @classmethod
    def get_group(cls, gid: str) -> GroupState:
        if gid not in cls._groups:
            cls._groups[gid] = GroupState(gid=gid)
        return cls._groups[gid]


@register("astrbot_plugin_outputpro", "Zhalslar", "...", "...")
class BetterIOPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message(self, event: AstrMessageEvent):
        """接收消息后的处理"""
        gid: str = event.get_group_id()
        g: GroupState = StateManager.get_group(gid)
        g.last_seen_mid = event.message_obj.message_id

    @filter.on_decorating_result(priority=15)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """发送消息前的预处理"""
        # 过滤空消息
        result = event.get_result()
        chain = result.chain
        if not chain:
            event.stop_event()
            return

        gid: str = event.get_group_id()
        g: GroupState = StateManager.get_group(gid)

        # 拦截重复消息
        msg = result.get_plain_text()
        if msg and msg in g.bot_msgs:
            event.set_result(event.plain_result(""))
            logger.info(f"已阻止发送重复消息：{msg}")
            return
        g.bot_msgs.append(msg)

        # 拦截错误信息(管理员触发的则不拦截)
        iconf = self.conf["intercept"]
        if iconf["block_error"] and not event.is_admin():
            for word in iconf["error_words"]:
                if word in msg:
                    event.set_result(event.plain_result(""))
                    logger.info(f"已阻止发送报错提示：{msg}")
                    return

        # 拦截人机发言
        if iconf["block_ai"]:
            for word in iconf["ai_words"]:
                if word in msg:
                    event.set_result(event.plain_result(""))
                    logger.info(f"已阻止人机发言:{msg}")
                    return

        # 过滤不支持的消息类型
        if not all(isinstance(comp, Plain | Image | Face) for comp in chain):
            return

        # 清洗文本消息
        cconf = self.conf["clean"]
        end_seg = chain[-1]
        if isinstance(end_seg, Plain) and len(end_seg.text) < cconf["text_threshold"]:
            # 1.摘掉开头的 [At:xxx] 或 [At：xxx]
            if cconf["format_at"]:
                end_seg.text = re.sub(r"^\[At[:：][^\]]+]\s*", "", end_seg.text)
                logger.debug("已摘掉开头的 [At:xxx]")

            # 2.把开头的“@xxx ”（含中英文空格）整体摘掉
            if cconf["fake_at"]:
                end_seg.text = re.sub(r"^@\S+\s*", "", end_seg.text)
                logger.debug("已摘掉开头的 @xxx ")

            # 3.清洗emoji
            if cconf["emoji"]:
                end_seg.text = emoji.replace_emoji(end_seg.text, replace="")
            # 4.去除指定开头字符
            if cconf["lead"]:
                for remove_lead in cconf["lead"]:
                    if end_seg.text.startswith(remove_lead):
                        end_seg.text = end_seg.text[len(remove_lead) :]
            # 5.去除指定结尾字符
            if cconf["tail"]:
                for remove_tail in cconf["tail"]:
                    if end_seg.text.endswith(remove_tail):
                        end_seg.text = end_seg.text[: -len(remove_tail)]
            # 6.整体清洗标点符号
            if cconf["punctuation"]:
                end_seg.text = re.sub(cconf["punctuation"], "", end_seg.text)

        if event.get_platform_name() == "aiocqhttp":
            trigger_mid = event.message_obj.message_id
            rconf = self.conf["reat"]
            # 1.消息被顶上去了, 则引用
            if (
                rconf["reply_switch"]
                and not any(isinstance(item, Reply) for item in chain)
                and trigger_mid != g.last_seen_mid
            ):
                chain.insert(0, Reply(id=trigger_mid))
                logger.debug("已插入Reply组件")

            # 2.按概率@发送者
            if (
                random.random() < rconf["at_prob"]
                and isinstance(end_seg, Plain)
                and end_seg.text.strip()
                and not any(isinstance(item, At) for item in chain)
                and not end_seg.text.startswith("@")
            ):
                if rconf["str_at"]:
                    send_name = event.get_sender_name()
                    end_seg.text = f"@{send_name} {end_seg.text}"
                    logger.debug("已插入假@")
                else:
                    chain.insert(0, At(qq=event.get_sender_id()))
                    logger.debug("已插入At组件")
