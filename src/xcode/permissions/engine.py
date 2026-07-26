"""权限引擎：规则文件 + hard deny + ask 回调。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable, Literal

from xcode.tools.builtins import PRIVILEGED_WORDS, _shell_command_words

Decision = Literal["allow", "deny", "ask"]
Scope = Literal["global", "project", "session"]

READ_ONLY_ALLOW = {
    "LS",
    "Glob",
    "Grep",
    "Read",
    "TaskList",
    "TaskGet",
    "Skill",
    "TodoWrite",
    "Compact",
}
ASK_BY_DEFAULT = {"Bash", "Edit", "Write"}
SCOPE_PRIORITY: dict[Scope, int] = {"global": 0, "project": 1, "session": 2}


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(slots=True)
class PermissionRule:
    tool_name: str
    field: str
    pattern: str
    decision: Decision
    scope: Scope = "project"
    reason: str | None = None


@dataclass(slots=True)
class PermissionResult:
    decision: Decision
    reason: str
    source: str


def load_rules_from_settings(path: Path, *, scope: Scope) -> list[PermissionRule]:
    """从 settings.json 读取 permissions.rules。"""
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = (raw.get("permissions") or {}).get("rules") or []
    rules: list[PermissionRule] = []
    for item in items:
        rules.append(
            PermissionRule(
                tool_name=str(item["tool_name"]),
                field=str(item.get("field") or "*"),
                pattern=str(item.get("pattern") or "*"),
                decision=str(item["decision"]),  # type: ignore[arg-type]
                scope=scope,
                reason=str(item["reason"]) if item.get("reason") is not None else None,
            )
        )
    return rules


def settings_paths(*, data_home: Path, workspace: Path) -> tuple[Path, Path]:
    """返回 (全局 settings, 项目 settings) 路径。"""
    return data_home / "settings.json", workspace / ".xcode" / "settings.json"


@dataclass
class PermissionEngine:
    """决定工具调用是否放行。

    auto_allow=True 时跳过 ask（测试 / -p 默认）；hard deny 仍生效。
    """

    rules: list[PermissionRule] = field(default_factory=list)
    ask: Callable[[str, dict[str, Any]], bool] | None = None
    auto_allow: bool = False

    @classmethod
    def from_settings(
        cls,
        *,
        data_home: Path,
        workspace: Path,
        ask: Callable[[str, dict[str, Any]], bool] | None = None,
        auto_allow: bool = False,
    ) -> PermissionEngine:
        global_path, project_path = settings_paths(data_home=data_home, workspace=workspace)
        rules = [
            *load_rules_from_settings(global_path, scope="global"),
            *load_rules_from_settings(project_path, scope="project"),
        ]
        return cls(rules=rules, ask=ask, auto_allow=auto_allow)

    def evaluate(self, tool_name: str, params: dict[str, Any]) -> PermissionResult:
        """判定工具调用：hard deny → 规则 → 默认策略。不弹 HITL。"""
        # --- 1) 硬拒绝（不可被规则放行） ---
        hard = self._hard_deny(tool_name, params)
        if hard:
            return hard
        # --- 2) 命中 settings 规则 ---
        rule = self._best_rule(tool_name, params)
        if rule is not None:
            return PermissionResult(
                decision=rule.decision,
                reason=rule.reason or f"命中 {rule.scope} 规则",
                source=f"rule:{rule.scope}",
            )
        # --- 3) 内置默认：只读放行 / 敏感询问 ---
        if tool_name in READ_ONLY_ALLOW:
            return PermissionResult("allow", "只读默认放行", "default")
        if tool_name in ASK_BY_DEFAULT:
            return PermissionResult("ask", "敏感工具默认询问", "default")
        return PermissionResult("allow", "默认放行", "default")

    def check(self, tool_name: str, params: dict[str, Any]) -> bool:
        """供工具回调：True 放行。

        deny 不可被 auto_allow 绕过；ask 在 auto_allow 或用户确认后放行。
        """
        result = self.evaluate(tool_name, params)
        if result.decision == "deny":
            return False
        if result.decision == "allow":
            return True
        if self.auto_allow:
            return True
        if self.ask is None:
            return False
        return bool(self.ask(tool_name, params))

    def _hard_deny(self, tool_name: str, params: dict[str, Any]) -> PermissionResult | None:
        if tool_name != "Bash":
            return None
        command = params.get("command")
        if not isinstance(command, str):
            return None
        normalized = " ".join(command.strip().split()).lower()
        if normalized in {"rm -rf /", "rm -rf /*"}:
            return PermissionResult("deny", "不允许删除系统根目录", "hard_deny")
        for word in _shell_command_words(command):
            if word.lower() in PRIVILEGED_WORDS:
                return PermissionResult("deny", f"特权命令被拒绝: {word}", "hard_deny")
        return None

    def _best_rule(self, tool_name: str, params: dict[str, Any]) -> PermissionRule | None:
        best: PermissionRule | None = None
        best_score = -1
        for index, rule in enumerate(self.rules):
            if not self._matches(rule, tool_name, params):
                continue
            score = SCOPE_PRIORITY[rule.scope] * 10_000 + index
            if score >= best_score:
                best = rule
                best_score = score
        return best

    def _matches(self, rule: PermissionRule, tool_name: str, params: dict[str, Any]) -> bool:
        if rule.tool_name not in {"*", tool_name} and not fnmatch(tool_name, rule.tool_name):
            return False
        if rule.field == "*":
            return True
        values = self._field_values(rule.field, tool_name, params)
        return any(fnmatch(v, rule.pattern) for v in values)

    def _field_values(self, field: str, tool_name: str, params: dict[str, Any]) -> list[str]:
        if field == "tool_name":
            return [tool_name]
        if field == "command_word":
            command = params.get("command")
            return _shell_command_words(command) if isinstance(command, str) else []
        value = params.get(field)
        if value is None:
            return []
        if isinstance(value, list):
            return [str(x) for x in value]
        return [str(value)]
