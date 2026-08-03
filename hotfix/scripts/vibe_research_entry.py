"""Run Vibe Trading with an explicit research-only tool projection."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from mt5_agent.vibe_handoff import proposal_to_handoff  # noqa: E402


SAFE_TOOL_NAMES = frozenset(
    {
        "alpha_compare",
        "alpha_zoo",
        "factor_analysis",
        "financial_rigor",
        "get_macro_series",
        "get_market_data",
        "get_research_reports",
        "get_stock_news",
        "pattern",
        "portfolio_risk_xray",
        "read_file",
        "read_url",
        "report_audit",
        "search_symbol",
        "sentiment",
        "technical_indicators",
        "web_search",
    }
)


def project_safe_registry(full_registry):
    """Project a Vibe registry to reviewed read-only research tools."""
    from src.agent.tools import ToolRegistry

    safe = ToolRegistry()
    for name in sorted(SAFE_TOOL_NAMES):
        tool = full_registry.get(name)
        if tool is not None and bool(getattr(tool, "is_readonly", False)):
            safe.register(tool)
    return safe


def install_safe_registry_projection(vibe_tools):
    """Patch the upstream registry factory before the CLI imports it locally."""
    original_build_registry = vibe_tools.build_registry

    def safe_build_registry(**kwargs):
        kwargs["include_shell_tools"] = False
        kwargs["agent_config"] = None
        kwargs["interactive"] = False
        return project_safe_registry(original_build_registry(**kwargs))

    vibe_tools.build_registry = safe_build_registry


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--max-iter", type=int, default=20)
    parser.add_argument("--audit-tools", action="store_true")
    parser.add_argument("--bundle-manifest", type=Path)
    parser.add_argument("--candidate-output", type=Path)
    return parser.parse_args()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_bundle_contract(manifest_path: Path) -> tuple[str, set[str], dict[str, str]]:
    manifest_path = manifest_path.resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if payload.get("schema") != "mt5.vibe_research_bundle.v1":
        raise ValueError("candidate bundle schema mismatch")
    if payload.get("mode") != "research_only" or payload.get("order_authority") is not False:
        raise ValueError("candidate bundle crossed the research boundary")
    records = [record for record in payload.get("files", []) if record.get("source_symbol")]
    broker_by_source = {
        str(record["source_symbol"]): str(record["broker_symbol"])
        for record in records
    }
    declared = payload.get("research_scope", {}).get("symbols")
    if declared != list(broker_by_source) or not broker_by_source:
        raise ValueError("candidate bundle symbol mapping is invalid")
    return file_sha256(manifest_path), set(broker_by_source), broker_by_source


def parse_agent_proposal(
    content: str,
    *,
    manifest_sha256: str,
    allowed_symbols: set[str],
    broker_by_source: dict[str, str],
) -> dict:
    """Parse exactly one JSON object and attach trusted boundary metadata."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Vibe final answer is empty")
    try:
        proposal = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("Vibe final answer must be one JSON object without Markdown fences") from exc
    return proposal_to_handoff(
        proposal,
        generated_at=datetime.now(tz=timezone.utc),
        manifest_sha256=manifest_sha256,
        allowed_symbols=allowed_symbols,
        broker_by_source=broker_by_source,
    )


def install_candidate_result_writer(
    legacy,
    *,
    candidate_output: Path,
    manifest_sha256: str,
    allowed_symbols: set[str],
    broker_by_source: dict[str, str],
) -> None:
    """Persist only schema-valid agent output and keep raw model text out of stdout."""
    candidate_output = candidate_output.resolve()

    def print_validated_result(result: dict) -> None:
        summary = {
            "status": result.get("status", "unknown"),
            "run_id": result.get("run_id"),
            "run_dir": result.get("run_dir"),
            "reason": result.get("reason"),
            "candidate_output": None,
            "candidate_count": 0,
        }
        if result.get("status") == "success":
            try:
                handoff = parse_agent_proposal(
                    result.get("content", ""),
                    manifest_sha256=manifest_sha256,
                    allowed_symbols=allowed_symbols,
                    broker_by_source=broker_by_source,
                )
                candidate_output.parent.mkdir(parents=True, exist_ok=True)
                temporary = candidate_output.with_suffix(candidate_output.suffix + ".tmp")
                temporary.write_text(
                    json.dumps(handoff, indent=2, sort_keys=True, allow_nan=False) + "\n",
                    encoding="utf-8",
                )
                temporary.replace(candidate_output)
                summary["candidate_output"] = str(candidate_output)
                summary["candidate_count"] = len(handoff["candidates"])
            except Exception as exc:
                result["status"] = "failed"
                result["reason"] = f"candidate_handoff_rejected: {exc}"
                summary["status"] = "failed"
                summary["reason"] = result["reason"]
        print(json.dumps(summary, ensure_ascii=True, allow_nan=False))

    legacy._print_json_result = print_validated_result


def main() -> int:
    args = parse_args()
    import src.tools as vibe_tools

    install_safe_registry_projection(vibe_tools)
    if args.audit_tools:
        registry = vibe_tools.build_registry(
            persistent_memory=None,
            include_shell_tools=True,
            agent_config=None,
            interactive=False,
        )
        names = sorted(registry.tool_names)
        if not names:
            raise SystemExit("Safe Vibe tool projection is empty")
        print(
            json.dumps(
                {
                    "schema": "mt5.vibe_tool_audit.v1",
                    "order_authority": False,
                    "tool_count": len(names),
                    "tools": names,
                },
                indent=2,
            )
        )
        return 0

    if args.prompt_file is None:
        raise SystemExit("--prompt-file is required unless --audit-tools is used")
    if args.bundle_manifest is None or args.candidate_output is None:
        raise SystemExit("--bundle-manifest and --candidate-output are required for agent research")
    prompt = args.prompt_file.read_text(encoding="utf-8")
    if not prompt.strip():
        raise SystemExit("Research prompt is empty")

    import cli._legacy as legacy

    manifest_sha256, allowed_symbols, broker_by_source = load_bundle_contract(
        args.bundle_manifest
    )
    if args.candidate_output.resolve().is_relative_to(args.bundle_manifest.resolve().parent):
        raise SystemExit("Candidate output must not modify the immutable research bundle")
    install_candidate_result_writer(
        legacy,
        candidate_output=args.candidate_output,
        manifest_sha256=manifest_sha256,
        allowed_symbols=allowed_symbols,
        broker_by_source=broker_by_source,
    )

    max_iterations = min(max(int(args.max_iter), 1), 20)
    print(
        "Vibe research boundary: read-only allowlist, no shell/MCP/connectors/orders; "
        f"max_iterations={max_iterations}",
        file=sys.stderr,
    )
    return int(
        legacy.cmd_run(
            prompt,
            max_iterations,
            json_mode=True,
            no_rich=True,
        )
        or 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
