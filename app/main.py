import logging
import re
import time

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from app.config import Settings, load_settings
from app.llm import LLMClient
from app.memory import MemoryStore
from app.telegram_format import markdown_to_telegram_html, split_plain_text, split_telegram_html_chunks


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
TELEGRAM_MAX_TEXT_LENGTH = 4000
STREAM_EDIT_INTERVAL_SECONDS = 0.6
STREAM_BLOCK_MIN_CHARS = 180
STREAM_BLOCK_MAX_CHARS = 700
STREAM_PREVIEW_MAX_CHARS = 3500
SESSION_BUTTON_LIMIT = 12
SUMMARY_TRIGGER_MESSAGES = 40
SUMMARY_EVERY_MESSAGES = 20
SUMMARY_RECENT_MESSAGES = 24
CONTEXT_TOKEN_BUDGET = 12000
CONTEXT_RESPONSE_RESERVE_TOKENS = 2000
CONTEXT_SYSTEM_RESERVE_TOKENS = 600
CONTEXT_MIN_HISTORY_MESSAGES = 6
SUMMARY_FORCE_TRIGGER_TOKENS = 9000
EMPTY_REPLY_TEXT = "抱歉，模型这次没有返回内容，请重试。"
SENTENCE_BREAK_RE = re.compile(r"[。！？.!?](?:\s|$)")


class BotApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.memory = MemoryStore()
        self.llm = LLMClient(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.openai_model,
        )
        self._pending_actions: dict[str, str] = {}

    async def new_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat or not update.message:
            return
        chat_id = str(update.effective_chat.id)
        await self._refresh_summary(chat_id, force=True)
        name = " ".join(context.args).strip() if context.args else ""
        created = self.memory.create_session(chat_id, name or "新会话")
        await self._send_reply(update, f"已创建并切换到会话：{created['name']}")

    async def list_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not update.effective_chat or not update.message:
            return
        chat_id = str(update.effective_chat.id)
        text, markup = self._build_sessions_view(chat_id)
        if not markup:
            await self._send_reply(update, text)
            return
        await update.message.reply_text(text, reply_markup=markup, disable_web_page_preview=True)

    async def rename_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat or not update.message:
            return
        chat_id = str(update.effective_chat.id)
        new_name = " ".join(context.args).strip() if context.args else ""
        if not new_name:
            self._pending_actions[chat_id] = "rename_session"
            await self._send_reply(update, "请发送新的会话名称。")
            return
        ok, result = self.memory.rename_active_session(chat_id, new_name)
        if not ok:
            await self._send_reply(update, result)
            return
        await self._send_reply(update, f"已重命名当前会话为：{result}")

    async def delete_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not update.effective_chat or not update.message:
            return
        chat_id = str(update.effective_chat.id)
        active = self.memory.get_active_session(chat_id)
        current_id = int(active["id"])
        current_name = str(active["name"])
        confirm_markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("确认删除", callback_data=f"delconfirm:{current_id}"),
                    InlineKeyboardButton("取消", callback_data=f"delcancel:{current_id}"),
                ]
            ]
        )
        await update.message.reply_text(
            f"确认删除当前会话“{current_name}”？删除后会自动新建会话。",
            reply_markup=confirm_markup,
            disable_web_page_preview=True,
        )

    async def session_action_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not update.callback_query or not update.effective_chat:
            return
        query = update.callback_query
        data = query.data or ""
        if ":" not in data:
            return
        action, raw_session_id = data.split(":", 1)
        chat_id = str(update.effective_chat.id)
        try:
            target_session_id = int(raw_session_id)
        except ValueError:
            await query.answer("会话参数错误", show_alert=True)
            return

        if action == "sess":
            await self._refresh_summary(chat_id, force=True)
            switched = self.memory.switch_session(chat_id, target_session_id)
            if not switched:
                await self._answer_callback(query, "会话不存在或不可切换", show_alert=True)
                return
            await self._answer_callback(query, "已切换")
            active = self.memory.get_active_session(chat_id)
            await self._edit_callback_message(query, f"已切换到会话：{active['name']}")
            return

        if action == "del":
            sessions = self.memory.list_sessions(chat_id, SESSION_BUTTON_LIMIT)
            target = next((s for s in sessions if int(s["id"]) == target_session_id), None)
            if not target:
                await self._answer_callback(query, "会话不存在", show_alert=True)
                return
            await self._answer_callback(query)
            name = str(target["name"])
            is_active = bool(target["is_active"])
            prompt = f"确认删除会话“{name}”？"
            if is_active:
                prompt = f"确认删除当前会话“{name}”？删除后会自动新建会话。"
            confirm_markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("确认删除", callback_data=f"delconfirm:{target_session_id}"),
                        InlineKeyboardButton("取消", callback_data=f"delcancel:{target_session_id}"),
                    ]
                ]
            )
            await self._edit_callback_message(query, prompt, confirm_markup)
            return

        if action == "delcancel":
            await self._answer_callback(query, "已取消")
            text, markup = self._build_sessions_view(chat_id)
            await self._edit_callback_message(query, text, markup)
            return

        if action == "delconfirm":
            ok, result, new_active_name = self.memory.delete_session_by_id(chat_id, target_session_id)
            if not ok:
                await self._answer_callback(query, result, show_alert=True)
                text, markup = self._build_sessions_view(chat_id)
                await self._edit_callback_message(query, text, markup)
                return
            await self._answer_callback(query, "已删除")
            text, markup = self._build_sessions_view(chat_id)
            if new_active_name:
                title = f"已删除会话“{result}”，已进入新会话：{new_active_name}"
            else:
                title = f"已删除会话“{result}”"
            display = f"{title}\n\n{text}" if markup else title
            await self._edit_callback_message(query, display, markup)
            return

    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if update.message:
            await self._send_reply(
                update,
                (
                    "命令说明：\n"
                    "/newsession [名称] 新建会话（自动切换）\n"
                    "/sessions 会话列表（切换/删除）\n"
                    "/renamesession <名称> 重命名当前会话\n"
                    "/delsession 删除会话\n"
                    "/help 查看帮助"
                ),
            )

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not update.message or not update.effective_chat:
            return

        user_text = (update.message.text or "").strip()
        if not user_text:
            return

        chat_id = str(update.effective_chat.id)
        pending = self._pending_actions.get(chat_id)
        if pending == "rename_session":
            self._pending_actions.pop(chat_id, None)
            ok, result = self.memory.rename_active_session(chat_id, user_text)
            if not ok:
                await self._send_reply(update, result)
                return
            await self._send_reply(update, f"已重命名当前会话为：{result}")
            return

        try:
            await update.message.chat.send_action(action=ChatAction.TYPING)
            active = self.memory.get_active_session(chat_id)
            all_history = self.memory.get_all_messages(chat_id)
            summary_text = str(active.get("summary", ""))
            system_prompt = self._compose_system_prompt(summary_text)
            await self._refresh_summary_if_context_large(chat_id, all_history, summary_text)
            active = self.memory.get_active_session(chat_id)
            summary_text = str(active.get("summary", ""))
            system_prompt = self._compose_system_prompt(summary_text)
            all_history = self.memory.get_all_messages(chat_id)
            history = self._select_history_for_budget(all_history, system_prompt, user_text)
            self.memory.add_message(chat_id, "user", user_text)

            if self.settings.telegram_streaming_enabled:
                answer, delivered = await self._send_stream_reply(update, history, user_text, system_prompt)
                if not answer.strip():
                    answer = EMPTY_REPLY_TEXT
                if not delivered and answer:
                    await self._send_reply(update, answer)
            else:
                answer = await self.llm.chat(
                    system_prompt=system_prompt,
                    history=history,
                    user_text=user_text,
                )
                if not answer.strip():
                    answer = EMPTY_REPLY_TEXT
                delivered = False
                await self._send_reply(update, answer)

            if answer:
                self.memory.add_message(chat_id, "assistant", answer)
                await self._refresh_summary(chat_id, force=False)
                await self._maybe_auto_title_session(chat_id, user_text, answer)
        except Exception as exc:
            logger.exception("Failed to process message")
            await self._send_reply(update, f"请求失败：{exc}")

    def _compose_system_prompt(self, session_summary: str) -> str:
        summary = (session_summary or "").strip()
        if not summary:
            return self.settings.system_prompt
        return (
            f"{self.settings.system_prompt}\n\n"
            "以下是当前会话摘要，请优先在此上下文内继续回答：\n"
            f"{summary}"
        )

    async def _refresh_summary_if_context_large(
        self,
        chat_id: str,
        all_history: list[dict[str, str]],
        summary_text: str,
    ) -> None:
        rough_tokens = self._estimate_messages_tokens(all_history) + self._estimate_text_tokens(summary_text)
        if rough_tokens < SUMMARY_FORCE_TRIGGER_TOKENS:
            return
        await self._refresh_summary(chat_id, force=True)

    async def _refresh_summary(self, chat_id: str, force: bool) -> None:
        active = self.memory.get_active_session(chat_id)
        message_count = int(active["message_count"])
        if message_count < 2:
            return
        if not force:
            if message_count < SUMMARY_TRIGGER_MESSAGES:
                return
            if message_count % SUMMARY_EVERY_MESSAGES != 0:
                return
        recent = self.memory.get_recent_messages(chat_id, SUMMARY_RECENT_MESSAGES)
        if len(recent) < 2:
            return
        try:
            summary = await self.llm.summarize_session(str(active.get("summary", "")), recent)
            if summary.strip():
                self.memory.update_active_session_summary(chat_id, summary.strip())
        except Exception:
            logger.exception("Failed to refresh session summary")

    async def _maybe_auto_title_session(self, chat_id: str, user_text: str, assistant_text: str) -> None:
        active = self.memory.get_active_session(chat_id)
        current_name = str(active.get("name", ""))
        if not current_name.startswith("新会话"):
            return
        if int(active.get("message_count", 0)) > 4:
            return
        try:
            title = await self.llm.generate_session_title(user_text=user_text, assistant_text=assistant_text)
            if not title:
                return
            self.memory.auto_rename_active_session(chat_id, title)
        except Exception:
            logger.exception("Failed to auto-title session")

    def _select_history_for_budget(
        self,
        all_history: list[dict[str, str]],
        system_prompt: str,
        user_text: str,
    ) -> list[dict[str, str]]:
        budget = (
            CONTEXT_TOKEN_BUDGET
            - CONTEXT_RESPONSE_RESERVE_TOKENS
            - CONTEXT_SYSTEM_RESERVE_TOKENS
            - self._estimate_text_tokens(system_prompt)
            - self._estimate_text_tokens(user_text)
        )
        if budget <= 0:
            return all_history[-CONTEXT_MIN_HISTORY_MESSAGES:]

        selected: list[dict[str, str]] = []
        used = 0
        for msg in reversed(all_history):
            msg_tokens = self._estimate_message_tokens(msg)
            if selected and used + msg_tokens > budget:
                break
            selected.append(msg)
            used += msg_tokens

        selected.reverse()
        if len(selected) < CONTEXT_MIN_HISTORY_MESSAGES:
            return all_history[-CONTEXT_MIN_HISTORY_MESSAGES:]
        return selected

    def _estimate_messages_tokens(self, messages: list[dict[str, str]]) -> int:
        return sum(self._estimate_message_tokens(m) for m in messages)

    def _estimate_message_tokens(self, message: dict[str, str]) -> int:
        role = message.get("role", "")
        content = message.get("content", "")
        return 6 + self._estimate_text_tokens(role) + self._estimate_text_tokens(content)

    def _estimate_text_tokens(self, text: str) -> int:
        tokens = 0.0
        for ch in text or "":
            code = ord(ch)
            if code <= 0x7F:
                tokens += 0.25
            elif 0x4E00 <= code <= 0x9FFF:
                tokens += 1.0
            else:
                tokens += 0.75
        return max(1, int(tokens + 0.999))

    async def _send_stream_reply(
        self,
        update: Update,
        history: list[dict[str, str]],
        user_text: str,
        system_prompt: str,
    ) -> tuple[str, bool]:
        if not update.message:
            return "", False

        full_text = ""
        shown_text = ""
        preview_msg: Message | None = None
        last_edit_at = 0.0
        last_typing_at = time.monotonic()
        had_stream_data = False

        try:
            async for snapshot in self.llm.chat_stream(
                system_prompt=system_prompt,
                history=history,
                user_text=user_text,
            ):
                had_stream_data = True
                full_text = snapshot
                now = time.monotonic()
                if now - last_typing_at >= 4:
                    await update.message.chat.send_action(action=ChatAction.TYPING)
                    last_typing_at = now

                if not snapshot.strip():
                    continue
                if now - last_edit_at < STREAM_EDIT_INTERVAL_SECONDS:
                    continue

                emit_index = self._next_stream_emit_index(snapshot, len(shown_text))
                if emit_index is None:
                    continue

                shown_text = snapshot[:emit_index]
                preview = self._preview_window(shown_text)
                if preview_msg is None:
                    preview_msg = await self._send_initial_preview(update, preview)
                else:
                    await self._edit_preview(preview_msg, preview)
                last_edit_at = now
        except Exception:
            if full_text.strip():
                return full_text, False
            raise

        if not had_stream_data:
            final = await self.llm.chat(
                system_prompt=system_prompt,
                history=history,
                user_text=user_text,
            )
            return final, False

        if not full_text.strip():
            return "", False

        if preview_msg is None:
            return full_text, False

        delivered = await self._finalize_stream_preview(update, preview_msg, full_text)
        return full_text, delivered

    @staticmethod
    def _preview_window(text: str) -> str:
        if len(text) <= STREAM_PREVIEW_MAX_CHARS:
            return text
        return "...\n" + text[-STREAM_PREVIEW_MAX_CHARS:]

    def _next_stream_emit_index(self, text: str, emitted_len: int) -> int | None:
        total = len(text)
        pending = total - emitted_len
        if pending <= 0:
            return None
        if pending < STREAM_BLOCK_MIN_CHARS:
            return None

        lower = emitted_len + STREAM_BLOCK_MIN_CHARS
        upper = min(total, emitted_len + STREAM_BLOCK_MAX_CHARS)
        segment = text[lower:upper]

        # Prefer paragraph boundary to reduce noisy edits in long responses.
        paragraph_idx = segment.rfind("\n\n")
        if paragraph_idx >= 0:
            return lower + paragraph_idx + 2

        newline_idx = segment.rfind("\n")
        if newline_idx >= 0:
            return lower + newline_idx + 1

        sentence_emit = None
        for match in SENTENCE_BREAK_RE.finditer(segment):
            sentence_emit = lower + match.end()
        if sentence_emit is not None:
            return sentence_emit

        if pending >= STREAM_BLOCK_MAX_CHARS:
            return upper
        return None

    async def _send_initial_preview(self, update: Update, text: str) -> Message:
        if not update.message:
            raise RuntimeError("missing message for preview")
        html_chunks, plain_chunks = self._prepare_chunks(text)
        if html_chunks:
            try:
                return await update.message.reply_text(
                    html_chunks[0],
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except BadRequest:
                pass
        return await update.message.reply_text(
            plain_chunks[0] if plain_chunks else text,
            disable_web_page_preview=True,
        )

    async def _edit_preview(self, msg: Message, text: str) -> None:
        html_chunks, plain_chunks = self._prepare_chunks(text)
        if html_chunks:
            try:
                await msg.edit_text(
                    html_chunks[0],
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                return
            except BadRequest as exc:
                if self._is_message_not_modified(exc):
                    return
                pass
        try:
            await msg.edit_text(
                plain_chunks[0] if plain_chunks else text,
                disable_web_page_preview=True,
            )
        except BadRequest as exc:
            if self._is_message_not_modified(exc):
                return
            raise

    async def _finalize_stream_preview(self, update: Update, preview_msg: Message, full_text: str) -> bool:
        html_chunks, plain_chunks = self._prepare_chunks(full_text)
        if not html_chunks:
            if not plain_chunks:
                return False
            try:
                await preview_msg.edit_text(
                    plain_chunks[0],
                    disable_web_page_preview=True,
                )
            except BadRequest as exc:
                if not self._is_message_not_modified(exc):
                    raise
            for extra in plain_chunks[1:]:
                await update.message.reply_text(extra, disable_web_page_preview=True)  # type: ignore[union-attr]
            return True

        try:
            await preview_msg.edit_text(
                html_chunks[0],
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            for extra in html_chunks[1:]:
                await update.message.reply_text(  # type: ignore[union-attr]
                    extra,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            return True
        except BadRequest as exc:
            if self._is_message_not_modified(exc):
                return True
            try:
                await preview_msg.edit_text(
                    plain_chunks[0] if plain_chunks else full_text,
                    disable_web_page_preview=True,
                )
            except BadRequest as plain_exc:
                if not self._is_message_not_modified(plain_exc):
                    raise
            for extra in plain_chunks[1:]:
                await update.message.reply_text(extra, disable_web_page_preview=True)  # type: ignore[union-attr]
            return True

    async def _send_reply(self, update: Update, text: str) -> None:
        if not update.message:
            return
        if not (text or "").strip():
            return

        html_chunks, plain_chunks = self._prepare_chunks(text)
        html_chunks = [chunk for chunk in html_chunks if chunk.strip()]
        plain_chunks = [chunk for chunk in plain_chunks if chunk.strip()]

        sent_count = 0

        if not html_chunks and not plain_chunks:
            return
        if not html_chunks:
            for plain_chunk in plain_chunks:
                await update.message.reply_text(
                    plain_chunk,
                    disable_web_page_preview=True,
                )
            return

        for idx, chunk in enumerate(html_chunks):
            try:
                await update.message.reply_text(
                    chunk,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                sent_count += 1
            except BadRequest as exc:
                logger.warning("HTML parse failed, fallback to plain text: %s", exc)
                start = min(idx, len(plain_chunks))
                remaining_plain = plain_chunks[start:] if sent_count > 0 else plain_chunks
                for plain_chunk in remaining_plain:
                    await update.message.reply_text(
                        plain_chunk,
                        disable_web_page_preview=True,
                    )
                return

    def _prepare_chunks(self, text: str) -> tuple[list[str], list[str]]:
        html_text = markdown_to_telegram_html(text)
        plain_chunks = split_plain_text(text, TELEGRAM_MAX_TEXT_LENGTH)
        try:
            html_chunks = split_telegram_html_chunks(html_text, TELEGRAM_MAX_TEXT_LENGTH)
        except Exception as exc:
            logger.warning("HTML chunk planning failed, fallback to plain text: %s", exc)
            html_chunks = []
        return html_chunks, plain_chunks

    def _build_sessions_view(self, chat_id: str) -> tuple[str, InlineKeyboardMarkup | None]:
        sessions = self.memory.list_sessions(chat_id, SESSION_BUTTON_LIMIT)
        if not sessions:
            return "当前还没有会话。", None
        buttons: list[list[InlineKeyboardButton]] = []
        for item in sessions:
            session_id = int(item["id"])
            name = str(item["name"])
            count = int(item["message_count"])
            prefix = "当前: " if bool(item["is_active"]) else ""
            display_name = name if len(name) <= 18 else f"{name[:18]}..."
            switch_text = f"{prefix}{display_name} ({count})"
            buttons.append(
                [
                    InlineKeyboardButton(switch_text, callback_data=f"sess:{session_id}"),
                    InlineKeyboardButton("删除", callback_data=f"del:{session_id}"),
                ]
            )
        return "会话列表：", InlineKeyboardMarkup(buttons)

    async def _edit_callback_message(
        self,
        query,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
        except BadRequest as exc:
            if not self._is_message_not_modified(exc):
                raise

    async def _answer_callback(self, query, text: str | None = None, show_alert: bool = False) -> None:
        try:
            if text is None:
                await query.answer()
            else:
                await query.answer(text, show_alert=show_alert)
        except BadRequest as exc:
            lowered = str(exc).lower()
            if "query is too old" in lowered or "query id is invalid" in lowered:
                return
            raise

    @staticmethod
    def _is_message_not_modified(exc: BadRequest) -> bool:
        return "message is not modified" in str(exc).lower()


def main() -> None:
    settings = load_settings()
    bot = BotApp(settings)

    async def _post_init(app: Application) -> None:
        try:
            await app.bot.set_my_commands(
                commands=[
                    BotCommand("newsession", "新建会话"),
                    BotCommand("sessions", "会话列表"),
                    BotCommand("renamesession", "重命名会话"),
                    BotCommand("delsession", "删除会话"),
                    BotCommand("help", "帮助"),
                ]
            )
        except Exception:
            logger.exception("Failed to sync telegram bot commands")

    app = Application.builder().token(settings.telegram_bot_token).post_init(_post_init).build()
    app.add_handler(CommandHandler("newsession", bot.new_session))
    app.add_handler(CommandHandler("sessions", bot.list_sessions))
    app.add_handler(CommandHandler("renamesession", bot.rename_session))
    app.add_handler(CommandHandler("delsession", bot.delete_session))
    app.add_handler(CommandHandler("help", bot.help_cmd))
    app.add_handler(CallbackQueryHandler(bot.session_action_callback, pattern=r"^(sess|del|delconfirm|delcancel):\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_text))

    app.run_polling(
        timeout=30,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
