"""Loads and pins config for a single pipeline run.

Lesson carried over from the parent project (ai_architecture.md v1.5 §8):
the v0.3 prototype was discarded because it read the *live* policy_config.json
and broke silently when the file changed underneath it. This loader reads
the config once at process start and pins config_version into every output —
replayability requires that a given run always declares which config version
it ran against.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@dataclass(frozen=True)
class PinnedConfig:
    config_version: str
    registry_version: str
    policy: dict
    flag_registry: dict
    schema: dict

    def flag_tier(self, flag: str) -> str:
        for entry in self.flag_registry["flags"]:
            if entry["flag"] == flag:
                return entry["tier"]
        raise KeyError(f"Unknown flag '{flag}' — not present in pinned flag_registry {self.registry_version}")

    def blocking_flags(self) -> set:
        return {f["flag"] for f in self.flag_registry["flags"] if f["tier"] == "blocking"}

    def informational_flags(self) -> set:
        return {f["flag"] for f in self.flag_registry["flags"] if f["tier"] == "informational"}


def load_pinned_config(config_dir: Path = CONFIG_DIR) -> PinnedConfig:
    policy = json.loads((config_dir / "policy_config.json").read_text())
    flag_registry = json.loads((config_dir / "flag_registry.json").read_text())
    schema = json.loads((config_dir / "expense_report.schema.json").read_text())

    # Three-way sync check (FLG-02 in the parent project): policy_config.flags,
    # flag_registry.flags, and the schema enum must agree. A demo that silently
    # tolerates drift here would undercut the exact thing it's trying to prove.
    policy_flags = set(policy["flags"])
    registry_flags = {f["flag"] for f in flag_registry["flags"]}
    schema_flags = set(schema["properties"]["flags"]["items"]["properties"]["flag"]["enum"])
    if not (policy_flags == registry_flags == schema_flags):
        raise ValueError(
            "Flag registry out of sync (FLG-02 check failed):\n"
            f"  policy_config only:   {policy_flags - registry_flags - schema_flags}\n"
            f"  flag_registry only:   {registry_flags - policy_flags - schema_flags}\n"
            f"  schema only:          {schema_flags - policy_flags - registry_flags}"
        )

    return PinnedConfig(
        config_version=policy["config_version"],
        registry_version=flag_registry["registry_version"],
        policy=policy,
        flag_registry=flag_registry,
        schema=schema,
    )
