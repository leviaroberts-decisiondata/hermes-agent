"""Queue-owned pricing-only CLI transaction; no config or home writes.

Reuses the existing CLI lease, approval binding, CAS journal and exact rollback.
Only the versioned admission adapter may realize its generated manifest.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import sys

if __package__:
    from scripts import dd_hermes_ordinary_release as common
    from scripts import dd_hermes_pricing_component as pricing
else:
    import dd_hermes_ordinary_release as common
    import dd_hermes_pricing_component as pricing

KIND = "hermes_cli_pricing_v1"
EXTRA_HELPERS = ("scripts/dd_hermes_cli_pricing_release.py", "scripts/run_dd_cli_pricing_release_tests.py",
                 "tests/test_gpt62_cli_attribution.py", "tests/test_gpt62_pricing_attribution.py",
                 "tests/test_gpt62_component_attribution.py", "tests/scripts/test_dd_hermes_profile_release.py",
                 "tests/scripts/test_dd_hermes_ordinary_release.py")
PRICE_OBSERVATION = '''
        from agent import usage_pricing as prices
        counts = prices.CanonicalUsage(input_tokens=3000, output_tokens=2000,
            cache_read_tokens=4000, cache_write_tokens=3000, reasoning_tokens=1500,
            **{"cache_write_30m_tokens": 2000, "cache_write_12h_tokens": 1000})
        def price(model, usage=counts, **kwargs):
            value = prices.estimate_usage_cost(model, usage, provider="openai", **kwargs).amount_usd
            return str(value) if value is not None else None
        minimal = prices.CanonicalUsage(input_tokens=1000, output_tokens=100)
        included = prices.estimate_usage_cost("gpt-6.2-astra", minimal, provider="openai-codex")
        result["pricing"] = {
            "sources": {name: hashlib.sha256((pathlib.Path(root) / name).read_bytes()).hexdigest()
                        for name in ("agent/usage_pricing.py", "dd_obs.py", "run_agent.py")},
            "origin": prices.__file__, "astra_mixed": price("gpt-6.2-astra"),
            "sol_mixed": price("gpt-6.2-sol"),
            "unknown_write": price("gpt-6.2-astra", prices.CanonicalUsage(cache_write_tokens=1)),
            "ultrafast_before": price("gpt-6.2-astra", minimal, service_tier="ultrafast", requested_at="2027-03-01T07:59:59Z"),
            "ultrafast_after": price("gpt-6.2-astra", minimal, service_tier="ultrafast", requested_at="2027-03-01T08:00:00Z"),
            "included_status": included.status, "included_cost": str(included.amount_usd),
            "legacy_gpt4o": price("gpt-4o", minimal)}
'''


def expected_prices(root, hashes):
    return {"sources": {name: hashes[str(root / name)] for name in pricing.PATHS},
            "origin": str(root / "agent/usage_pricing.py"), "astra_mixed": "0.1456",
            "sol_mixed": "0.0364", "unknown_write": None,
            "ultrafast_before": "0.036", "ultrafast_after": "0.072",
            "included_status": "included", "included_cost": "0", "legacy_gpt4o": "0.0035"}


def engine_for(artifact, target, pricing_commit):
    if any(not re.fullmatch(r"[0-9a-f]{40}", value or "") for value in (target, pricing_commit)):
        raise common.Refused("exact_pricing_commits_required")
    artifact = common.safe_path(Path(artifact))
    if artifact.resolve() != artifact or not artifact.is_dir() or not artifact.is_relative_to(common.APPS):
        raise common.Refused("pricing_artifact_path_refused")
    source = Path(__file__).with_name("dd_hermes_cli_release.py")
    spec = importlib.util.spec_from_file_location("scripts._pricing_cli_transaction", source)
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    engine.BASELINE_COMPONENT = engine.TARGET  # retained transcription/config artifact
    engine.TARGET, engine.ARTIFACT, engine.KIND = target, artifact, KIND
    engine.LINEAGE_PRICING_COMMIT = pricing_commit
    engine.SOURCES = tuple(engine.SOURCES) + pricing.PATHS
    engine.CONFIG_PROFILES = ()
    engine.EXTRA_HELPER_NAMES = EXTRA_HELPERS
    engine.PRESERVE_UNCHANGED = True
    marker = "        if blocked: raise RuntimeError(\"blocked\")"
    if engine.VERIFY_IMPORTS.count(marker) != 1:
        raise common.Refused("consumer_inspector_contract_changed")
    engine.VERIFY_IMPORTS = engine.VERIFY_IMPORTS.replace(marker, PRICE_OBSERVATION + marker)

    def apply_command(path, checksum):
        return shlex.join([str(engine.PYTHON), "-B", str(Path(__file__).resolve()), "apply",
                           "--manifest", str(path), "--manifest-sha256", checksum, "--queue-id", "auto"])
    engine.apply_command = apply_command
    original_read = engine.read_manifest
    def read_manifest(path, checksum):
        result = original_read(path, checksum)
        if result.get("artifact_path") != str(artifact) or result.get("pricing_reviewed_commit") != pricing_commit:
            raise common.Refused("pricing_manifest_binding_changed")
        return result
    engine.read_manifest = read_manifest

    class PricingHost(engine.Host):
        def consumers(self, deadline):
            environment = engine.environment_identity()
            if engine.PATH_CONSOLE.resolve() != engine.CONSOLE.resolve():
                raise common.Refused("path_console_resolution_changed")
            result = {}
            hashes = {str(engine.ROOT / name): common.digest(engine.ROOT / name) for name in engine.SOURCES}
            for profile, home in engine.HOMES.items():
                result[profile] = {}
                for mode in ("console", "path_console", "module"):
                    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG") if k in os.environ}
                    env.update(HERMES_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
                    env["PATH"] = str(engine.CONSOLE.parent) + os.pathsep + env.get("PATH", "")
                    console = engine.PATH_CONSOLE if mode == "path_console" else engine.CONSOLE
                    rc, raw = self.command([str(engine.PYTHON), "-B", "-c", engine.VERIFY_IMPORTS,
                                            mode, str(engine.ROOT), str(console)], deadline, True,
                                           cwd=Path("/private/tmp"), env=env)
                    if rc:
                        raise common.Refused("pricing_consumer_inspection_failed")
                    value = json.loads(raw)
                    expected = {"main": str(engine.ROOT / "hermes_cli/main.py"),
                                "transcription": str(engine.ROOT / engine.SOURCES[0]),
                                "config": str(engine.ROOT / engine.SOURCES[1]),
                                "transcription_sha256": hashes[str(engine.ROOT / engine.SOURCES[0])],
                                "config_sha256": hashes[str(engine.ROOT / engine.SOURCES[1])],
                                "provider": "dgx", "home": str(home),
                                "pricing": expected_prices(engine.ROOT, hashes)}
                    if value != expected:
                        raise common.Refused("pricing_consumer_identity_or_cost_mismatch")
                    result[profile][mode] = value
            if engine.environment_identity() != environment:
                raise common.Refused("environment_changed_during_inspection")
            return result
    engine.Host = PricingHost
    return engine


def stage(engine, reviewed, host=None):
    result = engine.stage(reviewed, host)
    result.update(artifact_path=str(engine.ARTIFACT), pricing_reviewed_commit=engine.LINEAGE_PRICING_COMMIT)
    return result


def main():
    def forbid_auth(event, args):
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            common.safe_path(Path(os.fsdecode(args[0])).absolute())
    sys.addaudithook(forbid_auth)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "apply", "verify"))
    for name in ("artifact", "target", "pricing-reviewed-commit", "reviewed-commit", "manifest", "manifest-sha256"):
        parser.add_argument("--" + name)
    parser.add_argument("--queue-id", default="auto")
    parser.add_argument("--challenge", default="")
    parser.add_argument("--attempt-id", default="")
    args = parser.parse_args()
    try:
        if args.action == "stage":
            engine = engine_for(args.artifact, args.target, args.pricing_reviewed_commit)
            result = stage(engine, args.reviewed_commit)
        else:
            raw = common.safe_path(Path(args.manifest)).read_bytes()
            if common.sha(raw) != args.manifest_sha256:
                raise common.Refused("manifest_digest_mismatch")
            manifest = json.loads(raw)
            if manifest.get("kind") != KIND:
                raise common.Refused("pricing_manifest_kind_required")
            engine = engine_for(manifest["artifact_path"], manifest["target_commit"], manifest["pricing_reviewed_commit"])
            if args.action == "apply":
                result = engine.apply(args.manifest, args.manifest_sha256, args.queue_id)
            else:
                result = engine.verify(args.manifest, args.manifest_sha256, args.queue_id,
                                       challenge=args.challenge, attempt_id=args.attempt_id)
        print(json.dumps(result, sort_keys=True))
        return 0 if args.action == "stage" or result["status"] in {"succeeded", "verified"} else 1
    except Exception as exc:
        print(json.dumps({"status": "refused", "reason": common.safe_reason(exc)}))
        return 1


if __name__ == "__main__":
    main()
