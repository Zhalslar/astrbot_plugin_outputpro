import random
import re
from collections import OrderedDict, deque

import emoji
from pydantic import BaseModel, Field

from astrbot import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import (
    At,
    BaseMessageComponent,
    Plain,
    Reply,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent


class GroupState(BaseModel):
    gid: str
    """群号"""
    bot_msgs: deque = Field(default_factory=lambda: deque(maxlen=5))
    """Bot消息缓存"""
    last_mid: str = ""
    """群内最新消息的ID"""
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


@register("astrbot_plugin_outputpro", "Zhalslar", "...", "...")
class BetterIOPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.at_regex = re.compile(
            r"\[at[:：]\s*(\d+)\]"  # [at:123]
            r"|\[at[:：]\s*([^\]]+)\]"  # [at:nickname]
            r"|@(\d{5,12})(?=\s|$)"  # @数字QQ
            r"|@(\S+)",  # @昵称
            re.IGNORECASE,  # **忽略大小写，At AT aT 全匹配**
        )

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message(self, event: AstrMessageEvent):
        """接收消息后的处理"""
        gid: str = event.get_group_id()
        g: GroupState = StateManager.get_group(gid)
        g.last_mid = event.message_obj.message_id

        # 缓存 “昵称 -> QQ”
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name()
        if sender_name not in g.name_to_qq and len(g.name_to_qq) == 100:
            g.name_to_qq.popitem(last=False)  # FIFO 头删
        g.name_to_qq[sender_name] = sender_id

    @filter.on_decorating_result(priority=15)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """发送消息前的预处理"""
        # 过滤空消息
        result = event.get_result()
        chain = result.chain
        if not result or not chain:
            event.stop_event()
            return

        msg = result.get_plain_text()
        gid: str = event.get_group_id()
        g: GroupState = StateManager.get_group(gid)

        # 拦截错误信息(管理员触发的则不拦截)
        econf = self.conf["error"]
        if econf["block_error"] and not event.is_admin():
            for word in econf["keywords"]:
                if word in msg:
                    event.set_result(event.plain_result(""))
                    logger.info(f"已阻止发送报错提示：{msg}")
                    return

        # 仅处理LLM消息
        if self.conf["only_llm_result"] and not result.is_llm_result():
            return

        # 拦截重复消息
        iconf = self.conf["intercept"]
        if iconf["reread_block"] and msg in g.bot_msgs:
            event.set_result(event.plain_result(""))
            logger.info(f"已阻止LLM发送重复消息：{msg}")
            return
        g.bot_msgs.append(msg)

        # 拦截人机发言
        tconf = self.conf["toobot"]
        if tconf["block_ai"]:
            for word in tconf["keywords"]:
                if word in msg:
                    event.set_result(event.plain_result(""))
                    logger.info(f"已阻止LLM过于人机的发言:{msg}")
                    return

        # 解析 At 消息
        await self.parse_ats(chain, g)

        # 清洗文本消息
        cconf = self.conf["clean"]
        for seg in chain:
            if isinstance(seg, Plain) and len(seg.text) < cconf["text_threshold"]:
                # 摘除中括号内容
                if cconf["bracket"]:
                    seg.text = re.sub(r"\[.*?\]", "", seg.text)
                # 摘除小括号内容（半角/全角）
                if cconf["parenthesis"]:
                    seg.text = re.sub(r"[（(].*?[）)]", "", seg.text)
                # 摘除情绪标签
                if cconf["emotion_tag"]:
                    seg.text = re.sub(r"&&.*?&&", "", seg.text)
                # 清洗emoji
                if cconf["emoji"]:
                    seg.text = emoji.replace_emoji(seg.text, replace="")
                # 去除指定开头字符
                if cconf["lead"]:
                    for remove_lead in cconf["lead"]:
                        if seg.text.startswith(remove_lead):
                            seg.text = seg.text[len(remove_lead) :]
                # 去除指定结尾字符
                if cconf["tail"]:
                    for remove_tail in cconf["tail"]:
                        if seg.text.endswith(remove_tail):
                            seg.text = seg.text[: -len(remove_tail)]
                # 整体清洗标点符号
                if cconf["punctuation"]:
                    seg.text = re.sub(cconf["punctuation"], "", seg.text)

        if event.get_platform_name() == "aiocqhttp":

            if self.conf["at_prob"]:
                has_at = any(isinstance(c, At) for c in chain)
                if random.random() < self.conf["at_prob"]:  # 概率命中 → 必须带 @
                    if not has_at and chain and isinstance(chain[0], Plain):
                        chain.insert(0, At(qq=event.get_sender_id()))
                else:  # 概率未命中 → 必须不带 @
                    if has_at:
                        chain[:] = [c for c in chain if not isinstance(c, At)]

            # 引用被顶的消息（仅aiocqhttp平台）
            if self.conf["smart_reply"]:
                trigger_mid = event.message_obj.message_id

                if (
                    not any(isinstance(item, Reply) for item in chain)
                    and trigger_mid != g.last_mid
                ):
                    chain.insert(0, Reply(id=trigger_mid))
                    logger.debug("已插入Reply组件")

    async def parse_ats(
        self, chain: list[BaseMessageComponent], gstate: GroupState
    ) -> None:
        """
        把字符串里的艾特语法原地换成真 At 组件。
        """
        for seg in chain:
            if not isinstance(seg, Plain):
                continue
            text = seg.text
            if not text:
                continue

            # 从后往前匹配，避免 insert 导致偏移
            for m in reversed(list(self.at_regex.finditer(text))):
                qq = (
                    m.group(1)  # [at:123]
                    or gstate.name_to_qq.get(m.group(2))  # [at:nick]
                    or m.group(3)  # @数字
                    or gstate.name_to_qq.get(m.group(4))  # @nick
                )
                if not qq:
                    continue  # 未命中缓存，保留原文

                # 拆段插入
                head, tail = text[: m.start()], text[m.end() :]
                seg.text = head
                idx = chain.index(seg) + 1
                chain.insert(idx, Plain("\u200b"))
                chain.insert(idx + 1, At(qq=qq))
                chain.insert(idx + 2, Plain("\u200b"))
                if tail:
                    chain.insert(idx + 3, Plain(tail))
                # 更新引用，继续处理剩余尾巴
                seg = chain[idx + 2]
                text = tail
