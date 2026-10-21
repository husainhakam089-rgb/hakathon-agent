"""Swappable model layer.

The agent talks to the model only through `LLM.complete()` using a small,
provider-neutral message format. To switch provider (OpenAI, Gemini, a
hackathon platform...), write another class with the same `complete()` and
change `make_llm()`. Nothing else needs to change.

Neutral message format (what `agent.py` stores and sends):
    {"role": "user", "text": "..."}
    {"role": "assistant", "text": "...", "tool_calls": [ToolCall, ...], "raw": <provider data or None>}
    {"role": "tool", "results": [{"id": "...", "content": "<json string>", "is_error": bool}]}

`raw` lets a provider replay its own native content (e.g. Claude thinking
blocks) inside the current turn. The agent drops it when a turn is saved to
history, so providers must work without it too.
"""

import json
import logging
import re
import os
import time
from dataclasses import dataclass, field
from datetime import date

# Models that accept the server-side refusal fallback (`fallbacks: "default"`).
_FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}

log = logging.getLogger("debt_agent")


class LLMError(Exception):
    """Model call failed. `user_message` is safe to show to the shop owner (Arabic)."""

    def __init__(self, user_message: str, detail: str = "", retryable: bool = False):
        super().__init__(detail or user_message)
        self.user_message = user_message
        self.detail = detail
        # A passing problem on the provider's side (overloaded, too slow, this model's quota):
        # starting the message again, on another model if needed, may well work.
        self.retryable = retryable


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: object = None  # provider-native assistant content, replayed within the turn
    refused: bool = False


class AnthropicLLM:
    def __init__(self, model: str | None = None, api_key: str | None = None,
                 effort: str | None = None, max_tokens: int = 16000):
        import anthropic

        self._anthropic = anthropic
        self.model = model or os.getenv("MODEL_NAME") or "claude-sonnet-5-5"
        self.effort = effort or os.getenv("EFFORT") or "medium"
        self.max_tokens = max_tokens
        # The SDK also resolves credentials itself when no key is passed.
        self.client = anthropic.Anthropic(api_key=api_key or os.getenv("ANTHROPIC_API_KEY") or None)

    # ----- format conversion -----

    @staticmethod
    def _to_api_messages(messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                if m.get("raw") is not None:
                    content = m["raw"]  # native blocks incl. thinking, unchanged
                else:
                    content = []
                    if m.get("text"):
                        content.append({"type": "text", "text": m["text"]})
                    for tc in m.get("tool_calls", []):
                        content.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})
                out.append({"role": "assistant", "content": content})
            elif m["role"] == "tool":
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"],
                     **({"is_error": True} if r.get("is_error") else {})}
                    for r in m["results"]
                ]})
        return out

    # ----- main call -----

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        params = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            tools=tools,
            messages=self._to_api_messages(messages),
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},  # system + tools are stable: cache them
        )
        response = self._create(params)

        if response.stop_reason == "refusal":
            return LLMResponse(text="", refused=True)
        if response.stop_reason == "max_tokens":
            raise LLMError("الرد طلع طويل وانقطع، عيد الطلب بطريقة أبسط.", "max_tokens", retryable=True)

        text = "".join(b.text for b in response.content if b.type == "text").strip()
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in response.content if b.type == "tool_use"]
        return LLMResponse(text=text, tool_calls=calls, raw=list(response.content))

    def extract(self, system: str, text: str, file_bytes: bytes | None, mime: str | None, tool: dict) -> dict:
        """One-shot extraction from an image or PDF (or from text alone when file_bytes is
        None): the model must answer by calling `tool` (and nothing else). Returns its input."""
        import base64

        content = [{"type": "text", "text": text}]
        if file_bytes is not None:
            data = base64.standard_b64encode(file_bytes).decode()
            kind = "document" if mime == "application/pdf" else "image"
            content.insert(0, {"type": kind, "source": {"type": "base64", "media_type": mime, "data": data}})
        params = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": content}],
        )
        response = self._create(params)
        if response.stop_reason == "refusal":
            raise LLMError("النموذج رفض يقرا هذا الملف.", "refusal")
        if response.stop_reason == "max_tokens":
            raise LLMError("الصفحة بيها هواية أسطر وانقطع الاستخراج. صوّر نص الصفحة وجرب.", "max_tokens")
        for block in response.content:
            if block.type == "tool_use" and block.name == tool["name"]:
                return dict(block.input)
        raise LLMError("النموذج ما رجّع جدول، عيد المحاولة.", "no tool call")

    def _create(self, params: dict):
        a = self._anthropic
        try:
            if self.model in _FALLBACK_MODELS:
                return self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
            return self.client.messages.create(**params)
        except a.AuthenticationError as e:
            raise LLMError("مفتاح الـ API غلط أو مو موجود. تأكد من ANTHROPIC_API_KEY بملف .env", str(e))
        except a.PermissionDeniedError as e:
            raise LLMError("المفتاح ما عنده صلاحية على هذا النموذج.", str(e))
        except a.NotFoundError as e:
            raise LLMError(f"النموذج {self.model} مو موجود. تأكد من MODEL_NAME بملف .env", str(e))
        except a.RateLimitError as e:
            raise LLMError("صار ضغط على الخدمة، انتظر شوية وعيد.", str(e))
        except a.BadRequestError as e:
            raise LLMError("صار خطأ بالطلب للنموذج، عيد المحاولة.", str(e))
        except a.APIStatusError as e:
            raise LLMError("خدمة الذكاء الاصطناعي بيها مشكلة هسه، جرب بعد شوية.", str(e))
        except a.APIConnectionError as e:
            raise LLMError("ما گدرت أتصل بالإنترنت. تأكد من النت وعيد.", str(e))


_GENERATED_ID = "gen_"

# Tried in this order after GEMINI_MODEL (each has its own free daily quota).
# Each has its own free daily quota, so more of them = more messages a day. All checked on 18-10-2026
# with the full agent (instructions, 47 tools, a two-step debt in Iraqi Arabic): right tools, right
# reply, 4-7 seconds. gemma-4-26b is last: it works but takes about two minutes. Left out:
# gemini-2.5-flash(-lite) (closed to new users) and gemma-4-31b (16,000 input tokens a minute).
DEFAULT_GEMINI_FALLBACKS = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.7-flash", "gemini-3.8-flash",
                            "gemini-3-flash-preview", "gemini-flash-latest", "gemini-3.5-flash-lite",
                            "gemini-3.1-flash-lite", "gemini-flash-lite-latest", "gemini-3.1-flash-lite-preview",
                            "gemma-4-26b-a4b-it"]
ALL_QUOTA_USED = ("خلصت حصة اليوم المجانية لكل نماذج Gemini. جرب باچر، "
                  "أو فعّل الدفع بحساب Google AI Studio حتى ما توگف.")


def _real_id(call_id: str):
    """Ids we made up locally are never sent back to Gemini."""
    return None if call_id.startswith(_GENERATED_ID) else call_id


class GeminiLLM:
    """Google Gemini via the official `google-genai` SDK."""

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 max_tokens: int = 8192):
        import httpx
        from google import genai
        from google.genai import errors, types

        self._httpx, self._errors, self.types = httpx, errors, types
        self.model = model or os.getenv("GEMINI_MODEL") or "gemini-3.6-flash"
        # Free tier gives each model its own daily quota. When the model in use runs
        # out (or stays overloaded), switch to the next one in this list.
        fallbacks = os.getenv("GEMINI_FALLBACK_MODELS")
        fallbacks = DEFAULT_GEMINI_FALLBACKS if fallbacks is None else fallbacks.split(",")
        self.models = list(dict.fromkeys([self.model] + [m.strip() for m in fallbacks if m.strip()]))
        self._out_of_quota: dict[str, date] = {}  # model -> day its daily quota ran out
        self.max_tokens = max_tokens
        # How much the model thinks before answering: minimal / low / medium / high.
        # Each message needs 2-3 model calls, so less thinking = noticeably faster replies.
        self.thinking = (os.getenv("GEMINI_THINKING") or "").strip().upper() or None
        key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise LLMError("ما كو مفتاح Gemini. حط GEMINI_API_KEY بملف .env", "missing GEMINI_API_KEY")
        # Gemini often answers 503 "high demand" for a moment: retry those (then
        # switch model). 429 is not retried: on the free tier it is usually the daily quota.
        self.http_options = types.HttpOptions(
            # ms, per attempt. A timeout is retried too, so a stuck model costs 2 x 40 s at most
            # before the next model is tried (it used to be 3 x 60 s).
            timeout=40_000,
            retry_options=types.HttpRetryOptions(
                attempts=2, initial_delay=1.0, max_delay=4.0,
                http_status_codes=[408, 500, 502, 503, 504]))
        self.client = genai.Client(api_key=key, http_options=self.http_options)

    # ----- format conversion -----

    def _tools(self, tools: list[dict]):
        t = self.types
        decls = []
        for tool in tools:
            schema = tool.get("input_schema") or {}
            kwargs = {"name": tool["name"], "description": tool.get("description", "")}
            if schema.get("properties"):  # tools without inputs get no schema
                kwargs["parameters_json_schema"] = schema
            decls.append(t.FunctionDeclaration(**kwargs))
        return [t.Tool(function_declarations=decls)]

    def _to_contents(self, messages: list[dict]):
        t = self.types
        out = []
        for m in messages:
            if m["role"] == "user":
                out.append(t.Content(role="user", parts=[t.Part(text=m["text"])]))
            elif m["role"] == "assistant":
                if m.get("raw") is not None:
                    out.append(m["raw"])  # native content incl. thought signatures, unchanged
                    continue
                parts = [t.Part(text=m["text"])] if m.get("text") else []
                parts += [t.Part(function_call=t.FunctionCall(id=_real_id(tc.id), name=tc.name, args=tc.input))
                          for tc in m.get("tool_calls", [])]
                out.append(t.Content(role="model", parts=parts))
            elif m["role"] == "tool":
                out.append(t.Content(role="user", parts=[
                    t.Part(function_response=t.FunctionResponse(
                        id=_real_id(r["id"]), name=r["name"], response=json.loads(r["content"])))
                    for r in m["results"]
                ]))
        return out

    # ----- main call -----

    MAX_RATE_LIMIT_WAIT = 60  # seconds: a per-minute window never needs more

    @staticmethod
    def _retry_delay(e) -> float | None:
        """Seconds Gemini asks us to wait (RetryInfo in the error details), if any."""
        error = e.details.get("error", e.details) if isinstance(e.details, dict) else {}
        for d in error.get("details") or []:
            if str(d.get("@type", "")).endswith("RetryInfo"):
                try:
                    return float(str(d.get("retryDelay", "")).rstrip("s"))
                except ValueError:
                    return None
        return None

    def _generate(self, contents, config, can_switch: bool):
        """Call the current model. At the start of a turn (`can_switch`), move on to the
        next model when this one is out of daily quota, rate limited, overloaded or
        unavailable; wait out a per-minute limit only when no model is free.
        Mid-turn we stay on one model: its native content is replayed in the request."""
        errors = self._errors
        today = date.today()
        if can_switch:
            candidates = [self.model] + [m for m in self.models if m != self.model]
            candidates = [m for m in candidates if self._out_of_quota.get(m) != today]
        else:
            candidates = [self.model]
        rate_limited = []  # (model, error) hit by a per-minute limit
        last_error = None
        for model in candidates:
            try:
                response = self._generate_with(model, contents, config, wait=not can_switch)
                self.model = model
                return response
            except errors.ClientError as e:
                if e.code == 429 and "PerDay" in str(e):
                    self._out_of_quota[model] = today
                elif e.code == 429:
                    rate_limited.append((model, e))
                elif e.code != 404:
                    raise
                last_error = e
            except errors.ServerError as e:  # still overloaded after retries
                last_error = e
            except self._httpx.TimeoutException as e:  # this model is too slow right now: try the next
                last_error = e
        if rate_limited:  # every usable model is busy this minute: wait for the first one
            model, first_error = rate_limited[0]
            response = self._generate_with(model, contents, config, wait=True, first_error=first_error)
            self.model = model
            return response
        if last_error is None:  # every model already known to be out of quota today
            raise LLMError(ALL_QUOTA_USED, "all Gemini models out of daily quota")
        raise last_error

    def _generate_with(self, model, contents, config, wait: bool, first_error=None):
        """Free tier allows only a few requests per minute. With `wait`, when Gemini
        says how long to wait, wait and retry, up to MAX_RATE_LIMIT_WAIT in total."""
        waited = 0.0
        error = first_error
        while True:
            if error is not None:
                delay = self._retry_delay(error) if error.code == 429 and "PerDay" not in str(error) else None
                if not wait or delay is None or waited + delay > self.MAX_RATE_LIMIT_WAIT:
                    raise error
                time.sleep(delay + 1)
                waited += delay + 1
            try:
                return self.client.models.generate_content(model=model, contents=contents, config=config)
            except self._errors.ClientError as e:
                error = e

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        t = self.types
        config = t.GenerateContentConfig(
            system_instruction=system,
            tools=self._tools(tools),
            # We run the tools ourselves in agent.py
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
            max_output_tokens=self.max_tokens,
            thinking_config=t.ThinkingConfig(thinking_level=self.thinking) if self.thinking else None,
        )
        # Native content in the request = we are mid-turn on the current model.
        can_switch = not any(m.get("raw") is not None for m in messages)
        response = self._safe_generate(self._to_contents(messages), config, can_switch)

        if not response.candidates:
            return LLMResponse(text="", refused=True)  # prompt blocked
        candidate = response.candidates[0]
        reason = candidate.finish_reason
        if reason == t.FinishReason.MAX_TOKENS:
            raise LLMError("الرد طلع طويل وانقطع، عيد الطلب بطريقة أبسط.", "max_tokens", retryable=True)
        parts = (candidate.content.parts if candidate.content else None) or []
        if not parts and reason in (t.FinishReason.SAFETY, t.FinishReason.PROHIBITED_CONTENT,
                                    t.FinishReason.BLOCKLIST, t.FinishReason.SPII):
            return LLMResponse(text="", refused=True)

        text = "".join(p.text for p in parts if p.text and not p.thought).strip()
        calls = []
        for i, p in enumerate(parts):
            if p.function_call:
                fc = p.function_call
                # Some models give no id; then tool results are matched by name only.
                call_id = fc.id or f"{_GENERATED_ID}{len(messages)}_{i}"
                calls.append(ToolCall(call_id, fc.name, dict(fc.args or {})))
        return LLMResponse(text=text, tool_calls=calls, raw=candidate.content)

    def extract(self, system: str, text: str, file_bytes: bytes | None, mime: str | None, tool: dict) -> dict:
        """One-shot extraction from an image or PDF (or from text alone when file_bytes is
        None): the model must answer by calling `tool` (and nothing else). Returns its args."""
        t = self.types
        config = t.GenerateContentConfig(
            system_instruction=system,
            tools=self._tools([tool]),
            tool_config=t.ToolConfig(function_calling_config=t.FunctionCallingConfig(
                mode=t.FunctionCallingConfigMode.ANY, allowed_function_names=[tool["name"]])),
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
            max_output_tokens=self.max_tokens,
            thinking_config=t.ThinkingConfig(thinking_level=self.thinking) if self.thinking else None,
        )
        parts = [t.Part(text=text)]
        if file_bytes is not None:
            parts.insert(0, t.Part.from_bytes(data=file_bytes, mime_type=mime))
        contents = [t.Content(role="user", parts=parts)]
        response = self._safe_generate(contents, config, can_switch=True)
        candidate = response.candidates[0] if response.candidates else None
        if candidate is None:
            raise LLMError("النموذج رفض يقرا هذا الملف.", "blocked")
        if candidate.finish_reason == t.FinishReason.MAX_TOKENS:
            raise LLMError("الصفحة بيها هواية أسطر وانقطع الاستخراج. صوّر نص الصفحة وجرب.", "max_tokens")
        for p in (candidate.content.parts if candidate.content else None) or []:
            if p.function_call and p.function_call.name == tool["name"]:
                return dict(p.function_call.args or {})
        raise LLMError("النموذج ما رجّع جدول، عيد المحاولة.", f"no tool call ({candidate.finish_reason})")

    def _safe_generate(self, contents, config, can_switch: bool):
        """_generate with every failure turned into an LLMError the shop owner can read."""
        errors = self._errors
        try:
            return self._generate(contents, config, can_switch)
        except errors.ClientError as e:
            if e.code == 429:
                if "PerDay" in str(e):
                    if any(self._out_of_quota.get(m) != date.today() for m in self.models):
                        raise LLMError("خلصت حصة النموذج بنص الطلب. عيد رسالتك وراح أستعمل نموذج ثاني.", str(e),
                                       retryable=True)
                    raise LLMError(ALL_QUOTA_USED, str(e))
                raise LLMError("خلصت حصة Gemini المجانية هسه، انتظر دقيقة وعيد.", str(e))
            if e.code in (401, 403) or "API_KEY" in str(e):
                raise LLMError("مفتاح Gemini غلط أو ما عنده صلاحية. تأكد من GEMINI_API_KEY بملف .env", str(e))
            if e.code == 404:
                raise LLMError(f"النموذج {self.model} مو موجود. تأكد من GEMINI_MODEL بملف .env", str(e))
            raise LLMError("صار خطأ بالطلب للنموذج، عيد المحاولة.", str(e))
        except errors.APIError as e:
            raise LLMError("خدمة Gemini بيها زحمة أو مشكلة هسه (مو من عندك)، جرب بعد شوية.", str(e), retryable=True)
        except self._httpx.TimeoutException as e:
            # The request reached Google but no answer came in time: their side is slow, not the owner's internet.
            raise LLMError("Gemini تأخر بالرد (زحمة عندهم، مو مشكلة النت عندك). عيد بعد شوية.", f"timeout: {e!r}",
                           retryable=True)
        except self._httpx.TransportError as e:
            raise LLMError("ما گدرت أوصل لـ Gemini. تأكد من النت عندك وعيد.", f"{type(e).__name__}: {e}")


# ---------- Groq: the backup when Gemini's free quota runs out ----------

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Online on 18-10-2026 Groq answered 404 for llama-3.3-70b, llama-4-scout, qwen3-32b and kimi-k2: only
# gpt-oss is left. GROQ_MODEL can still name another one.
DEFAULT_GROQ_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
GROQ_REPLY_TOKENS = 2000  # a sentence or a tool call plus gpt-oss's short thinking; the free tier counts this room too
GROQ_HISTORY = 4          # earlier messages kept (two exchanges), plus all of this message's work
GROQ_WAIT_SHORT = 10      # a wait this short is spent on the same model
GROQ_WAIT_MAX = 60        # seconds worth waiting for the minute's limit to free up, once every model is busy


def _wait_seconds(r) -> float | None:
    """How long Groq says to wait before its per-minute limit frees up, when that is short
    enough to wait for in a chat ("try again in 12.5s" or a Retry-After header); None otherwise."""
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", r.text or "")
    seconds = (int(m.group(1) or 0) * 60 + float(m.group(2))) if m else None
    if seconds is None:
        try:
            seconds = float(r.headers.get("retry-after", ""))
        except ValueError:
            return None
    return seconds + 0.5 if 0 <= seconds <= GROQ_WAIT_MAX else None


def _made_up_tool_text(error_text: str) -> str | None:
    """Groq refuses a call to a tool that was not offered (21-10-2026 online: gpt-oss called
    "ask_user" with {"question": "تقصد 187,500؟"}). When that call carries a question or message
    for the owner, it is the reply; None otherwise (the next model is asked)."""
    try:
        err = json.loads(error_text).get("error") or {}
        if err.get("code") != "tool_use_failed":
            return None
        call = json.loads(err.get("failed_generation") or "{}")
        args = call.get("arguments") or call.get("parameters") or {}
        if isinstance(args, str):
            args = json.loads(args)
    except (ValueError, AttributeError, TypeError):
        return None
    for key in ("question", "message", "text", "reply", "content"):
        if isinstance(args, dict) and isinstance(args.get(key), str) and args[key].strip():
            return args[key].strip()
    return None


def _fits_per_minute(error_text: str, max_tokens) -> int | None:
    """Groq's free tier refuses a request whose message plus reply room is over its tokens a
    minute ("Limit 8000, Requested 10062"). The reply room that would fit, if there is a useful
    amount of it (our replies are short); None otherwise."""
    m = re.search(r"Limit (\d+), Requested (\d+)", error_text or "")
    if not m or not max_tokens:
        return None
    limit, requested = int(m.group(1)), int(m.group(2))
    fit = max_tokens - (requested - limit) - 200  # a little margin: the count is approximate
    return fit if 600 <= fit < max_tokens else None


class GroqLLM:
    """Groq's OpenAI-compatible chat API (free tier: far more requests a day than Gemini's).
    Same complete() as the others. Images are read by Gemini only: extract() here handles
    images with a vision model when one is set (GROQ_VISION_MODEL), never PDFs."""

    def __init__(self, model: str | None = None, api_key: str | None = None, max_tokens: int = GROQ_REPLY_TOKENS,
                 client=None):
        import httpx

        self._httpx = httpx
        self.key = api_key or os.getenv("GROQ_API_KEY")
        if not self.key:
            raise LLMError("ما كو مفتاح Groq. حط GROQ_API_KEY بملف .env", "missing GROQ_API_KEY")
        first = model or os.getenv("GROQ_MODEL") or DEFAULT_GROQ_MODELS[0]
        self.models = list(dict.fromkeys([first] + DEFAULT_GROQ_MODELS))
        self.model = first
        self.max_tokens = max_tokens
        self.client = client or httpx.Client(timeout=40)

    @staticmethod
    def _tools(tools: list[dict]) -> list[dict]:
        return [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}} for t in tools]

    @staticmethod
    def _light_tools(tools: list[dict]) -> list[dict]:
        """The same tools, lighter: each description cut to its first sentence and no field
        descriptions (names, types, choices and required fields stay, so calls stay valid)."""
        def strip(schema):
            if isinstance(schema, dict):
                return {k: strip(v) for k, v in schema.items() if k != "description"}
            if isinstance(schema, list):
                return [strip(v) for v in schema]
            return schema
        out = []
        for t in GroqLLM._tools(tools):
            fn = t["function"]
            first = re.split(r"(?<=[.؟!:])\s", fn["description"] or "", maxsplit=1)[0][:140]
            out.append({"type": "function", "function": {**fn, "description": first,
                                                         "parameters": strip(fn["parameters"])}})
        return out

    @staticmethod
    def _light_system(system: str) -> str:
        """The short rules in place of the full prompt; the memory block after it stays."""
        from app.prompts import COMPACT_SYSTEM_PROMPT, SYSTEM_PROMPT
        return COMPACT_SYSTEM_PROMPT + system[len(SYSTEM_PROMPT):] if system.startswith(SYSTEM_PROMPT) else system

    @staticmethod
    def _light_messages(messages: list[dict]) -> list[dict]:
        """This message with all its tool work, and only the last few earlier messages."""
        work = [i for i, m in enumerate(messages) if m["role"] == "tool" or m.get("tool_calls")]
        users = [i for i, m in enumerate(messages) if m["role"] == "user" and (not work or i < work[0])]
        start = users[-1] if users else 0  # where this message begins
        earlier = messages[:start][-GROQ_HISTORY:]
        while earlier and earlier[0]["role"] != "user":
            earlier = earlier[1:]
        return earlier + messages[start:]

    @staticmethod
    def _messages(system: str, messages: list[dict]) -> list[dict]:
        out = [{"role": "system", "content": system}]
        for m in messages:  # the neutral format; any provider's raw content is ignored
            if m["role"] == "user":
                out.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                msg = {"role": "assistant", "content": m.get("text") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [{"id": tc.id, "type": "function", "function": {
                        "name": tc.name, "arguments": json.dumps(tc.input, ensure_ascii=False)}}
                        for tc in m["tool_calls"]]
                out.append(msg)
            elif m["role"] == "tool":
                for r in m["results"]:
                    out.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"]})
        return out

    def _send(self, body: dict, model: str, max_tokens=None):
        extra = {"max_tokens": max_tokens} if max_tokens else {}
        try:
            return self.client.post(GROQ_URL, json={**body, "model": model, **extra},
                                    headers={"Authorization": f"Bearer {self.key}"})
        except self._httpx.TimeoutException as e:
            raise LLMError("Groq تأخر بالرد. عيد بعد شوية.", f"timeout: {e!r}", retryable=True)
        except self._httpx.TransportError as e:
            raise LLMError("ما گدرت أوصل لـ Groq. تأكد من النت عندك وعيد.", f"{type(e).__name__}: {e}")

    def _post(self, body: dict) -> dict:
        last = None
        tried = []  # every model's answer, so a failure says why each one refused (not only the last)
        later = []  # (seconds, model): models whose minute frees up too late to wait for at once
        for model in [self.model] + [m for m in self.models if m != self.model]:
            r = self._send(body, model)
            if r.status_code == 429 and (wait := _wait_seconds(r)) is not None:
                if wait <= GROQ_WAIT_SHORT:
                    # this minute's tokens are used up (a message often needs two calls): wait, once
                    time.sleep(wait)
                    r = self._send(body, model)
                else:
                    later.append((wait, model))  # each model has its own minute: try the next one first
            if r.status_code == 413 and (fit := _fits_per_minute(r.text, body.get("max_tokens"))):
                # the free tier counts the reply room too: ask again with only the room that fits
                r = self._send(body, model, fit)
            if r.status_code == 400 and (asked := _made_up_tool_text(r.text)):
                # it called a tool that does not exist to ask the owner something: that question is the reply
                self.model = model
                return {"choices": [{"message": {"content": asked}, "finish_reason": "stop"}]}
            if r.status_code == 200:
                self.model = model
                return r.json()
            last = f"{r.status_code}: {r.text[:300]}"
            tried.append(f"{model} → {r.status_code}: {r.text[:160]}")
            if r.status_code == 413:
                continue  # too big for this model's free per-minute limit: the next model has more room
            if r.status_code in (401, 403):
                raise LLMError("مفتاح Groq غلط. تأكد من GROQ_API_KEY بملف .env", last)
            if r.status_code in (404, 429, 500, 502, 503) or r.status_code == 400 and (
                    "model" in r.text[:200].lower() or "tool_use_failed" in r.text):
                continue  # this model is gone, busy or out of quota: the next one
            raise LLMError("صار خطأ بالطلب لـ Groq، عيد المحاولة.", last)
        if later:
            # every model is busy this minute (messages sent one after another): wait for the
            # soonest one rather than tell the owner to try again
            wait, model = min(later)
            time.sleep(wait)
            r = self._send(body, model)
            if r.status_code == 200:
                self.model = model
                return r.json()
            tried.append(f"{model} (after waiting) → {r.status_code}: {r.text[:160]}")
        raise LLMError("Groq هم مشغول أو خلصت حصته هسه. جرب بعد شوية.", " | ".join(tried) or last or "",
                       retryable=True)

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        # Light, so a message fits the free tier's tokens a minute (18-10-2026: refused at ~10,000)
        body = {"messages": self._messages(self._light_system(system), self._light_messages(messages)),
                "max_tokens": self.max_tokens}
        if "gpt-oss" in self.model:
            # it thinks before answering and the thinking counts in max_tokens: online on
            # 21-10-2026 the reply was cut off ("max_tokens"). Our turns need little thinking.
            body["reasoning_effort"] = "low"
        if tools:
            body.update(tools=self._light_tools(tools), tool_choice="auto")
        choice = self._post(body)["choices"][0]
        if choice.get("finish_reason") == "length":
            raise LLMError("الرد طلع طويل وانقطع، عيد الطلب بطريقة أبسط.", "max_tokens", retryable=True)
        msg = choice.get("message") or {}
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(tc.get("id") or f"groq_{len(messages)}_{i}", fn.get("name", ""), args))
        return LLMResponse(text=(msg.get("content") or "").strip(), tool_calls=calls)

    def extract(self, system: str, text: str, file_bytes: bytes | None, mime: str | None, tool: dict) -> dict:
        vision = os.getenv("GROQ_VISION_MODEL")
        if file_bytes is not None and (not vision or not (mime or "").startswith("image/")):
            raise LLMError("قراية الصور تحتاج Gemini، وهو مو متوفر هسه. جرب بعد شوية.", "groq: no vision")
        import base64
        content = [{"type": "text", "text": text}]
        if file_bytes is not None:
            content.insert(0, {"type": "image_url", "image_url": {
                "url": f"data:{mime};base64,{base64.b64encode(file_bytes).decode()}"}})
        body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
                "tools": self._tools([tool]), "max_tokens": self.max_tokens,
                "tool_choice": {"type": "function", "function": {"name": tool["name"]}}}
        if vision and file_bytes is not None:
            self.model = vision
        msg = self._post(body)["choices"][0].get("message") or {}
        for tc in msg.get("tool_calls") or []:
            if (tc.get("function") or {}).get("name") == tool["name"]:
                return json.loads(tc["function"].get("arguments") or "{}")
        raise LLMError("النموذج ما رجّع جدول، عيد المحاولة.", "groq: no tool call")


class FallbackLLM:
    """Gemini first; when it fails (quota, busy, down), the same call goes to the backup.
    After Gemini says every model is out of today's quota, the backup is used for the rest of
    the day; after another failure, for 5 minutes. The agent's neutral messages work for both,
    so a turn can even switch halfway."""

    COOL_DOWN = 300  # seconds

    def __init__(self, primary, backup):
        self.primary, self.backup = primary, backup
        self._skip_until = 0.0
        self._skip_day = None

    @property
    def model(self):
        return getattr(self._current(), "model", "")

    def _current(self):
        if self._skip_day == date.today() or time.time() < self._skip_until:
            return self.backup
        return self.primary

    def _call(self, name, *args):
        if self._current() is self.primary:
            try:
                return getattr(self.primary, name)(*args)
            except LLMError as e:
                log.warning("Gemini failed, using the backup: %s", e.detail)
                if e.user_message == ALL_QUOTA_USED:
                    self._skip_day = date.today()
                else:
                    self._skip_until = time.time() + self.COOL_DOWN
                return getattr(self.backup, name)(*args)
        try:
            return getattr(self.backup, name)(*args)
        except LLMError as e:
            if self._skip_day == date.today():
                raise  # Gemini is out for the day: nothing else to ask
            # Gemini was only resting after one failure (busy for a moment): ask it again
            # rather than give the owner the backup's error
            log.warning("Backup failed while Gemini rested, asking Gemini again: %s", e.detail)
            try:
                answer = getattr(self.primary, name)(*args)
            except LLMError:
                raise e from None
            self._skip_until = 0.0
            return answer

    def complete(self, system, messages, tools):
        return self._call("complete", system, messages, tools)

    def extract(self, system, text, file_bytes, mime, tool):
        # pages are read by Gemini when it can; the backup only gets what it can read
        try:
            return self.primary.extract(system, text, file_bytes, mime, tool)
        except LLMError:
            return self.backup.extract(system, text, file_bytes, mime, tool)


def make_llm():
    """Factory: the one place to change when switching provider (LLM_PROVIDER in .env).
    Gemini by default; then any more Gemini keys; with GROQ_API_KEY set, Groq takes over when
    every Gemini key's quota runs out."""
    provider = (os.getenv("LLM_PROVIDER") or "gemini").strip().lower()
    if provider == "anthropic":
        return AnthropicLLM()
    if provider == "groq":
        return GroqLLM()
    # More Gemini keys (GEMINI_API_KEY_2, _3, ...) are asked in turn, each with its own free quota
    chain = [GeminiLLM()] + [GeminiLLM(api_key=os.getenv(f"GEMINI_API_KEY_{i}")) for i in range(2, 10)
                             if os.getenv(f"GEMINI_API_KEY_{i}")]
    if os.getenv("GROQ_API_KEY"):
        chain.append(GroqLLM())
    llm = chain.pop()
    while chain:
        llm = FallbackLLM(chain.pop(), llm)
    return llm
