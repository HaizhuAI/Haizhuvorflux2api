"""OpenAI-compatible API: /v1/models, /v1/chat/completions."""
from __future__ import annotations

import asyncio
import json
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import hashlib
import re

import config
import db
import vorflux
from pool import pool, AccountCtx

router = APIRouter()

_model_cache: dict = {"ts": 0.0, "data": None}
MODEL_CACHE_TTL = 300.0


# --------------------------------------------------------------------- auth
async def api_auth(authorization: str = Header(default="")) -> None:
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, {"error": {"message": "Missing API key",
                                            "type": "invalid_request_error"}})
    if db.api_key_valid(token):
        return
    # built-in playground uses the admin session token
    from admin_api import _admin_sessions, _admin_token
    if token in _admin_sessions or token == _admin_token():
        return
    raise HTTPException(401, {"error": {"message": "Invalid API key",
                                        "type": "invalid_request_error"}})


def openai_error(status: int, message: str, etype: str = "server_error",
                 code: str = "internal_error"):
    return JSONResponse({"error": {"message": message, "type": etype,
                                   "code": code}}, status_code=status)


# ------------------------------------------------------------------- models
async def _fetch_models() -> dict:
    """Union of available models across healthy accounts (cached)."""
    if _model_cache["data"] and time.time() - _model_cache["ts"] < MODEL_CACHE_TTL:
        return _model_cache["data"]
    models: dict[str, dict] = {}
    default_key = ""
    for snap in pool.snapshot():
        if not snap["healthy"]:
            continue
        ctx = pool.get_ctx(snap["id"])
        try:
            sess = await pool.session(ctx)
            data = await sess.list_models()
        except vorflux.VorfluxError:
            continue
        default_key = default_key or data.get("defaultModelKey") or ""
        for m in data.get("models") or []:
            key = m.get("modelKey")
            if key and (m.get("isAvailable") or key not in models):
                models[key] = m
        if models:
            break  # first healthy account is authoritative enough
    if not models:
        # static fallback so clients can still address the gateway
        models = {"vorflux-auto": {"modelKey": "", "displayName": "Vorflux Auto",
                                   "family": "vorflux", "isAvailable": True}}
    data = {"models": list(models.values()), "default": default_key}
    _model_cache.update(ts=time.time(), data=data)
    return data


@router.get("/v1/models", dependencies=[Depends(api_auth)])
async def list_models():
    data = await _fetch_models()
    seen: set[str] = set()
    out = []
    for m in data["models"]:
        mid = m["modelKey"] or "vorflux-auto"
        if mid in seen:
            continue
        seen.add(mid)
        out.append({"id": mid, "object": "model", "created": 1700000000,
                    "owned_by": f"vorflux/{m.get('family') or 'default'}",
                    "display_name": m.get("displayName"),
                    "available": bool(m.get("isAvailable", True))})
    if "vorflux-auto" not in seen:
        out.insert(0, {"id": "vorflux-auto", "object": "model",
                       "created": 1700000000, "owned_by": "vorflux",
                       "display_name": "Vorflux Auto", "available": True})
    return {"object": "list", "data": out}


# --------------------------------------------------------------- completion
def _flatten_messages(messages: list[dict]) -> str:
    """Render OpenAI message list as a single agent prompt."""
    parts: list[str] = []
    for m in messages:
        role = (m.get("role") or "user").lower()
        content = m.get("content")
        if isinstance(content, list):  # multimodal blocks -> text parts only
            content = "\n".join(p.get("text", "") for p in content
                                if isinstance(p, dict) and p.get("type") == "text")
        content = (content or "").strip()
        if not content:
            continue
        if role == "system":
            parts.append(f"[System]\n{content}")
        elif role == "assistant":
            parts.append(f"[Assistant]\n{content}")
        elif role == "tool":
            parts.append(f"[Tool:{m.get('name') or 'result'}]\n{content}")
        else:
            parts.append(f"[User]\n{content}")
    return "\n\n".join(parts)


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if (m.get("role") or "").lower() == "user":
            c = m.get("content")
            if isinstance(c, list):
                return "\n".join(p.get("text", "") for p in c
                                 if isinstance(p, dict) and p.get("type") == "text")
            return c or ""
    return _flatten_messages(messages)


def _extract_agent_texts(messages: list[dict], seen: set[str]) -> list[str]:
    """New agent-authored text since last poll, in order."""
    out = []
    for msg in messages:
        mid = msg.get("id")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        mc = msg.get("messageContent") or {}
        tn = mc.get("__typename", "")
        if tn in ("SimpleTextMessage", "InterimUpdateMessage"):
            text = mc.get("content") or ""
            if text.strip():
                out.append(text)
        elif tn == "ErrorOutputMessage":
            raise vorflux.VorfluxError(mc.get("message") or "upstream agent error",
                                       0, "agent_error")
    return out


def _usage_from_session(sess: dict | None) -> dict:
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for t in (sess or {}).get("tokenUsage") or []:
        usage["prompt_tokens"] += t.get("inputTokens") or 0
        usage["completion_tokens"] += t.get("outputTokens") or 0
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return usage


# ---------------------------------------------------------- tool calling
def _tools_preamble(tools: list, tool_choice) -> str:
    """Render OpenAI tools as prompt instructions for the upstream agent."""
    lines = []
    for t in tools or []:
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        fn = fn or {}
        name = fn.get("name")
        if not name:
            continue
        desc = fn.get("description") or ""
        params = fn.get("parameters") or {}
        props = params.get("properties") or {}
        req = params.get("required") or []
        sig = ", ".join(f"{k}: {(v or {}).get('type', 'any')}"
                        for k, v in props.items())
        lines.append(f"- {name}({sig})" + (f" — {desc}" if desc else ""))
        if params:
            lines.append(f"  parameters schema: {json.dumps(params)}")
        if req:
            lines.append(f"  required: {', '.join(req)}")
    if not lines:
        return ""
    force = ""
    if tool_choice == "required":
        force = "\nYou MUST call a function in your response."
    elif isinstance(tool_choice, dict):
        fn = (tool_choice.get("function") or {})
        if fn.get("name"):
            force = f"\nYou MUST call the function {fn['name']}."
    return (
        "You can call functions to help answer. Available functions:\n\n"
        + "\n".join(lines)
        + "\n\nTo call a function, reply with ONLY this JSON — no prose, no markdown fences:"
        + '\n{"tool_call": {"name": "<name>", "arguments": {<arguments matching the schema>}}}'
        + "\nFor multiple calls in one turn:"
        + '\n{"tool_calls": [{"name": "<name>", "arguments": {...}}, ...]}'
        + "\nIf no function is needed, answer normally."
        + force)


def _parse_tool_calls(text: str) -> list[dict] | None:
    """Extract a tool_call/tool_calls JSON object from agent output."""
    t = (text or "").strip()
    if not t or "tool_call" not in t:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    raw = d.get("tool_calls") or ([d["tool_call"]] if d.get("tool_call") else None)
    if not isinstance(raw, list):
        return None
    out = []
    for c in raw:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        args = c.get("arguments") or {}
        out.append({
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {"name": c["name"],
                         "arguments": args if isinstance(args, str)
                                       else json.dumps(args)},
        })
    return out or None


def _resolve_model(req_model: str) -> tuple[str | None, dict]:
    """OpenAI model -> vorflux modelKey; 'vorflux-auto'/empty -> platform default."""
    model_map = db.get_json_setting("model_map", {})
    key = model_map.get(req_model, req_model)
    if key in ("", "vorflux-auto", "auto", "default", None):
        return None, {}
    if "|" in key:  # "modelKey|effort" sugar
        key, effort = key.split("|", 1)
        return key, {"reasoningEffort": effort}
    return key, {}


class _Turn:
    """One agent turn on a specific account session."""

    def __init__(self, ctx: AccountCtx, sess: vorflux.AccountSession,
                 session_id: str):
        self.ctx = ctx
        self.sess = sess
        self.session_id = session_id
        self.seen: set[str] = set()
        self.texts: list[str] = []
        self.status = "QUEUED"
        self.info: dict = {}

    async def poll_once(self) -> tuple[list[str], str]:
        msgs = await self.sess.get_messages(self.session_id)
        new = _extract_agent_texts(msgs, self.seen)
        self.texts.extend(new)
        s = await self.sess.get_session(self.session_id)
        if s:
            self.status = s.get("sessionStatus") or self.status
            self.info = s
        return new, self.status

    @property
    def answer(self) -> str:
        return "\n\n".join(t.strip() for t in self.texts if t.strip())


async def _run_turn(ctx: AccountCtx, sess: vorflux.AccountSession,
                    session_id: str, on_delta=None) -> _Turn:
    """Poll messages until terminal status. Adaptive cadence (fast early),
    stall detection triggers account failover."""
    turn = _Turn(ctx, sess, session_id)
    deadline = time.time() + config.MAX_TURN_WAIT
    quiet_polls = 0
    polls = 0
    last_new = time.time()
    while time.time() < deadline:
        try:
            new, status = await turn.poll_once()
        except vorflux.VorfluxError as e:
            if e.code == "unauthorized":
                raise
            await asyncio.sleep(config.POLL_INTERVAL)
            continue
        polls += 1
        if new:
            last_new = time.time()
            if on_delta:
                for t in new:
                    await on_delta(t)
        if status in vorflux.TERMINAL_STATUSES:
            quiet_polls += 1
            # grace polls: agent may append trailing messages right after status flips
            if quiet_polls >= config.IDLE_GRACE_POLLS:
                return turn
        else:
            quiet_polls = 0
            if time.time() - last_new > config.STALL_TIMEOUT:
                raise vorflux.VorfluxError("turn stalled: no agent output",
                                           0, "stall")
        interval = (config.POLL_FAST_INTERVAL
                    if polls <= config.POLL_FAST_POLLS else config.POLL_INTERVAL)
        await asyncio.sleep(interval)
    raise vorflux.VorfluxError("turn timeout", 0, "timeout")


def _is_credit_exhausted(e: vorflux.VorfluxError) -> bool:
    m = str(e).lower()
    return "credits are exhausted" in m or "insufficient_credits" in m


# -------------------------------------------------- conversation affinity
def _conv_key(messages: list[dict]) -> str:
    """Stable hash of a message list -> upstream session reuse key."""
    canon = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content
                              if isinstance(p, dict))
        tc = None
        if m.get("tool_calls"):
            tc = [[c.get("function", {}).get("name", ""),
                   c.get("function", {}).get("arguments", "")]
                  for c in m["tool_calls"]]
        canon.append({"r": m.get("role"), "c": content,
                      "n": m.get("name"), "tc": tc})
    return hashlib.sha256(
        json.dumps(canon, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


async def _attempt_loop(body: dict, on_delta=None) -> tuple[_Turn, str]:
    """Sequential retry loop across the pool; returns (turn, model_used)."""
    model_key, model_sel = _resolve_model(body.get("model") or "")
    messages = list(body.get("messages") or [])
    tools = body.get("tools") or []
    tchoice = body.get("tool_choice", "auto")
    if tools and tchoice != "none":
        pre = _tools_preamble(tools, tchoice)
        if pre:
            messages = [{"role": "system", "content": pre}] + messages
    continue_session = body.get("session_id") or body.get("conversation_id")

    # affinity: longest remembered prefix (up to 4 msgs back) -> continue session
    conv = None
    conv_key = ""
    if not continue_session:
        for k in range(1, min(5, len(messages))):
            h = _conv_key(messages[:-k])
            row = await asyncio.to_thread(db.get_conversation, h, config.CONV_TTL)
            if row:
                conv = {"session_id": row["session_id"],
                        "account_id": row["account_id"],
                        "delta": messages[len(messages) - k:]}
                conv_key = h
                break

    last_err: Exception | None = None
    attempts = 0
    credit_retries = 0
    prefer_id = conv["account_id"] if conv else None
    used_conv = conv is not None
    while True:
        ctx = await pool.pick(prefer_id)
        if ctx is None:
            raise last_err or vorflux.VorfluxError(
                "no healthy account available", 503, "pool_exhausted")
        prefer_id = None  # pin once; failover rotates freely
        t0 = time.time()
        try:
            sess = await pool.session(ctx)
            if used_conv:
                text = _flatten_messages(conv["delta"])
                await sess.add_message(conv["session_id"], [text],
                                       model_key, model_sel)
                session_id = conv["session_id"]
                created = {}
            elif continue_session:
                text = _last_user_text(messages)
                await sess.add_message(continue_session, [text],
                                       model_key, model_sel)
                session_id = continue_session
                created = {}
            else:
                prompt = _flatten_messages(messages)
                created = await sess.create_session(prompt, model_key, model_sel)
                session_id = created["sessionId"]
            turn = await _run_turn(ctx, sess, session_id, on_delta)
            if created:
                turn.info = created | (turn.info or {})
                turn.info["tokenUsage"] = (turn.info.get("tokenUsage")
                                           or created.get("tokenUsage"))
            await pool.release(ctx, ok=True, latency=time.time() - t0)
            stats = json.loads(ctx.row.get("stats_json") or "{}")
            db.bump_account_stats(ctx.id, {
                "requests": (stats.get("requests") or 0) + 1,
                "last_ok": time.time()})
            # remember conversation for the next request (prefix reuse)
            calls = _parse_tool_calls(turn.answer) if tools else None
            echo: dict = {"role": "assistant",
                          "content": None if calls else turn.answer}
            if calls:
                echo["tool_calls"] = calls
            await asyncio.to_thread(
                db.put_conversation, _conv_key(messages + [echo]),
                session_id, ctx.id)
            return turn, (created.get("currentModelKey") or model_key or "")
        except vorflux.VorfluxError as e:
            if used_conv:
                # remembered session is unusable upstream; drop and go fresh
                await asyncio.to_thread(db.delete_conversation, conv_key)
                used_conv = False
            # upstream out-of-credits: sideline account, retry is free
            if _is_credit_exhausted(e) and credit_retries < 16:
                credit_retries += 1
                ctx.row["status"] = "no_credits"
                await asyncio.to_thread(
                    db.update_account, ctx.id, {"status": "no_credits"})
                await pool.release(ctx, ok=False, latency=time.time() - t0)
                last_err = e
                continue
            attempts += 1
            hard = e.code in ("refresh_rejected", "no_account")
            if e.code == "unauthorized":
                await pool.invalidate_token(ctx)
                try:
                    await pool.force_refresh(ctx)
                    continue  # same retry budget consumed; fresh token next pick
                except vorflux.VorfluxError:
                    hard = True
            await pool.release(ctx, ok=False, latency=time.time() - t0,
                               hard_fail=hard)
            last_err = e
            if e.code == "graphql_error" and "model" in str(e).lower():
                raise  # client error, no point rotating
            if attempts > config.RETRY_BUDGET:
                break
    raise last_err or vorflux.VorfluxError("all accounts failed", 502, "pool_failed")


async def _execute(body: dict, on_delta=None) -> tuple[_Turn, str]:
    """Dispatch with optional hedging: after HEDGE_MS a second attempt runs
    on another account; first to finish (or first to stream) wins."""
    hedge = config.HEDGE_MS
    if hedge <= 0:
        return await _attempt_loop(body, on_delta)

    owner = {"id": None}

    def gate(i: int):
        if not on_delta:
            return None

        async def d(text: str):
            if owner["id"] in (None, i):
                owner["id"] = i
                await on_delta(text)
        return d

    tasks = [asyncio.create_task(_attempt_loop(body, gate(0)))]
    await asyncio.sleep(hedge / 1000)
    if not tasks[0].done():
        tasks.append(asyncio.create_task(_attempt_loop(body, gate(1))))
    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    winner = next(iter(done))
    if winner.exception() is None:
        for t in tasks:
            if t is not winner:
                t.cancel()
        return winner.result()
    # winner failed; give the hedge a chance
    errors = [winner.exception()]
    for t in tasks:
        if t is winner:
            continue
        try:
            return await t
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    raise errors[0]


def _completion_payload(turn: _Turn, model: str, answer: str,
                        tool_calls: list | None = None) -> dict:
    message: dict = {"role": "assistant", "content": answer}
    if tool_calls:
        message["content"] = None
        message["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "vorflux-auto",
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }],
        "usage": _usage_from_session(turn.info),
        "vorflux": {
            "session_id": turn.session_id,
            "account": turn.ctx.email,
            "session_status": turn.status,
            "session_url": f"{config.VORFLUX_BASE}/agent-sessions/{turn.session_id}",
        },
    }


@router.post("/v1/chat/completions", dependencies=[Depends(api_auth)])
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        return openai_error(400, "invalid JSON body", "invalid_request_error")
    messages = body.get("messages")
    if not messages or not isinstance(messages, list):
        return openai_error(400, "messages[] required", "invalid_request_error")
    stream = bool(body.get("stream"))
    req_model = body.get("model") or "vorflux-auto"
    t0 = time.time()

    if not stream:
        try:
            turn, model_used = await _execute(body)
        except vorflux.VorfluxError as e:
            status = 503 if e.code in ("pool_exhausted", "pool_failed") else 502
            if e.status in (400, 401, 403):
                status = e.status
            db.log_request(model=req_model, kind="chat", status="error",
                           latency_ms=int((time.time() - t0) * 1000), error=str(e))
            return openai_error(status, str(e))
        answer = turn.answer or "(agent produced no text output)"
        calls = _parse_tool_calls(answer) if body.get("tools") else None
        db.log_request(model=req_model, kind="chat", status="ok",
                       account_email=turn.ctx.email,
                       latency_ms=int((time.time() - t0) * 1000),
                       session_id=turn.session_id)
        return _completion_payload(turn, model_used or req_model, answer, calls)

    # ---------------- streaming ----------------
    want_tools = bool(body.get("tools"))
    queue: asyncio.Queue = asyncio.Queue()
    DONE = object()

    async def produce():
        async def on_delta(text: str):
            await queue.put(("delta", text))
        try:
            # buffer when tools are in play: a tool_call JSON must not stream as text
            turn, model_used = await _execute(body, None if want_tools else on_delta)
            await queue.put(("done", (turn, model_used)))
        except vorflux.VorfluxError as e:
            await queue.put(("error", e))
        except Exception as e:  # noqa: BLE001
            await queue.put(("error", vorflux.VorfluxError(str(e))))
        finally:
            await queue.put(DONE)

    async def sse():
        task = asyncio.create_task(produce())
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        sent_role = False
        account_email = ""
        session_id = ""

        def chunk(delta: dict, finish: str | None = None):
            return ("data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk",
                "created": created, "model": req_model,
                "choices": [{"index": 0, "delta": delta,
                             "finish_reason": finish}]}) + "\n\n")
        try:
            while True:
                if await request.is_disconnected():
                    task.cancel()
                    return
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=5)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if item is DONE:
                    break
                kind, payload = item
                if kind == "delta":
                    if not sent_role:
                        yield chunk({"role": "assistant"})
                        sent_role = True
                    yield chunk({"content": payload})
                elif kind == "error":
                    yield f"data: {json.dumps({'error': {'message': str(payload)}})}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                elif kind == "done":
                    turn, model_used = payload
                    account_email = turn.ctx.email
                    session_id = turn.session_id
                    calls = _parse_tool_calls(turn.answer) if want_tools else None
                    if not sent_role:
                        yield chunk({"role": "assistant"})
                        sent_role = True
                        if calls:
                            yield chunk({"tool_calls": calls})
                        else:
                            yield chunk({"content": turn.answer
                                         or "(agent produced no text output)"})
                    yield chunk({}, "tool_calls" if calls else "stop")
                    meta = {"id": cid, "object": "chat.completion.chunk",
                            "created": created, "model": model_used or req_model,
                            "choices": [], "usage": _usage_from_session(turn.info),
                            "vorflux": {"session_id": session_id,
                                        "account": account_email}}
                    yield f"data: {json.dumps(meta)}\n\n"
            yield "data: [DONE]\n\n"
            db.log_request(model=req_model, kind="chat_stream", status="ok",
                           account_email=account_email,
                           latency_ms=int((time.time() - t0) * 1000),
                           session_id=session_id)
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ------------------------------------------- Responses API (Codex / agents)
def _responses_to_chat(body: dict) -> dict:
    """Translate a Responses-API request into our chat-style body."""
    msgs: list[dict] = []
    if body.get("instructions"):
        msgs.append({"role": "system", "content": body["instructions"]})
    inp = body.get("input")
    if isinstance(inp, str):
        msgs.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "function_call_output":
                msgs.append({"role": "tool",
                             "name": item.get("name") or item.get("call_id", ""),
                             "content": item.get("output") or ""})
                continue
            if itype == "function_call":
                msgs.append({"role": "assistant",
                             "content": f"[function_call {item.get('name')}"
                                        f"({item.get('arguments')})]"})
                continue
            role = item.get("role") or "user"
            content = item.get("content")
            if isinstance(content, list):
                text = "".join(c.get("text", "") for c in content
                               if isinstance(c, dict) and c.get("type") in
                               ("input_text", "output_text", "text"))
            else:
                text = content or ""
            msgs.append({"role": role, "content": text})
    out: dict = {"model": body.get("model"), "messages": msgs,
                 "stream": body.get("stream")}
    if body.get("tools"):
        out["tools"] = body["tools"]
        if body.get("tool_choice") is not None:
            out["tool_choice"] = body["tool_choice"]
    sid = body.get("session_id") or body.get("conversation_id")
    if sid:
        out["session_id"] = sid
    return out


def _response_obj(rid: str, model: str, turn: "_Turn",
                  calls: list | None = None) -> dict:
    output: list[dict] = []
    if calls:
        for c in calls:
            output.append({
                "type": "function_call", "id": f"fc_{uuid.uuid4().hex[:20]}",
                "call_id": c["id"], "name": c["function"]["name"],
                "arguments": c["function"]["arguments"], "status": "completed"})
    else:
        output.append({"type": "message", "id": f"msg_{uuid.uuid4().hex[:20]}",
                       "status": "completed", "role": "assistant",
                       "content": [{"type": "output_text",
                                    "text": turn.answer or "",
                                    "annotations": []}]})
    usage = _usage_from_session(turn.info)
    return {"id": rid, "object": "response", "created_at": int(time.time()),
            "status": "completed", "model": model or "vorflux-auto",
            "output": output,
            "usage": {"input_tokens": usage["prompt_tokens"],
                      "output_tokens": usage["completion_tokens"],
                      "total_tokens": usage["total_tokens"]},
            "vorflux": {"session_id": turn.session_id,
                        "account": turn.ctx.email}}


@router.post("/v1/responses", dependencies=[Depends(api_auth)])
async def responses_api(request: Request):
    try:
        raw = await request.json()
    except Exception:
        return openai_error(400, "invalid JSON body", "invalid_request_error")
    body = _responses_to_chat(raw)
    if not body["messages"]:
        return openai_error(400, "input required", "invalid_request_error")
    model = body.get("model") or "vorflux-auto"
    t0 = time.time()
    rid = f"resp_{uuid.uuid4().hex[:24]}"

    if not raw.get("stream"):
        try:
            turn, model_used = await _execute(body)
        except vorflux.VorfluxError as e:
            status = 503 if e.code in ("pool_exhausted", "pool_failed") else 502
            if e.status in (400, 401, 403):
                status = e.status
            db.log_request(model=model, kind="responses", status="error",
                           latency_ms=int((time.time() - t0) * 1000), error=str(e))
            return openai_error(status, str(e))
        calls = _parse_tool_calls(turn.answer) if body.get("tools") else None
        db.log_request(model=model, kind="responses", status="ok",
                       account_email=turn.ctx.email,
                       latency_ms=int((time.time() - t0) * 1000),
                       session_id=turn.session_id)
        return _response_obj(rid, model_used or model, turn, calls)

    # ---- streaming: Responses event sequence ----
    want_tools = bool(body.get("tools"))
    queue: asyncio.Queue = asyncio.Queue()
    DONE = object()

    async def produce():
        async def on_delta(text: str):
            await queue.put(("delta", text))
        try:
            turn, model_used = await _execute(body, None if want_tools else on_delta)
            await queue.put(("done", (turn, model_used)))
        except vorflux.VorfluxError as e:
            await queue.put(("error", e))
        except Exception as e:  # noqa: BLE001
            await queue.put(("error", vorflux.VorfluxError(str(e))))
        finally:
            await queue.put(DONE)

    async def sse():
        task = asyncio.create_task(produce())
        base = {"id": rid, "object": "response", "created_at": int(time.time()),
                "status": "in_progress", "model": model, "output": []}
        seq = 0

        def ev(name: str, data: dict) -> str:
            nonlocal seq
            seq += 1
            data.setdefault("sequence_number", seq)
            return f"event: {name}\ndata: {json.dumps(data)}\n\n"

        yield ev("response.created", {"type": "response.created", "response": base})
        msg_id = f"msg_{uuid.uuid4().hex[:20]}"
        yield ev("response.output_item.added",
                 {"type": "response.output_item.added", "output_index": 0,
                  "item": {"type": "message", "id": msg_id, "status": "in_progress",
                           "role": "assistant", "content": []}})
        yield ev("response.content_part.added",
                 {"type": "response.content_part.added", "item_id": msg_id,
                  "output_index": 0, "content_index": 0,
                  "part": {"type": "output_text", "text": "", "annotations": []}})
        full = ""
        try:
            while True:
                if await request.is_disconnected():
                    task.cancel()
                    return
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=5)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if item is DONE:
                    break
                kind, payload = item
                if kind == "delta":
                    full += payload
                    yield ev("response.output_text.delta",
                             {"type": "response.output_text.delta",
                              "item_id": msg_id, "output_index": 0,
                              "content_index": 0, "delta": payload})
                elif kind == "error":
                    yield ev("response.failed",
                             {"type": "response.failed",
                              "response": {**base, "status": "failed",
                                           "error": {"message": str(payload)}}})
                    yield "data: [DONE]\n\n"
                    return
                elif kind == "done":
                    turn, model_used = payload
                    calls = (_parse_tool_calls(turn.answer)
                             if want_tools else None)
                    if calls:
                        for i, c in enumerate(calls, start=1):
                            fc = {"type": "function_call",
                                  "id": f"fc_{uuid.uuid4().hex[:20]}",
                                  "call_id": c["id"],
                                  "name": c["function"]["name"],
                                  "arguments": c["function"]["arguments"],
                                  "status": "completed"}
                            yield ev("response.output_item.added",
                                     {"type": "response.output_item.added",
                                      "output_index": i, "item": fc})
                            yield ev("response.function_call_arguments.done",
                                     {"type": "response.function_call_arguments.done",
                                      "item_id": fc["id"], "output_index": i,
                                      "arguments": fc["arguments"]})
                            yield ev("response.output_item.done",
                                     {"type": "response.output_item.done",
                                      "output_index": i, "item": fc})
                        final = _response_obj(rid, model_used or model, turn, calls)
                    else:
                        if not full and turn.answer:
                            full = turn.answer
                            yield ev("response.output_text.delta",
                                     {"type": "response.output_text.delta",
                                      "item_id": msg_id, "output_index": 0,
                                      "content_index": 0, "delta": full})
                        yield ev("response.output_text.done",
                                 {"type": "response.output_text.done",
                                  "item_id": msg_id, "output_index": 0,
                                  "content_index": 0, "text": full})
                        msg_done = {"type": "message", "id": msg_id,
                                    "status": "completed", "role": "assistant",
                                    "content": [{"type": "output_text",
                                                 "text": full,
                                                 "annotations": []}]}
                        yield ev("response.content_part.done",
                                 {"type": "response.content_part.done",
                                  "item_id": msg_id, "output_index": 0,
                                  "content_index": 0,
                                  "part": {"type": "output_text", "text": full,
                                           "annotations": []}})
                        yield ev("response.output_item.done",
                                 {"type": "response.output_item.done",
                                  "output_index": 0, "item": msg_done})
                        final = _response_obj(rid, model_used or model, turn)
                    yield ev("response.completed",
                             {"type": "response.completed", "response": final})
            yield "data: [DONE]\n\n"
            db.log_request(model=model, kind="responses_stream", status="ok",
                           latency_ms=int((time.time() - t0) * 1000))
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})

