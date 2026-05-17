from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .container import resolve_devcontainer_config_path


@dataclass(frozen=True)
class FeatureVersion:
	ref: str
	name: str | None
	version: str | None
	canonical_id: str | None
	pinned: bool
	warning: str | None = None


def _strip_jsonc(text: str) -> str:
	# Devcontainer files are commonly JSONC. This small stripper handles comments
	# and trailing commas without pulling in a parser dependency for one field.
	result: list[str] = []
	i = 0
	in_string = False
	escape = False
	while i < len(text):
		ch = text[i]
		nxt = text[i + 1] if i + 1 < len(text) else ""
		if in_string:
			result.append(ch)
			if escape:
				escape = False
			elif ch == "\\":
				escape = True
			elif ch == '"':
				in_string = False
			i += 1
			continue
		if ch == '"':
			in_string = True
			result.append(ch)
			i += 1
			continue
		if ch == "/" and nxt == "/":
			while i < len(text) and text[i] not in "\r\n":
				i += 1
			continue
		if ch == "/" and nxt == "*":
			i += 2
			while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
				i += 1
			i += 2
			continue
		result.append(ch)
		i += 1

	return re.sub(r",\s*([}\]])", r"\1", "".join(result))


def _feature_refs_from_config(config_path: Path) -> list[str]:
	try:
		text = config_path.read_text()
	except OSError:
		return []
	try:
		data = json.loads(text)
	except json.JSONDecodeError:
		try:
			data = json.loads(_strip_jsonc(text))
		except json.JSONDecodeError:
			return []

	features = data.get("features") if isinstance(data, dict) else None
	if not isinstance(features, dict):
		return []
	return [ref for ref in features if isinstance(ref, str)]


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


def _metadata_from_verbose_info(ref: str) -> tuple[str | None, str | None, str | None]:
	# Let the official devcontainer CLI resolve floating tags and registry
	# metadata, so dcman does not need to understand every registry detail.
	result = subprocess.run(
		[
			"devcontainer",
			"features",
			"info",
			"verbose",
			ref,
			"--output-format",
			"json",
		],
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
		check=False,
	)
	if result.returncode != 0:
		detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
		raise RuntimeError(detail)

	payload = json.loads(result.stdout)
	canonical_id = payload.get("canonicalId")
	annotations = payload.get("manifest", {}).get("annotations", {})
	metadata_raw = annotations.get("dev.containers.metadata")
	metadata = json.loads(metadata_raw) if isinstance(metadata_raw, str) else {}
	name = metadata.get("name") if isinstance(metadata.get("name"), str) else None
	version = metadata.get("version") if isinstance(metadata.get("version"), str) else None
	return name, version, canonical_id if isinstance(canonical_id, str) else None


def resolve_feature_versions(workspace: Path) -> list[FeatureVersion]:
	config_path = resolve_devcontainer_config_path(workspace)
	if config_path is None:
		return []

	versions: list[FeatureVersion] = []
	for ref in _feature_refs_from_config(config_path):
		tag = _feature_tag(ref)
		if _is_exact_semver(tag):
			# Exact semver tags are already explicit in the config; avoid a
			# registry lookup unless the ref is floating like :1 or :latest.
			versions.append(
				FeatureVersion(ref=ref, name=None, version=tag.lstrip("v") if tag else None, canonical_id=None, pinned=True)
			)
			continue

		try:
			name, version, canonical_id = _metadata_from_verbose_info(ref)
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

	widths = [len(header) for header in headers]
	for row in rows:
		for idx in range(4):
			widths[idx] = max(widths[idx], len(row[idx]))

	table_width = sum(widths) + 2 * (len(widths) - 1)
	lines.append(f"  {headers[0]:<{widths[0]}}  {headers[1]:>{widths[1]}}  {headers[2]:<{widths[2]}}  {headers[3]:<{widths[3]}}")
	lines.append(f"  {'-' * table_width}")
	for name, version, resolved_from, digest in rows:
		lines.append(f"  {name:<{widths[0]}}  {version:>{widths[1]}}  {resolved_from:<{widths[2]}}  {digest:<{widths[3]}}")
	lines.append(f"  {'-' * table_width}")
	return "\n".join(lines)
