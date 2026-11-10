"""模型适配器：OpenAI 兼容 /chat/completions 接口，零第三方依赖（urllib）。

密钥只从环境变量读取，配置文件里只写变量名，避免把密钥写进仓库。
配置查找顺序：环境变量 → hub/model.json → 不可用。

可用性是可判定的：没有 base_url / 没有密钥时 status().ready=False，
上层会自动回退规则适配器，并在 generation.json 里写明回退原因，
不会出现"看起来调了模型其实没调"的情况。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .base import AdapterError, BaseAdapter, digest, strip_fences, validate

SYSTEM_PROMPT = """你是资深 Java 迁移工程师，负责把 Java 8 存量实现等价迁移为 JDK 17 目标实现。

硬性约束，违反任意一条即判定失败：
1. 保留事务边界：settle 方法必须带 @TxRequired(propagation = "REQUIRED")，并保留 dao.begin() / dao.commit() / dao.rollback()
2. 金额一律使用 BigDecimal，禁止出现 double 变量
3. 必须 implements SettleApi，类名 ModernSettleService，包名 com.demo.settle
4. 不引入新依赖，只允许 java.* 与 com.demo.settle.*
5. 输出口径必须与存量实现逐字节一致：金额文本保留 2 位小数，异常类型与消息不变
6. 只输出 Java 源码，不要 Markdown 围栏，不要任何解释文字"""

def _evidence_text(evidence: list) -> str:
    if not evidence:
        return "（无）"
    lines = []
    for item in evidence[:12]:
        lines.append("- 用例 {} · 金额 {} · 维度 {}：存量={!r} 你的实现={!r}".format(
            item.get("case"), item.get("amount"), item.get("dimension"),
            item.get("legacy", ""), item.get("modern", "")))
    return "\n".join(lines)


REPAIR_PROMPT = """上述输出未通过静态契约校验，问题如下：
{problems}

请只输出修正后的完整 Java 源码，不要解释，不要 Markdown 围栏。"""


class LLMAdapter(BaseAdapter):
    id = "llm-api-v1"
    label = "模型生成"
    ai = True

    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        self.config = self._load_config()

    # ---------------- 配置与可用性 ----------------
    def _load_config(self) -> dict:
        config = {
            "base_url": os.environ.get("HUB_MODEL_BASE_URL", ""),
            "model": os.environ.get("HUB_MODEL_NAME", ""),
            "api_key_env": "HUB_MODEL_API_KEY",
            "temperature": 0.1,
            "max_tokens": 2400,
            "timeout": 60,
            "price_in_per_1k": None,
            "price_out_per_1k": None,
        }
        if self.config_path.exists():
            try:
                file_config = json.loads(self.config_path.read_text(encoding="utf-8"))
                for key, value in file_config.items():
                    if value not in (None, ""):
                        config[key] = value
            except (ValueError, OSError):
                pass
        if os.environ.get("HUB_MODEL_BASE_URL"):
            config["base_url"] = os.environ["HUB_MODEL_BASE_URL"]
        if os.environ.get("HUB_MODEL_NAME"):
            config["model"] = os.environ["HUB_MODEL_NAME"]
        return config

    def _key(self) -> str:
        """优先环境变量；model.json 里的 api_key 只作为本机演示的兜底。"""
        from_env = os.environ.get(self.config.get("api_key_env") or "HUB_MODEL_API_KEY", "")
        return from_env or str(self.config.get("api_key") or "")

    def status(self) -> dict:
        missing = []
        if not self.config.get("base_url"):
            missing.append("基址（HUB_MODEL_BASE_URL 或 model.json:base_url）")
        if not self.config.get("model"):
            missing.append("模型名（HUB_MODEL_NAME 或 model.json:model）")
        if not self._key():
            missing.append("密钥（环境变量 {} 或 model.json:api_key）".format(
                self.config.get("api_key_env") or "HUB_MODEL_API_KEY"))
        return {
            "ready": not missing,
            "reason": "" if not missing else "缺少" + "、".join(missing),
            "base_url": self.config.get("base_url") or "",
            "model": self.config.get("model") or "",
            "key_present": bool(self._key()),
            "key_source": "env" if os.environ.get(
                self.config.get("api_key_env") or "HUB_MODEL_API_KEY") else (
                "file" if self.config.get("api_key") else "none"),
            "timeout": self.config.get("timeout"),
        }

    # ---------------- 生成 ----------------
    def generate(self, context: dict) -> dict:
        status = self.status()
        if not status["ready"]:
            raise AdapterError(status["reason"])

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._build_prompt(context)},
        ]
        began = time.time()
        attempts, problems = 0, []
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        code, served_model = "", ""

        while attempts < 2:
            attempts += 1
            payload = self._post(messages)
            served_model = payload.get("model") or served_model
            usage = payload.get("usage") or {}
            for key in usage_total:
                usage_total[key] += int(usage.get(key) or 0)
            choices = payload.get("choices") or []
            if not choices:
                raise AdapterError("响应缺少 choices：{}".format(json.dumps(payload)[:300]))
            code = strip_fences(choices[0].get("message", {}).get("content", ""))
            problems = validate(code)
            if not problems:
                break
            if attempts >= 2:
                break
            messages.append({"role": "assistant", "content": code})
            messages.append({"role": "user",
                             "content": REPAIR_PROMPT.format(
                                 problems="\n".join("- " + item for item in problems))})

        latency = int((time.time() - began) * 1000)
        meta = self._meta(code, usage=dict(usage_total, model=served_model or self.config.get("model"),
                                           cost=self._cost(usage_total),
                                           cost_currency=self.config.get("cost_currency") or "CNY"),
                          self_check=problems)
        meta.update({
            "mode": "ok",
            "model": served_model or self.config.get("model"),
            "requested_model": self.config.get("model"),
            "latency_ms": latency,
            "attempts": attempts,
            "prompt_digest": digest(messages[1]["content"]),
            "note": "模型生成" + ("" if not problems else " · 自检未通过，交由闸门判定"),
            "applied_mappings": ["MAP-001", "MAP-002", "MAP-003"],
        })
        return meta

    def raw(self, prompt: str, system: str = "") -> dict:
        """单次调用，不做契约自检、不做修复重试。

        用于「基线臂」对照：模拟把存量代码贴进对话框、拿回结果就用的场景。
        """
        status = self.status()
        if not status["ready"]:
            raise AdapterError(status["reason"])
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        began = time.time()
        payload = self._post(messages)
        usage = payload.get("usage") or {}
        choices = payload.get("choices") or []
        if not choices:
            raise AdapterError("响应缺少 choices")
        return {
            "code": strip_fences(choices[0].get("message", {}).get("content", "")),
            "usage": {
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
                "cost": self._cost(usage),
                "cost_currency": self.config.get("cost_currency") or "CNY",
                "model": payload.get("model") or self.config.get("model"),
            },
            "model": payload.get("model") or self.config.get("model"),
            "latency_ms": int((time.time() - began) * 1000),
        }

    def _build_prompt(self, context: dict) -> str:
        rules = "\n".join("- {} {}（来源 {}，置信度 {}）".format(
            rule.get("id"), rule.get("text"), rule.get("source"), rule.get("confidence"))
            for rule in context.get("rules", []))
        return "\n".join([
            "[存量实现 · LegacySettleService.java]",
            context.get("legacy_source", ""),
            "",
            "[已存在的接口面 · 从代码图谱抽取，必须严格按这些签名调用，不得臆造]",
            context.get("api_surface", "（无）"),
            "",
            "[业务规则库]",
            rules or "（无）",
            "",
            "[迁移映射表]",
            context.get("mapping", ""),
            "",
            "[上一版被闸门拦下的用例 · 这些输入的行为必须与存量一致]",
            _evidence_text(context.get("evidence") or []),
            "",
            "[已确认的历史坑]",
            "- PREC-003 存量用 double 做金额中间计算，目标实现必须改 BigDecimal",
            "- TX-001 事务注解与 begin/commit 缺失不会被编译器和单测发现",
            "- TZ-002 禁止使用 java.util.Date",
            "",
            "请输出完整的 {} 源码。".format(context.get("target_class", "ModernSettleService")),
        ])

    def _post(self, messages: list) -> dict:
        base = str(self.config["base_url"]).rstrip("/")
        url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
        body = json.dumps({
            "model": self.config["model"],
            "messages": messages,
            "temperature": self.config.get("temperature", 0.1),
            "max_tokens": self.config.get("max_tokens", 2400),
        }).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self._key(),
        })
        try:
            with urllib.request.urlopen(request, timeout=self.config.get("timeout", 60)) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:400]
            raise AdapterError("HTTP {} · {}".format(error.code, detail))
        except urllib.error.URLError as error:
            raise AdapterError("网络不可达 · {}".format(error.reason))

    def _cost(self, usage: dict):
        price_in = self.config.get("price_in_per_1k")
        price_out = self.config.get("price_out_per_1k")
        if price_in is None or price_out is None:
            return None
        return round(usage.get("prompt_tokens", 0) / 1000 * float(price_in) +
                     usage.get("completion_tokens", 0) / 1000 * float(price_out), 6)
