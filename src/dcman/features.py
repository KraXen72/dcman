from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .container import devcontainer_feature_metadata, devcontainer_feature_refs
from .rendering import render_table


@dataclass(frozen=True)
class FeatureVersion:
	ref: str
	name: str | None
	version: str | None
	canonical_id: str | None
	pinned: bool
	warning: str | None = None


def _feature_tag(ref: str) -> str | None:
	# Digest-pinned refs do not have a mutable tag to report.
	if "@" in ref.rsplit("/", 1)[-1]:
		return None
	last = ref.rsplit("/", 1)[-1]
	if ":" not in last:
		return "latest"
	return last.rsplit(":", 1)[1]


def _is_exact_semver(tag: str | None) -> bool:
	if tag is None:
		return False
	return bool(re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", tag))


def resolve_feature_versions(workspace: Path) -> list[FeatureVersion]:
	versions: list[FeatureVersion] = []
	for ref in devcontainer_feature_refs(workspace):
		tag = _feature_tag(ref)
		if _is_exact_semver(tag):
			# Exact semver tags are already explicit in the config; avoid a
			# registry lookup unless the ref is floating like :1 or :latest.
			versions.append(
				FeatureVersion(ref=ref, name=None, version=tag.lstrip("v") if tag else None, canonical_id=None, pinned=True)
			)
			continue

		try:
			name, version, canonical_id = devcontainer_feature_metadata(ref)
		except Exception as exc:
			# Version reporting is diagnostic only. Keep startup usable even if a
			# registry is offline or the devcontainer CLI cannot inspect a ref.
			versions.append(FeatureVersion(ref=ref, name=None, version=tag, canonical_id=None, pinned=False, warning=str(exc)))
			continue

		versions.append(FeatureVersion(ref=ref, name=name, version=version or tag, canonical_id=canonical_id, pinned=False))
	return versions


def format_feature_versions(versions: list[FeatureVersion]) -> str | None:
	if not versions:
		return None

	lines = ["Resolved devcontainer feature versions:"]
	headers = ("feature", "version", "source", "digest")
	rows: list[tuple[str, str, str, str]] = []
	for feature in versions:
		name = feature.name or feature.ref.rsplit("/", 1)[-1]
		version = feature.version or "unknown"
		resolved_from = feature.ref
		digest = ""
		if feature.canonical_id and "@sha256:" in feature.canonical_id:
			# Full digests make the report hard to scan; a sha256 prefix is enough
			# to correlate with devcontainer CLI logs during debugging.
			digest = feature.canonical_id.rsplit("@", 1)[1][:19]
		if feature.warning:
			digest = f"warning: {feature.warning}"
		rows.append((name, version, resolved_from, digest))

	lines.append(render_table(headers, rows))
	return "\n".join(lines)
