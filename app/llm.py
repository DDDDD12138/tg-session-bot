from urllib.parse import urlparse

from openai import AsyncOpenAI


class LLMClient:
    def __init__(self, api_key: str, base_url: str | None, model: str) -> None:
        self._clients = [AsyncOpenAI(api_key=api_key, base_url=url) for url in self._build_base_url_candidates(base_url)]
        self.model = model
        self._preferred_client_index = 0

    async def chat(self, system_prompt: str, history: list[dict[str, str]], user_text: str) -> str:
        input_messages = [*history, {"role": "user", "content": user_text}]
        last_error: Exception | None = None

        for index, client in self._iter_clients_from_preferred():
            try:
                if self._supports_responses(client):
                    text = await self._chat_with_responses(client, input_messages, system_prompt)
                else:
                    text = await self._chat_with_completions(client, input_messages, system_prompt)
                if text:
                    self._preferred_client_index = index
                    return text
                last_error = RuntimeError("Empty LLM response")
            except Exception as exc:
                last_error = exc

        raise RuntimeError(f"LLM request failed after base_url fallbacks: {last_error}")

    async def chat_stream(self, system_prompt: str, history: list[dict[str, str]], user_text: str):
        input_messages = [*history, {"role": "user", "content": user_text}]
        last_error: Exception | None = None

        for index, client in self._iter_clients_from_preferred():
            try:
                text = ""
                completed_text = ""
                if self._supports_responses(client):
                    stream = await client.responses.create(
                        model=self.model,
                        input=input_messages,
                        instructions=system_prompt,
                        temperature=0.7,
                        timeout=60,
                        stream=True,
                    )
                    async for event in stream:
                        delta, final_candidate = self._extract_stream_event(event)
                        if delta:
                            text += delta
                            yield text
                        if final_candidate:
                            completed_text = final_candidate
                else:
                    completion_messages = [{"role": "system", "content": system_prompt}, *input_messages]
                    stream = await client.chat.completions.create(
                        model=self.model,
                        messages=completion_messages,
                        temperature=0.7,
                        timeout=60,
                        stream=True,
                    )
                    async for chunk in stream:
                        delta = self._extract_completion_delta(chunk)
                        if delta:
                            text += delta
                            yield text
                    completed_text = text

                final_text = text.strip() or completed_text.strip()
                if final_text:
                    if not text.strip():
                        yield final_text
                    self._preferred_client_index = index
                    return
                last_error = RuntimeError("Empty streamed response")
            except Exception as exc:
                last_error = exc

        raise RuntimeError(f"LLM stream request failed after base_url fallbacks: {last_error}")

    @staticmethod
    def _supports_responses(client: AsyncOpenAI) -> bool:
        responses = getattr(client, "responses", None)
        return responses is not None and callable(getattr(responses, "create", None))

    async def _chat_with_responses(
        self,
        client: AsyncOpenAI,
        input_messages: list[dict[str, str]],
        system_prompt: str,
    ) -> str:
        response = await client.responses.create(
            model=self.model,
            input=input_messages,
            instructions=system_prompt,
            temperature=0.7,
            timeout=60,
        )
        return self._extract_responses_text(response)

    async def _chat_with_completions(
        self,
        client: AsyncOpenAI,
        input_messages: list[dict[str, str]],
        system_prompt: str,
    ) -> str:
        completion_messages = [{"role": "system", "content": system_prompt}, *input_messages]
        response = await client.chat.completions.create(
            model=self.model,
            messages=completion_messages,
            temperature=0.7,
            timeout=60,
        )
        return self._extract_completion_text(response)

    async def summarize_session(self, current_summary: str, recent_messages: list[dict[str, str]]) -> str:
        summary_prompt = (
            "你是会话摘要助手。请将当前会话压缩为简洁摘要，便于后续继续对话。\n"
            "要求：\n"
            "1) 用中文输出；\n"
            "2) 控制在 8 行以内；\n"
            "3) 覆盖：用户目标、已确认事实、未完成事项、关键约束；\n"
            "4) 只保留与后续对话相关的信息。"
        )
        seed = current_summary.strip()
        seed_message = {"role": "user", "content": f"历史摘要（可为空）：\n{seed}"} if seed else None
        messages = [seed_message] if seed_message else []
        messages.extend(recent_messages)
        return await self.chat(system_prompt=summary_prompt, history=messages, user_text="请输出更新后的摘要。")

    async def generate_session_title(self, user_text: str, assistant_text: str = "") -> str:
        title_prompt = (
            "你是标题生成助手。根据对话内容生成一个简短中文会话标题。\n"
            "要求：\n"
            "1) 8-16个中文字符优先；\n"
            "2) 不要标点，不要引号，不要前缀；\n"
            "3) 直接输出标题本身。"
        )
        history = [{"role": "user", "content": user_text}]
        if assistant_text.strip():
            history.append({"role": "assistant", "content": assistant_text[:300]})
        raw = await self.chat(
            system_prompt=title_prompt,
            history=history,
            user_text="请输出会话标题。",
        )
        cleaned = raw.strip().replace("\n", " ")
        cleaned = cleaned.strip("`\"'“”‘’：:;,.!?！？()（）[]【】")
        return cleaned[:24].strip()

    def _iter_clients_from_preferred(self) -> list[tuple[int, AsyncOpenAI]]:
        total = len(self._clients)
        if total <= 1:
            return list(enumerate(self._clients))
        start = self._preferred_client_index % total
        order = list(range(start, total)) + list(range(0, start))
        return [(idx, self._clients[idx]) for idx in order]

    @staticmethod
    def _extract_responses_text(response: object) -> str:
        if isinstance(response, str):
            return ""

        output_text = getattr(response, "output_text", None)
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        output_items = getattr(response, "output", None)
        if not isinstance(output_items, list):
            return ""

        pieces: list[str] = []
        for item in output_items:
            if not isinstance(item, dict):
                item_type = getattr(item, "type", "")
                if item_type != "message":
                    continue
                content = getattr(item, "content", None)
                if not isinstance(content, list):
                    continue
                for part in content:
                    part_type = getattr(part, "type", "")
                    if part_type in ("output_text", "text"):
                        text = getattr(part, "text", "")
                        if isinstance(text, str) and text:
                            pieces.append(text)
                continue

            if item.get("type") != "message":
                continue
            for part in item.get("content", []):
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    text = part.get("text", "")
                    if isinstance(text, str) and text:
                        pieces.append(text)

        return "".join(pieces).strip()

    @staticmethod
    def _extract_completion_text(response: object) -> str:
        choices = getattr(response, "choices", None)
        if not isinstance(choices, list) or not choices:
            return ""
        message = getattr(choices[0], "message", None)
        if message is None:
            return ""
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            pieces: list[str] = []
            for part in content:
                text = getattr(part, "text", None)
                if isinstance(text, str) and text:
                    pieces.append(text)
                elif isinstance(part, dict):
                    maybe_text = part.get("text", "")
                    if isinstance(maybe_text, str) and maybe_text:
                        pieces.append(maybe_text)
            return "".join(pieces).strip()
        return ""

    @staticmethod
    def _extract_completion_delta(chunk: object) -> str:
        choices = getattr(chunk, "choices", None)
        if not isinstance(choices, list) or not choices:
            return ""
        delta = getattr(choices[0], "delta", None)
        if delta is None:
            return ""
        content = getattr(delta, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            pieces: list[str] = []
            for part in content:
                text = getattr(part, "text", None)
                if isinstance(text, str) and text:
                    pieces.append(text)
                elif isinstance(part, dict):
                    maybe_text = part.get("text", "")
                    if isinstance(maybe_text, str) and maybe_text:
                        pieces.append(maybe_text)
            return "".join(pieces)
        return ""

    def _extract_stream_event(self, event: object) -> tuple[str, str]:
        if isinstance(event, dict):
            event_type = str(event.get("type", ""))
            if event_type == "response.output_text.delta":
                delta = event.get("delta")
                return (delta if isinstance(delta, str) else "", "")
            if event_type in ("response.completed", "response.done"):
                response = event.get("response")
                return ("", self._extract_responses_text(response))
            return ("", "")

        event_type = str(getattr(event, "type", ""))
        if event_type == "response.output_text.delta":
            delta = getattr(event, "delta", "")
            return (delta if isinstance(delta, str) else "", "")
        if event_type in ("response.completed", "response.done"):
            response = getattr(event, "response", None)
            return ("", self._extract_responses_text(response))
        return ("", "")

    @staticmethod
    def _build_base_url_candidates(base_url: str | None) -> list[str | None]:
        if not base_url:
            return [None]

        raw = base_url.strip().rstrip("/")
        candidates: list[str | None] = [raw]

        parsed = urlparse(raw)
        path = parsed.path.rstrip("/")
        if path in ("", "/"):
            candidates.append(f"{raw}/v1")

        # Deduplicate while preserving order.
        return list(dict.fromkeys(candidates))
