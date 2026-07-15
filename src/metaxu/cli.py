"""Metaxu command-line inspector.

Commands:
    metaxu inspect <artifact.json>              Human-readable summary
    metaxu validate <artifact.json>             Schema + structural validation
    metaxu verify <artifact.json> --snapshots d Re-verify provenance hashes
    metaxu mcp-proxy [opts] -- <server cmd>     Transparent MCP assurance proxy
"""

from __future__ import annotations

import argparse
import importlib.resources
import json
import sys

from .artifact import AssuranceArtifact
from .replay import snapshot_resolver, verify


def _load(path: str) -> AssuranceArtifact:
    try:
        return AssuranceArtifact.load(path)
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"error: could not load artifact {path!r}: {exc}", file=sys.stderr)
        raise SystemExit(2)


def cmd_inspect(args: argparse.Namespace) -> int:
    artifact = _load(args.artifact)
    print(f"Assurance Artifact {artifact.id}")
    print(f"  schema:     {artifact.schema_version}")
    print(f"  created:    {artifact.created_at}")
    print(f"  integrity:  {'ok' if AssuranceArtifact.verify_file(args.artifact) else 'FAILED'}")
    print()
    print(f"Question: {artifact.question}")
    print(f"Answer:   {artifact.answer or '(none recorded)'}")

    print(f"\nTool trace ({len(artifact.tool_trace)} calls):")
    for call in artifact.tool_trace:
        payload = call.get("payload", {})
        status = "error" if payload.get("error") else "ok"
        duration = payload.get("duration_ms")
        timing = f" [{duration:.1f}ms]" if isinstance(duration, (int, float)) else ""
        print(f"  - {call['name']}({_fmt_args(payload.get('arguments', {}))}) -> {status}{timing}")

    print(f"\nProvenance ({len(artifact.provenance)} resources):")
    for record in artifact.provenance:
        print(
            f"  - {record.resource_type}/{record.resource_id}"
            f" from {record.source_system} @ {record.retrieved_at} ({record.cache_state})"
        )

    print(f"\nPolicy checks ({len(artifact.policy_checks)}):")
    for check in artifact.policy_checks:
        if not check.get("triggered"):
            print(f"  - {check['policy']}: not triggered")
            continue
        status = "PASS" if check.get("passed") else "FAIL"
        line = f"  - {check['policy']}: {status}"
        if check.get("missing"):
            line += f" (missing: {', '.join(check['missing'])})"
        if check.get("errored"):
            line += f" (attempted but errored: {', '.join(check['errored'])})"
        print(line)

    print(f"\nSafety findings ({len(artifact.safety_checks)}):")
    if not artifact.safety_checks:
        print("  (none)")
    for finding in artifact.safety_checks:
        print(f"  - [{finding['severity']}] {finding['check']}: {finding['message']}")

    if artifact.missing_data:
        print(f"\nMissing data ({len(artifact.missing_data)}):")
        for item in artifact.missing_data:
            reason = f" — {item['reason']}" if item.get("reason") else ""
            print(f"  - {item['item']}{reason}")

    print("\nTrust dimensions:")
    for name, dim in sorted(artifact.trust_scores.items()):
        print(f"  - {name}: {dim['score']:.2f}  {dim['rationale']}")

    repro = artifact.reproducibility
    if repro:
        print("\nReproducibility:")
        for key in ("model", "prompt_version", "python_version", "platform"):
            if key in repro:
                print(f"  - {key}: {repro[key]}")
        for tool, version in repro.get("tool_versions", {}).items():
            print(f"  - tool {tool}: {version}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    with open(args.artifact, encoding="utf-8") as f:
        data = json.load(f)
    schema_text = (
        importlib.resources.files("metaxu.spec")
        .joinpath("assurance-artifact.schema.json")
        .read_text(encoding="utf-8")
    )
    schema = json.loads(schema_text)
    try:
        import jsonschema
    except ImportError:
        # Structural fallback so validation works without optional deps.
        missing = [key for key in schema["required"] if key not in data]
        if missing:
            print(f"INVALID: missing required fields: {', '.join(missing)}")
            return 1
        print(
            "valid (structural check only — install metaxu[schema] for full "
            "JSON Schema validation)"
        )
        return 0
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        print(f"INVALID: {len(errors)} schema violation(s)")
        for error in errors:
            location = "/".join(str(p) for p in error.path) or "(root)"
            print(f"  - {location}: {error.message}")
        return 1
    print("valid")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    artifact = _load(args.artifact)
    resolver = snapshot_resolver(args.snapshots) if args.snapshots else None
    report = verify(artifact, resolver)
    print(f"integrity:  {'ok' if report.integrity_ok else 'FAILED'}")
    if resolver is not None:
        print(
            f"provenance: {report.provenance_matched}/{report.provenance_checked} "
            f"hashes match"
        )
        for miss in report.mismatches:
            print(f"  - CHANGED since retrieval: {miss['resource']}")
        for unresolved in report.unresolved:
            print(f"  - unresolved (no snapshot): {unresolved}")
    print(f"result:     {'ok' if report.ok else 'FAILED'}")
    return 0 if report.ok else 1


def cmd_mcp_proxy(args: argparse.Namespace) -> int:
    from .mcp_proxy import run_proxy

    command = args.server_command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("error: no server command given (metaxu mcp-proxy -- <cmd> ...)", file=sys.stderr)
        return 2
    run_proxy(
        server_command=command,
        out_dir=args.out,
        question=args.question,
        policy_file=args.policies,
        tags_file=args.tags,
        snapshots=not args.no_snapshots,
    )
    return 0


def _fmt_args(arguments: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in arguments.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="metaxu", description="Inspect and verify Metaxu assurance artifacts."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser("inspect", help="print a human-readable artifact summary")
    p_inspect.add_argument("artifact")
    p_inspect.set_defaults(func=cmd_inspect)

    p_validate = sub.add_parser("validate", help="validate an artifact against the schema")
    p_validate.add_argument("artifact")
    p_validate.set_defaults(func=cmd_validate)

    p_verify = sub.add_parser("verify", help="verify integrity and provenance hashes")
    p_verify.add_argument("artifact")
    p_verify.add_argument("--snapshots", help="directory of resource snapshots", default=None)
    p_verify.set_defaults(func=cmd_verify)

    p_proxy = sub.add_parser(
        "mcp-proxy",
        help="wrap an MCP stdio server, recording an assurance artifact",
    )
    p_proxy.add_argument("--out", default="metaxu-artifacts", help="artifact output directory")
    p_proxy.add_argument("--question", default=None, help="question/task to record on the artifact")
    p_proxy.add_argument("--policies", default=None, help="policy pack (JSON/YAML) to evaluate")
    p_proxy.add_argument(
        "--tags", default=None, help="JSON file mapping tool names to policy tags"
    )
    p_proxy.add_argument(
        "--no-snapshots", action="store_true", help="do not snapshot retrieved content"
    )
    p_proxy.add_argument(
        "server_command",
        nargs=argparse.REMAINDER,
        help="the real MCP server command (prefix with --)",
    )
    p_proxy.set_defaults(func=cmd_mcp_proxy)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
