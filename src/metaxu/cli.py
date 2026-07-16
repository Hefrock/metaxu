"""Metaxu command-line inspector.

Commands:
    metaxu inspect <artifact.json>              Human-readable summary
    metaxu validate <artifact.json>             Schema + structural validation
    metaxu verify <artifact.json> --snapshots d Re-verify provenance hashes
    metaxu mcp-proxy [opts] -- <server cmd>     Transparent MCP assurance proxy
    metaxu merge -o out.json a.json b.json ...  Merge partial artifacts (one interaction)
    metaxu report <dir> [--json | --html out]   Governance report over an artifact store
    metaxu drift <baseline> <current> [--json]  Detect drift between artifact cohorts
    metaxu diff <original> <replay> [--json]    Compare two artifacts of one interaction
    metaxu replay <artifact> --runner mod:fn    Re-run the workflow and diff the result
    metaxu graph <artifact> [--format ...]      Render the evidence graph
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
    correlation = artifact.correlation
    if correlation:
        role = correlation.get("role", "partial")
        observer = correlation.get("observer", "unknown")
        print(f"  view:       {role} (observer: {observer})")
        print(f"  interaction: {correlation.get('interaction_id')}")
        if role == "merged":
            print(f"  merged from: {', '.join(correlation.get('merged_from', []))}")
        else:
            print(
                "              (a single observer's view — merge with other "
                "observers of this interaction via `metaxu merge`)"
            )
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
        if check.get("unmet"):
            line += f" (condition not met: {', '.join(check['unmet'])})"
        print(line)

    print(f"\nSafety findings ({len(artifact.safety_checks)}):")
    if not artifact.safety_checks:
        print("  (none)")
    for finding in artifact.safety_checks:
        print(f"  - [{finding['severity']}] {finding['check']}: {finding['message']}")

    if artifact.terminology:
        malformed = sum(1 for t in artifact.terminology if not t.get("valid"))
        version = artifact.terminology[0].get("terminology_version", "?")
        print(
            f"\nTerminology ({len(artifact.terminology)} code(s), "
            f"{malformed} malformed, checked against {version}):"
        )
        for result in artifact.terminology:
            mark = "ok " if result["valid"] else "BAD"
            print(f"  [{mark}] {result['system']} {result['code']} ({result['status']})")

    conflicts = artifact.metadata.get("dev.metaxu/merge_conflicts", [])
    if conflicts:
        print(f"\nMerge conflicts ({len(conflicts)}) — observers disagreed:")
        for conflict in conflicts:
            print(
                f"  - {conflict['field']}: kept {conflict['kept_from']}'s value, "
                f"discarded {conflict['discarded_from']}'s ({conflict['discarded']!r})"
            )

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
    from .adapters.mcp import run_proxy

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
        interaction_id=args.interaction_id,
    )
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    from .merge import merge_artifacts
    from .policy import PolicyEngine

    artifacts = [_load(path) for path in args.artifacts]
    engine = PolicyEngine.from_file(args.policies) if args.policies else None
    try:
        merged = merge_artifacts(artifacts, policy_engine=engine)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    merged.save(args.out)
    conflicts = merged.metadata.get("dev.metaxu/merge_conflicts", [])
    print(
        f"merged {len(artifacts)} artifacts -> {args.out} "
        f"({len(merged.events)} events, {len(merged.provenance)} provenance records, "
        f"{len(conflicts)} conflict(s))"
    )
    for conflict in conflicts:
        print(
            f"  conflict on {conflict['field']}: kept value from "
            f"{conflict['kept_from']}, discarded value from {conflict['discarded_from']}"
        )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .governance import aggregate_artifacts, load_artifacts, render_text

    artifacts = load_artifacts(args.directory)
    report = aggregate_artifacts(artifacts)
    if args.json:
        print(json.dumps(report, indent=2))
    elif args.html:
        from .dashboard import render_html

        with open(args.html, "w", encoding="utf-8") as f:
            f.write(render_html(report, title=args.title))
        print(f"wrote {args.html} ({report['artifact_count']} artifact(s))")
    else:
        print(render_text(report))
    if args.fail_on_review and report["needs_review"]:
        return 1
    return 0


def cmd_drift(args: argparse.Namespace) -> int:
    from . import drift as drift_mod
    from .governance import load_artifacts

    baseline = load_artifacts(args.baseline)
    current = load_artifacts(args.current)
    report = drift_mod.compare_cohorts(baseline, current, threshold=args.threshold)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(drift_mod.render_text(report))
    if args.fail_on_drift and report["flags"]:
        return 1
    return 0


def _print_diff(diff: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(diff, indent=2))
        return
    print(f"original: {diff['original_artifact']}")
    print(f"replay:   {diff['replay_artifact']}")
    print(f"reproduced: {'YES' if diff['reproduced'] else 'NO'}")
    for aspect in ("answer", "tool_calls", "claims", "policies"):
        print(f"  {aspect:<12} {'match' if diff[aspect]['match'] else 'DIFFER'}")
    provenance = diff["provenance"]
    print(
        f"  provenance   {provenance['shared_resources']} shared resource(s), "
        f"{len(provenance['hash_mismatches'])} hash mismatch(es)"
    )
    if diff["differences"]:
        print("Differences:")
        for difference in diff["differences"]:
            print(f"  - {difference}")


def cmd_diff(args: argparse.Namespace) -> int:
    from .replay import diff_artifacts

    diff = diff_artifacts(_load(args.original), _load(args.replay))
    _print_diff(diff, args.json)
    if args.fail_on_diff and not diff["reproduced"]:
        return 1
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    import importlib
    import os as _os
    import sys as _sys

    from .policy import PolicyEngine
    from .replay import replay_with_runner

    module_name, _, func_name = args.runner.partition(":")
    if not module_name or not func_name:
        print("error: --runner must be 'module.path:function'", file=sys.stderr)
        return 2
    _sys.path.insert(0, _os.getcwd())
    try:
        runner = getattr(importlib.import_module(module_name), func_name)
    except (ImportError, AttributeError) as exc:
        print(f"error: could not load runner {args.runner!r}: {exc}", file=sys.stderr)
        return 2

    artifact = _load(args.artifact)
    engine = PolicyEngine.from_file(args.policies) if args.policies else None
    replay_artifact, diff = replay_with_runner(artifact, runner, policy_engine=engine)
    if args.out:
        replay_artifact.save(args.out)
    _print_diff(diff, args.json)
    if args.out and not args.json:
        print(f"replay artifact written to {args.out}")
    return 0 if diff["reproduced"] else 1


def cmd_graph(args: argparse.Namespace) -> int:
    from .graph import EvidenceGraph

    graph = EvidenceGraph.from_artifact(_load(args.artifact))

    if args.dependents:
        matches = graph.find(args.dependents)
        if not matches:
            print(f"error: no node matching {args.dependents!r}", file=sys.stderr)
            return 2
        for node in matches:
            print(f"Dependents of {node.type} {node.label} ({node.id}):")
            dependents = graph.dependents(node.id)
            if not dependents:
                print("  (nothing rests on this node)")
            for dep in dependents:
                print(f"  - {dep.type}: {dep.label}")
        return 0

    if args.format == "json":
        print(graph.to_json())
    elif args.format == "mermaid":
        print(graph.to_mermaid())
    elif args.format == "dot":
        print(graph.to_dot())
    else:
        print(graph.render_text())
    return 0


def _fmt_args(arguments: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in arguments.items())


def main(argv: list[str] | None = None) -> int:
    import signal

    if hasattr(signal, "SIGPIPE"):
        # Die quietly when piped into head/less instead of tracebacking.
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

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
        "--interaction-id",
        default=None,
        help="correlation id shared with other observers (default: METAXU_INTERACTION_ID env var)",
    )
    p_proxy.add_argument(
        "server_command",
        nargs=argparse.REMAINDER,
        help="the real MCP server command (prefix with --)",
    )
    p_proxy.set_defaults(func=cmd_mcp_proxy)

    p_merge = sub.add_parser(
        "merge",
        help="merge partial artifacts from multiple observers of one interaction",
    )
    p_merge.add_argument("artifacts", nargs="+", help="partial artifacts, most authoritative first")
    p_merge.add_argument("-o", "--out", required=True, help="output path for the merged artifact")
    p_merge.add_argument(
        "--policies", default=None, help="policy pack (JSON/YAML) to re-evaluate on merge"
    )
    p_merge.set_defaults(func=cmd_merge)

    p_report = sub.add_parser(
        "report",
        help="aggregate governance metrics over a directory of artifacts",
    )
    p_report.add_argument("directory", help="artifact store to aggregate (searched recursively)")
    output = p_report.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="emit the report as JSON")
    output.add_argument("--html", default=None, help="write a self-contained HTML dashboard here")
    p_report.add_argument(
        "--title", default="Metaxu governance report", help="title for the HTML dashboard"
    )
    p_report.add_argument(
        "--fail-on-review",
        action="store_true",
        help="exit 1 if any artifact needs review (for CI gates)",
    )
    p_report.set_defaults(func=cmd_report)

    p_drift = sub.add_parser(
        "drift",
        help="detect environment, behavior, answer, and source drift between two artifact cohorts",
    )
    p_drift.add_argument("baseline", help="baseline artifact store (searched recursively)")
    p_drift.add_argument("current", help="current artifact store (searched recursively)")
    p_drift.add_argument("--json", action="store_true", help="emit the report as JSON")
    p_drift.add_argument(
        "--threshold",
        type=float,
        default=0.1,
        help="minimum regression in a rate/score to flag (default 0.1)",
    )
    p_drift.add_argument(
        "--fail-on-drift",
        action="store_true",
        help="exit 1 if any drift flags are raised (for CI gates)",
    )
    p_drift.set_defaults(func=cmd_drift)

    p_diff = sub.add_parser(
        "diff", help="compare two artifacts that describe the same interaction"
    )
    p_diff.add_argument("original")
    p_diff.add_argument("replay")
    p_diff.add_argument("--json", action="store_true", help="emit the diff as JSON")
    p_diff.add_argument(
        "--fail-on-diff", action="store_true", help="exit 1 unless fully reproduced"
    )
    p_diff.set_defaults(func=cmd_diff)

    p_replay = sub.add_parser(
        "replay",
        help="re-run the workflow for an artifact's question and diff against the original",
    )
    p_replay.add_argument("artifact")
    p_replay.add_argument(
        "--runner",
        required=True,
        help="python entrypoint 'module.path:function' taking (question, session)",
    )
    p_replay.add_argument("--policies", default=None, help="policy pack for the replay session")
    p_replay.add_argument("--out", default=None, help="write the replay artifact here")
    p_replay.add_argument("--json", action="store_true", help="emit the diff as JSON")
    p_replay.set_defaults(func=cmd_replay)

    p_graph = sub.add_parser(
        "graph", help="render an artifact's evidence graph (the reasoning chain)"
    )
    p_graph.add_argument("artifact")
    p_graph.add_argument(
        "--format",
        choices=["text", "json", "mermaid", "dot"],
        default="text",
        help="output format (default: text tree)",
    )
    p_graph.add_argument(
        "--dependents",
        default=None,
        metavar="NODE",
        help="impact analysis: list everything resting on nodes matching this id/label fragment",
    )
    p_graph.set_defaults(func=cmd_graph)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
