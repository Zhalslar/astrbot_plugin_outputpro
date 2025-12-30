import asyncio

from aiocqhttp import CQHttp

from astrbot.api import logger
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import (
    At,
    AtAll,
    BaseMessageComponent,
    Face,
    Forward,
    Image,
    Nodes,
    Plain,
    Reply,
    Video,
)
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .model import OutContext


class Recaller:
    def __init__(self, config: AstrBotConfig):
        self.conf = config
        self.recall_tasks: list[asyncio.Task] = []

    async def initialize(self):
        pass

    async def terminate(self):
        """取消所有撤回任务"""
        for task in self.recall_tasks:
            task.cancel()
        await asyncio.gather(*self.recall_tasks, return_exceptions=True)
        self.recall_tasks.clear()

    def _remove_task(self, task: asyncio.Task):
        try:
            self.recall_tasks.remove(task)
        except ValueError:
            pass

    def _is_recall(self, chain: list[BaseMessageComponent]) -> bool:
        """判断消息是否需撤回"""
        for seg in chain:
            if isinstance(seg, Plain):
                # 判断关键词
                for word in self.conf["recall"]["keywords"]:
                    if word in seg.text:
                        return True
            elif isinstance(seg, Image):
                # TODO: 判断色图
                continue
        return False

    async def _recall_msg(self, client: CQHttp, message_id: int = 1):
        """撤回消息"""
        await asyncio.sleep(self.conf["recall"]["delay"])
        try:
            if message_id:
                await client.delete_msg(message_id=message_id)
                logger.debug(f"已自动撤回消息: {message_id}")
        except Exception as e:
            logger.error(f"撤回消息失败: {e}")

    async def send_and_recall(self, ctx: OutContext):
        """对外接口：发消息并撤回"""
        if not isinstance(ctx.event, AiocqhttpMessageEvent):
            return
        if not any(
            isinstance(
                seg, Plain | Image | Video | Face | At | AtAll | Forward | Reply | Nodes
            )
            for seg in ctx.chain
        ):
            return

        # 判断消息是否需要撤回
        if not self._is_recall(ctx.chain):
            return

        ctx.event.should_call_llm(True)
        obmsg = await ctx.event._parse_onebot_json(MessageChain(chain=ctx.chain))
        client = ctx.event.bot

        send_result = None
        if ctx.gid:
            send_result = await client.send_group_msg(
                group_id=int(ctx.gid), message=obmsg
            )
        elif ctx.uid:
            send_result = await client.send_private_msg(
                user_id=int(ctx.uid), message=obmsg
            )

        # 启动撤回任务
        if send_result and (message_id := send_result.get("message_id")):
            task = asyncio.create_task(self._recall_msg(client, int(message_id)))  # type: ignore
            task.add_done_callback(self._remove_task)
            self.recall_tasks.append(task)

        # 清空原消息链
        ctx.chain.clear()
