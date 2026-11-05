# -*- coding: utf-8 -*-
"""LLM 客户端：OpenAI 兼容网关接入层（无 mock、只连真实模型）。

支持：deepseek / openai / ollama(本地) / custom(任意兼容网关)

工程要点（针对真实网关踩过的坑）：
    1. 很多网关/思考型模型会把 JSON 包在 ```json 围栏里 → 统一剥离；
    2. 思考型模型会用 completion_thinking_tokens 吃掉 max_tokens，
       导致输出被截断 → 检测 finish_reason=="length" 并自动加倍额度重试；
    3. 大段 Markdown 塞进 JSON 字符串极易因转义/截断而崩 →
       复盘阶段改用「JSON + 分隔符 + 纯文本」两段式协议（见 SPLIT_PLAYBOOK）；
    4. 解析失败时提供 JSON 修复（补全未闭合的字符串与括号）。
"""
from __future__ import annotations

import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROVIDERS = {
    "deepseek": {"base": "https://api.deepseek.com/v1", "model": "deepseek-chat",
                 "key_env": "DEEPSEEK_API_KEY"},
    "openai": {"base": "https://api.openai.com/v1", "model": "gpt-4o-mini",
               "key_env": "OPENAI_API_KEY"},
    "ollama": {"base": "http://127.0.0.1:11434/v1", "model": "qwen2.5:14b",
               "key_env": ""},
    "custom": {"base": "", "model": "", "key_env": "LLM_API_KEY"},
}

SPLIT_PLAYBOOK = "===PLAYBOOK==="
_FENCE = re.compile(r"^```(?:json|markdown|md)?\s*|\s*```$", re.I)


class LLMError(RuntimeError):
    pass


def _strip_fence(t):
    t = (t or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    if t.endswith("```"):
        t = t[:-3].rstrip()
    return t


def repair_json(text):
    """尽量把被截断的 JSON 补成可解析（补字符串引号、闭合括号）。"""
    if not text:
        return None
    t = _strip_fence(text)
    start = t.find("{")
    if start < 0:
        return None
    t = t[start:]
    stack, in_str, esc = [], False, False
    cut = len(t)
    for i, ch in enumerate(t):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
            if not stack:                    # 顶层对象已闭合
                cut = i + 1
                break
    body = t[:cut]
    if in_str:
        body = body.rstrip("\\") + '"'
    body = body.rstrip().rstrip(",")
    for closer in reversed(stack):
        body = body.rstrip().rstrip(",") + closer
    try:
        return json.loads(body)
    except Exception:
        return None


def extract_json(text):
    """从模型输出里稳健地抠出 JSON 对象。"""
    if not text:
        return None
    t = _strip_fence(text)
    try:
        return json.loads(t)
    except Exception:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except Exception:
            pass
    return repair_json(t)


def split_sections(text, marker=SPLIT_PLAYBOOK):
    """两段式协议解析：返回 (json_obj, 纯文本段落 or None)。"""
    if not text:
        return None, None
    raw = text
    idx = raw.find(marker)
    head, tail = (raw[:idx], raw[idx + len(marker):]) if idx >= 0 else (raw, None)
    obj = extract_json(head)
    if obj is None:
        obj = extract_json(raw)              # 兜底：结构合规时整段就是 JSON
    return obj, (_strip_fence(tail).strip() if tail else None)


class LLMClient:
    def __init__(self, provider, cfg):
        p = PROVIDERS.get(provider)
        if not p:
            raise LLMError("未知 provider：%s（可选：%s）"
                           % (provider, "/".join(PROVIDERS)))
        self.name = provider
        self.base = (cfg.get("base_url") or p["base"]).rstrip("/")
        self.model = cfg.get("model") or p["model"]
        key = cfg.get("api_key") or ""
        if not key and p["key_env"]:
            key = os.environ.get(p["key_env"], "")
        self.api_key = key
        self.timeout = float(cfg.get("timeout_sec", 180))
        self.temperature = float(cfg.get("temperature", 0.4))
        self.max_tokens = int(cfg.get("max_tokens", 8000))
        # 输出额度上限（截断时自动升额的天花板）：默认取模型自身上限，不再人为压缩
        self.ceiling = int(cfg.get("max_tokens_ceiling") or 393216)
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self.log_path = cfg.get("log_path")
        self.last_finish_reason = None
        self.last_truncated = False
        if not self.base:
            raise LLMError("未配置 base_url：请在 Web「设置」页或 config.json → ai.base_url 填写网关地址")
        if provider != "ollama" and not self.api_key:
            raise LLMError("未配置 API Key：请在 Web「设置」页填写 api_key，"
                           "或设置环境变量 %s" % (p["key_env"] or "LLM_API_KEY"))

    # ------------------------------------------------------------------ #
    def chat(self, system, user, json_mode=True, temperature=None, max_tokens=None):
        """返回 (text, usage)。空输出或截断时自动加倍 max_tokens 重试（最多两次、上限 32000）。

        思考型模型会把额度烧在 reasoning 上，所以预算必须能往上走；
        以前写死「budget < 8000 才重试、最多到 8000」，调大配置后反而不会重试了。
        """
        budget = int(max_tokens or self.max_tokens)
        text, usage = self._call(system, user, json_mode, temperature, budget)
        tries = 0
        while tries < 2 and budget < self.ceiling and \
                (self.last_truncated or not (text or "").strip()):
            tries += 1
            bigger = min(max(budget * 2, budget + 8000), self.ceiling)
            retry, usage2 = self._call(system, user, json_mode, temperature, bigger)
            budget = bigger
            if retry and len(retry.strip()) > len((text or "").strip()):
                text, usage = retry, usage2
        if not (text or "").strip():
            raise LLMError(
                "模型只输出了思考内容、没有正文（finish_reason=%s，max_tokens=%d，已自动升额重试 %d 次）。"
                "思考型模型会消耗 completion_thinking_tokens：可以把该阶段的 max_tokens 再调大，"
                "或换个不带思考的模型。" % (self.last_finish_reason, budget, tries))
        return text, usage

    def _call(self, system, user, json_mode, temperature, budget):
        url = self.base + "/chat/completions"
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": budget,
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        last = None
        for attempt in range(3):
            data = json.dumps(body).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = "Bearer " + self.api_key
            try:
                req = Request(url, data=data, headers=headers)
                with urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                choice = payload["choices"][0]
                text = choice["message"]["content"]
                self.last_finish_reason = choice.get("finish_reason")
                self.last_truncated = (self.last_finish_reason == "length")
                u = payload.get("usage") or {}
                self.usage["calls"] += 1
                self.usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
                self.usage["completion_tokens"] += int(u.get("completion_tokens") or 0)
                self._log({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "model": self.model,
                           "max_tokens": budget, "finish_reason": self.last_finish_reason,
                           "usage": {k: u.get(k) for k in
                                     ("prompt_tokens", "completion_tokens",
                                      "total_tokens", "completion_thinking_tokens")}})
                return text, u
            except HTTPError as exc:
                body_txt = ""
                try:
                    body_txt = exc.read().decode("utf-8", "replace")[:400]
                except Exception:
                    pass
                last = "%s %s" % (exc, body_txt)
                if exc.code == 400 and "response_format" in body:
                    body.pop("response_format", None)     # 网关不支持 JSON 模式 → 降级
                    continue
                if exc.code == 400 and "max_tokens" in body_txt and budget > 1024:
                    body["max_tokens"] = max(1024, budget // 2)   # 额度上限太严
                    budget = body["max_tokens"]
                    continue
                if exc.code in (401, 403, 404):
                    break
                time.sleep(1.5 * (attempt + 1))
            except (URLError, TimeoutError, OSError, KeyError, ValueError) as exc:
                last = exc
                time.sleep(1.5 * (attempt + 1))
        raise LLMError("模型调用失败：%s" % last)

    def _log(self, row):
        if not self.log_path:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass


def make_llm(ai_cfg):
    provider = (ai_cfg.get("provider") or "").strip().lower()
    if not provider:
        raise LLMError("未配置 AI 供应商：请在 Web「设置」页选择 provider 并填入 base_url/model/api_key")
    return LLMClient(provider, ai_cfg)
