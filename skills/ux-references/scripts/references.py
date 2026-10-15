#!/usr/bin/env python3
"""Local, source-neutral UX reference search. Python standard library only."""
import argparse
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

from library import file_signature, library_root, local_file, require_library
VERSION = 1
LANDING_TYPES = {"landing", "hero"}


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


local = local_file


def url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError(f"Expected an HTTP(S) source URL: {value!r}")


def site_location(value):
    """Product host and page, independent of protocol, www, and tracking parameters."""
    url(value)
    parsed = urlsplit(value)
    host = parsed.hostname.rstrip(".").lower().encode("idna").decode("ascii")
    host = host.removeprefix("www.")
    if parsed.username or parsed.password:
        raise ValueError("site_url must not contain credentials")
    if parsed.port and parsed.port not in {80, 443}:
        host += ":" + str(parsed.port)
    query = urlencode(sorted((key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
                             if not key.casefold().startswith("utm_") and
                             key.casefold() not in {"gclid", "fbclid", "msclkid"}))
    page = (parsed.path.rstrip("/") or "/") + ("?" + query if query else "")
    return host, page


def site_fields(record):
    """Validate optional acquisition identity without classifying app flows as landings."""
    result = {key: record.get(key, "") for key in ("site_url", "site_id", "page_type")}
    for key, value in result.items():
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
    if result["site_id"] and (not result["site_id"].startswith(record["source"] + ":") or
                               len(result["site_id"]) <= len(record["source"]) + 1 or
                               re.search(r"\s", result["site_id"])):
        raise ValueError("site_id must be a stable source-prefixed identifier, e.g. app-archive:site-uuid")
    if result["page_type"] and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", result["page_type"]):
        raise ValueError("page_type must be a lowercase slug, e.g. landing or hero")
    result["site_host"], result["site_page"] = site_location(result["site_url"]) if result["site_url"] else ("", "/")
    return result


def site_match(candidate, record):
    if candidate.get("site_host") and candidate["site_host"] == record.get("site_host"):
        return "site_host"
    if candidate.get("site_id") and candidate["site_id"] == record.get("site_id"):
        return "site_id"
    if ((candidate.get("site_host") and record.get("site_host")) or
            (candidate.get("site_id") and record.get("site_id"))):
        return None
    if (candidate.get("source") and candidate["source"] == record["source"] and candidate.get("product") and
            candidate["product"].strip().casefold() == record["product"].strip().casefold()):
        return "same_source_product"
    return None


def landing_keys(record):
    """A hero crop and its landing page are one acquisition; paths/versions remain distinct."""
    if record.get("page_type") not in LANDING_TYPES:
        return []
    identities = []
    if record.get("site_host"):
        identities.append("host:" + record["site_host"])
    if record.get("site_id"):
        identities.append(record["site_id"])
    # Name lookup also catches an older acquisition that did not yet record a host/site ID.
    identities.append("product:" + record["source"] + ":" + record["product"].strip().casefold())
    version = record.get("revision") or record.get("captured_label", "")
    return [(identity, record["platform"], record.get("site_page", "/"), version) for identity in identities]


def taxonomy(root):
    return read(root / "catalog/taxonomy.json")["tags"]


def classify(text, rules):
    return [rule["id"] for rule in rules if re.search(rule["pattern"], text, re.I) or
            any(alias in text for alias in rule["aliases"] if re.search("[가-힣]", alias))]


def inputs(root):
    config = read(root / "catalog/sources.json")
    if config["schema_version"] != VERSION:
        raise ValueError("Unsupported sources schema_version")
    entries, files = [], {root / "catalog/sources.json", root / "catalog/taxonomy.json"}
    for relative in ('catalog/site-matches.json', 'catalog/site-aliases.json',
                     'knowledge/design-rules/catalog.json'):
        if (root / relative).exists():
            files.add(root / relative)
    for spec in config["inputs"]:
        if spec["format"] not in {"app-archive", "jsonl"}:
            raise ValueError(f"Unknown input format: {spec['format']}")
        for path in sorted(root.glob(spec["glob"])):
            local(root, path.relative_to(root).as_posix())
            entries.append((spec["format"], path))
            files.add(path)
            if spec["format"] == "app-archive":
                for app in read(path)["apps"]:
                    directory = path.parent / app["archiveDir"]
                    for name in ("app.json", "all-flows.json", "flow-analysis.json"):
                        child = directory / name
                        local(root, child.relative_to(root).as_posix())
                        files.add(child)
            else:
                for record in jsonl_records(path):
                    for document in record.get("documents", []):
                        local(root, document["path"])
                        files.add(root / document["path"])
    for path in root.glob("curations/*/references.json"):
        files.add(path)
    signature = hashlib.sha256(encode(file_signature(root, files,
        ("library.py", "references.py", "site_registry.py", "build_site_view.py"))).encode()).hexdigest()
    return entries, signature, len(files)


def curation_notes(root):
    notes = {}
    for path in sorted(root.glob("curations/*/references.json")):
        data = read(path)
        for ref in data.get("references", []):
            notes.setdefault(ref["flowId"], []).append({
                "origin": path.relative_to(root).as_posix(),
                "project": path.parent.name,
                "observation": ref.get("observation", ""),
                "interpretation": ref.get("transfer", ""),
                "caution": ref.get("adaptation", ""),
                "observed_steps": ref.get("observedSteps", []),
                "scope": data.get("evidenceScope", ""),
            })
    return notes


def archive_records(root, selection, curated):
    collection = selection.parent
    selected = read(selection)
    for app in selected["apps"]:
        directory = collection / app["archiveDir"]
        relative = directory.relative_to(root).as_posix()
        metadata = read(directory / "app.json")
        data = read(directory / "all-flows.json")
        analysis = read(directory / "flow-analysis.json")
        if data["status"] != "complete":
            raise ValueError(f"Incomplete app archive mapping: {directory}")
        if data.get("flowCount", len(data["flows"])) != len(data["flows"]) or data.get("screenCount", len(data["screens"])) != len(data["screens"]):
            raise ValueError(f"app archive count mismatch: {directory}")
        if len({f["flowId"] for f in data["flows"]}) != len(data["flows"]):
            raise ValueError(f"Duplicate Flow ID within an app archive snapshot: {directory}")
        revision = metadata["version"]
        identity = lambda f: f"app-archive:{revision}:{f['flowId']}"
        children = {}
        for index, flow in enumerate(data["flows"]):
            parent = flow["parentIndex"]
            if flow["index"] != index or (parent is not None and (type(parent) is not int or not 0 <= parent < index)):
                raise ValueError(f"Invalid Flow hierarchy: {directory}, {flow['flowId']}")
            if parent is not None and data["flows"][parent]["path"] != flow["path"][:-1]:
                raise ValueError(f"Parent path mismatch: {directory}, {flow['flowId']}")
            if not flow["screenIndices"] or any(type(i) is not int or not 0 <= i < len(data["screens"]) for i in flow["screenIndices"]):
                raise ValueError(f"Invalid screen sequence: {directory}, {flow['flowId']}")
            if flow.get("screenCount", len(flow["screenIndices"])) != len(flow["screenIndices"]):
                raise ValueError(f"Flow screen count mismatch: {directory}, {flow['flowId']}")
            children.setdefault(flow["parentIndex"], []).append(identity(flow))
        for flow in data["flows"]:
            notes = []
            for item in analysis.get("strengths", []):
                # Explicit indices take precedence over potentially repeated names.
                matched = (flow["index"] in item["flowIndices"] if "flowIndices" in item
                           else flow["name"] in item.get("flowNames", []))
                if matched:
                    notes.append({
                        "origin": relative + "/flow-analysis.json",
                        "observation": item.get("observed", ""),
                        "interpretation": item.get("opinion", ""),
                        "scope": analysis.get("scope", ""),
                        "evidence_paths": [relative + "/" + p for p in item.get("screens", [])],
                    })
            notes.extend(curated.get(flow["flowId"], []))
            screens = []
            for index in flow["screenIndices"]:
                screen = data["screens"][index]
                screens.append({"path": relative + "/" + screen["path"],
                                "source_url": screen["sourceUrl"],
                                "sha256": screen["sha256"]})
            yield {
                "schema_version": VERSION, "id": identity(flow), "source": "app-archive",
                "kind": "flow", "product": app["name"], "platform": app["platform"],
                "title": flow["name"], "breadcrumb": flow["path"],
                "source_url": flow["url"], "revision": revision,
                "collected_at": metadata.get("savedAt") or metadata.get("capturedAt") or selected["createdAt"],
                "captured_label": metadata.get("captureLabel", ""),
                "collection": collection.relative_to(root).as_posix(),
                "summary": f"{' → '.join(flow['path'])} · 기록된 화면 {len(screens)}장",
                "screens": screens, "notes": notes,
                "local_viewer": relative + "/index.html#flow-" + flow["flowId"],
                "local_metadata": relative + "/all-flows.json",
                "mapping_method": data.get("mappingMethod", "full_gallery_dom"),
                "app_context": {"summary": analysis.get("summary", ""),
                                "patterns": analysis.get("patterns", []),
                                "scope": "서비스 단위의 기존 분석 의견. 개별 Flow의 관찰로 간주하지 않는다."},
                "relations": {
                    "parent": identity(data["flows"][flow["parentIndex"]]) if flow["parentIndex"] is not None else None,
                    "children": children.get(flow["index"], []),
                    "basis": "원본 목록의 계층이며 실제 화면 전환 관계를 보증하지 않는다.",
                },
            }


def jsonl_records(path):
    with path.open(encoding="utf-8") as file:
        for number, line in enumerate(file, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("Each line must be an object")
                yield value
            except ValueError as error:
                raise ValueError(f"{path}:{number}: {error}") from error


def normalize(record, root, rules):
    record = dict(record)
    if record.get("schema_version") != VERSION:
        raise ValueError("Each record requires schema_version: 1")
    for key in ("id", "source", "kind", "product", "platform", "title", "source_url", "collected_at"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise ValueError(f"Missing/non-string {key}: {record.get('id')}")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", record["source"]):
        raise ValueError("source must be a lowercase slug")
    if not record["id"].startswith(record["source"] + ":"):
        raise ValueError("id must start with source:")
    # Optional gallery identity is advisory: malformed values must not break an otherwise
    # usable library. The original JSONL retains the raw value; normalized queries show why
    # the stable, source-prefixed full reference ID is used instead.
    design_id = record.get("design_id")
    if design_id is not None and (type(design_id) not in {str, int} or not str(design_id).strip()):
        record["design_id"] = None
        record["design_id_warning"] = "Optional design_id unavailable; using the full reference ID"
    if record["kind"] not in {"flow", "screen", "page", "article", "pattern"}:
        raise ValueError("kind must be flow, screen, page, article, or pattern")
    if record["platform"] not in {"web", "ios", "android", "desktop", "cross-platform", "unknown"}:
        raise ValueError("Unsupported platform")
    datetime.fromisoformat(record["collected_at"].replace("Z", "+00:00"))
    url(record["source_url"])
    record.update(site_fields(record))
    for key in ("summary", "captured_label", "revision", "collection"):
        record.setdefault(key, "")
        if not isinstance(record[key], str):
            raise ValueError(f"{key} must be a string")
    for key in ("tags", "breadcrumb"):
        record.setdefault(key, [])
        if not isinstance(record[key], list) or any(not isinstance(v, str) or not v for v in record[key]):
            raise ValueError(f"{key} must be a list of non-empty strings")
    record.setdefault("screens", [])
    if not isinstance(record["screens"], list):
        raise ValueError("screens must be a list")
    for screen in record["screens"]:
        if not isinstance(screen, dict) or not screen.get("path"):
            raise ValueError("Every screen needs a local path; use screens: [] for URL-only records")
        local(root, screen["path"])
        if screen.get("source_url"):
            url(screen["source_url"])
        if screen.get("sha256") and not re.fullmatch(r"[0-9a-f]{64}", screen["sha256"]):
            raise ValueError("Invalid screen sha256")
    record.setdefault("documents", [])
    if not isinstance(record["documents"], list):
        raise ValueError("documents must be a list")
    for document in record["documents"]:
        if not isinstance(document, dict) or not document.get("path"):
            raise ValueError("Every document needs a local path")
        path = local(root, document["path"])
        if path.suffix.lower() not in {".md", ".txt", ".json"}:
            raise ValueError("Search documents must be Markdown, text, or JSON")
        checksum = document.get("sha256")
        if checksum and (not re.fullmatch(r"[0-9a-f]{64}", checksum) or hashlib.sha256(path.read_bytes()).hexdigest() != checksum):
            raise ValueError(f"Document hash mismatch: {document['path']}")
        path.read_text(encoding="utf-8")
    record.setdefault("notes", [])
    if not isinstance(record["notes"], list):
        raise ValueError("notes must be a list")
    for note in record["notes"]:
        if not isinstance(note, dict):
            raise ValueError("Each note must be an object")
        for key in ("observation", "interpretation", "scope", "origin", "caution", "project"):
            if key in note and not isinstance(note[key], str):
                raise ValueError(f"note.{key} must be a string")
        if not isinstance(note.get("evidence_paths", []), list) or not isinstance(note.get("observed_steps", []), list):
            raise ValueError("note.evidence_paths and note.observed_steps must be lists")
        for path in note.get("evidence_paths", []):
            local(root, path)
        for step in note.get("observed_steps", []):
            if type(step) is not int or not 1 <= step <= len(record["screens"]):
                raise ValueError("observed_steps must be valid one-based screen numbers")
    for key in ("local_viewer", "local_metadata"):
        if record.get(key):
            local(root, record[key].split("#", 1)[0])
    record["auto_tags"] = classify(record["title"], rules)
    record["context_tags"] = classify(" ".join(record["breadcrumb"][:-1]), rules)
    record["screen_count"] = len(record["screens"])
    record["tag_basis"] = "tags: supplied; auto_tags/context_tags: title/breadcrumb keyword rules, not visual verification"
    return record


def merge(existing, incoming):
    """Deduplicate the same source revision; preserve collection locations and notes."""
    def identity(record):
        return [record.get(k) for k in ("source", "kind", "product", "platform", "title", "source_url", "revision", "summary", "breadcrumb", "site_host", "site_page", "site_id", "page_type")] + [
            [(s.get("source_url"), s.get("sha256") or s["path"]) for s in record["screens"]],
            [d.get("sha256") or d["path"] for d in record.get("documents", [])]]
    if identity(existing) != identity(incoming):
        raise ValueError(f"Conflicting ID {incoming['id']}; give a changed source revision its own ID")
    # Prefer the first canonical record; later observations never silently replace it.
    for key in ("occurrences", "notes", "tags"):
        for value in incoming.get(key, []):
            if value not in existing[key]:
                existing[key].append(value)
    return existing


@contextmanager
def index_lock(root):
    (root/'catalog').mkdir(parents=True, exist_ok=True)
    with (root/'catalog/.reference-index.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def build(root):
    with index_lock(root):
        return _build(root)


def _build(root):
    started = time.perf_counter()
    from site_registry import ensure, attach
    site_registry = ensure(root)
    entries, signature, file_count = inputs(root)
    rules, curated, records = taxonomy(root), curation_notes(root), {}
    landings = {}
    duplicates = 0
    for format_name, path in entries:
        source_records = archive_records(root, path, curated) if format_name == "app-archive" else jsonl_records(path)
        for record in source_records:
            record = normalize(record, root, rules)
            record["occurrences"] = [{"input": path.relative_to(root).as_posix(),
                                      "collection": record["collection"],
                                      "collected_at": record["collected_at"],
                                      "local_metadata": record.get("local_metadata"),
                                      "local_viewer": record.get("local_viewer")}]
            if record["id"] in records:
                record = merge(records[record["id"]], record)
                duplicates += 1
            for key in landing_keys(record):
                for existing_id in landings.get(key, set()):
                    if existing_id != record["id"] and site_match(record, records[existing_id]):
                        raise ValueError(f"Duplicate landing/hero page: {record['id']} matches {existing_id} "
                                         f"at {key[0]}{key[2]} (version {key[3] or 'unspecified'}). "
                                         "Reuse the existing record ID for another observation, or provide "
                                         "the actual site_url path/revision for a genuinely different page/version.")
                landings.setdefault(key, set()).add(record["id"])
            records[record["id"]] = attach(record, site_registry)
    catalog = root / "catalog"
    catalog.mkdir(exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=".references-", suffix=".sqlite", dir=catalog)
    os.close(handle)
    try:
        with closing(sqlite3.connect(temp_name)) as connection, connection:
            connection.executescript("""
                CREATE TABLE refs (id TEXT PRIMARY KEY, source TEXT, kind TEXT, platform TEXT,
                    product TEXT, tags TEXT, document TEXT);
                CREATE VIRTUAL TABLE search_index USING fts5(title, product, breadcrumb, tags,
                    context_tags, notes, tokenize='unicode61');
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
                CREATE INDEX filters ON refs(source, platform, kind, product);
            """)
            for rowid, record in enumerate(records.values(), 1):
                tags = sorted(set(record["tags"] + record["auto_tags"]))
                connection.execute("INSERT INTO refs VALUES (?, ?, ?, ?, ?, ?, ?)",
                                   (record["id"], record["source"], record["kind"], record["platform"],
                                    record["product"], encode(tags), encode(record)))
                notes = " ".join(n.get("observation", "") + " " + n.get("interpretation", "") for n in record["notes"])
                document_text = "\n".join(local(root, d["path"]).read_text(encoding="utf-8") for d in record["documents"])
                connection.execute("INSERT INTO search_index(rowid,title,product,breadcrumb,tags,context_tags,notes) VALUES (?,?,?,?,?,?,?)",
                                   (rowid, record["title"], record["product"], " ".join(record["breadcrumb"]),
                                    " ".join(tags), " ".join(record["context_tags"]), record["summary"] + " " + notes + " " + document_text))
            stats = {
                "schema_version": VERSION, "built_at": datetime.now(timezone.utc).isoformat(),
                "records": len(records), "duplicate_observations": duplicates,
                "products_platforms": len({(r["product"], r["platform"]) for r in records.values()}),
                "sources": dict(Counter(r["source"] for r in records.values())),
                "platforms": dict(Counter(r["platform"] for r in records.values())),
                "kinds": dict(Counter(r["kind"] for r in records.values())),
                "screen_steps": sum(r["screen_count"] for r in records.values()),
                "unique_image_paths": len({s["path"] for r in records.values() for s in r["screens"]}),
                "records_with_notes": sum(bool(r["notes"]) for r in records.values()),
                "landing_hero_records": sum(r["page_type"] in LANDING_TYPES for r in records.values()),
                "identified_site_hosts": len({r["site_host"] for r in records.values() if r["site_host"]}),
                "text_documents": sum(len(r["documents"]) for r in records.values()),
                "input_files": file_count,
            }
            connection.execute("INSERT INTO metadata VALUES ('signature', ?)", (signature,))
            connection.execute("INSERT INTO metadata VALUES ('stats', ?)", (encode(stats),))
        if inputs(root)[1] != signature:
            raise ValueError("Inputs changed during build; rerun to obtain a consistent index")
        os.replace(temp_name, catalog / "references.sqlite")
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return dict(stats, elapsed_ms=round((time.perf_counter() - started) * 1000, 1))


def open_index(root):
    with index_lock(root):
        return _open_index(root)


def _open_index(root):
    target = root / "catalog/references.sqlite"
    signature = inputs(root)[1]
    if target.exists():
        with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as connection:
            stored = connection.execute("SELECT value FROM metadata WHERE key='signature'").fetchone()
            fresh = stored and stored[0] == signature
    else:
        fresh = False
    if not fresh:
        _build(root)
    connection = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection, not fresh


def check_site(root, args):
    """Read current source metadata before acquisition, without creating/rebuilding an index."""
    candidate = {key: getattr(args, key, "") or "" for key in
                 ("site_url", "site_id", "product", "source", "revision", "captured_label", "platform", "page_type")}
    if not (candidate["site_url"] or candidate["site_id"] or (candidate["product"] and candidate["source"])):
        raise ValueError("Provide --site-url, --site-id with --source, or both --product and --source")
    if candidate["site_id"] and not candidate["source"]:
        raise ValueError("--site-id requires --source")
    candidate.update(site_fields(candidate))

    products, landings = {}, {}
    rules = taxonomy(root)
    for format_name, path in inputs(root)[0]:
        if format_name == "app-archive":
            # Product inventory is enough: avoid loading all screenshot/flow mappings for a preflight.
            for app in read(path)["apps"]:
                item = {"source": "app-archive", "product": app["name"], "platform": app["platform"]}
                basis = site_match(candidate, item)
                if not basis:
                    continue
                metadata = read(path.parent / app["archiveDir"] / "app.json")
                key = (item["source"], item["product"], item["platform"], metadata["version"])
                products[key] = dict(item, revision=metadata["version"],
                                     flow_count=metadata.get("flowCount", app.get("treeNodeCount")),
                                     match_basis=basis, source_url=metadata.get("sourceUrl", app.get("url")),
                                     collection=path.parent.relative_to(root).as_posix())
        else:
            for raw in jsonl_records(path):
                record = normalize(raw, root, rules)
                basis = site_match(candidate, record)
                if not basis:
                    continue
                if record["page_type"] in LANDING_TYPES:
                    item = {key: record[key] for key in ("id", "product", "source", "platform", "title", "site_url",
                            "site_id", "site_host", "site_page", "page_type", "revision", "captured_label", "source_url")}
                    item["match_basis"] = basis
                    item["input"] = path.relative_to(root).as_posix()
                    landings.setdefault(record["id"], item)
                else:
                    key = (record["source"], record["product"], record["platform"], record["revision"])
                    products.setdefault(key, {key: record[key] for key in
                                              ("source", "product", "platform", "revision", "source_url")})
                    products[key]["match_basis"] = basis
    seen = [value for value in getattr(args, "seen_site_url", [])
            if candidate["site_host"] and site_location(value)[0] == candidate["site_host"]]
    version = candidate["revision"] or candidate["captured_label"]
    duplicate_ids = [item["id"] for item in landings.values()
                     if item["site_page"] == candidate["site_page"] and
                     item["platform"] == candidate["platform"] and
                     (item["revision"] or item["captured_label"]) == version]
    return {"candidate": candidate, "skip_site": bool(landings or seen),
            "duplicate_page": bool(duplicate_ids), "duplicate_record_ids": duplicate_ids,
            "reason": "already_collected_landing" if landings else "seen_in_current_selection" if seen else
                      "existing_product_only" if products else "new_site",
            "existing_products": list(products.values()), "existing_landings": list(landings.values()),
            "seen_site_urls": seen,
            "identity_note": "Host/site_id matches are explicit; same_source_product is an exact name match within one source. "
                             "App flows do not block a first landing acquisition. skip_site excludes already selected sites "
                             "across ranking cohorts; distinct page paths and versions remain valid imports.",
            "read_only": True, "index_rebuilt": False}


def query_groups(query, rules):
    stopwords = {"ui", "ux", "레퍼런스", "참고", "사례", "화면", "플로우", "찾아줘", "찾기", "reference", "references", "flow", "flows", "screen", "screens", "and", "the", "a", "an", "of", "for"}
    groups = []
    query = query.casefold()
    for rule in rules:
        for alias in rule["aliases"]:
            if " " in alias:
                query = re.sub(r"(?<!\w)" + re.escape(alias.casefold()) + r"(?!\w)", rule["id"], query)
    for token in re.findall(r"[^\W_]+(?:-[^\W_]+)*", query):
        if token in stopwords:
            continue
        alternatives = [token]
        for rule in rules:
            for alias in [rule["id"], *rule["aliases"]]:
                # A small bilingual glossary, not semantic search or Korean morphology.
                # Add aliases here; consider embeddings only after measuring missed queries.
                if token == alias.casefold() or (re.search("[가-힣]", alias) and
                   token in {alias + suffix for suffix in ("을", "를", "의", "에", "에서", "이", "가")}):
                    alternatives.append(rule["id"])
                    break
        group = "(" + " OR ".join('"' + term.replace('"', '""') + '"' +
                                  ("*" if re.search("[가-힣]", term) else "")
                                  for term in dict.fromkeys(alternatives)) + ")"
        if group not in groups:
            groups.append(group)
    return groups


def search_parts(root, args):
    groups = query_groups(args.query, taxonomy(root))
    where, params = [], []
    for key in ("source", "platform", "kind", "product"):
        if getattr(args, key, None):
            where.append(f"r.{key} = ? COLLATE NOCASE")
            params.append(getattr(args, key))
    if getattr(args, 'entity', None):
        from site_registry import find
        entity = find(root, args.entity)
        if 'id' not in entity:
            raise ValueError('Entity not found or ambiguous; use site <name> to select an exact ID')
        where.append("json_extract(r.document, '$.site_entity.entity_id') = ?")
        params.append(entity['id'])
    for tag in args.tag:
        where.append("EXISTS (SELECT 1 FROM json_each(r.tags) WHERE value = ?)")
        params.append(tag)
    if args.query.strip() and not groups:
        raise ValueError("Use specific concepts, e.g. 초대 권한, subscription cancellation, or --tag invitation")
    if groups:
        where.insert(0, "search_index MATCH ?")
        params.insert(0, (" OR " if args.any else " AND ").join(groups))
        table = "refs r JOIN search_index ON search_index.rowid=r.rowid"
        ranking = "bm25(search_index, 8, 5, 1, 6, 0.5, 1)"
        excerpt = "snippet(search_index, -1, '[', ']', ' … ', 24)"
    else:
        table, ranking = "refs r", "0.0"
        excerpt = "''"
    predicate = " WHERE " + " AND ".join(where) if where else ""
    return table, predicate, params, ranking, excerpt, groups


def search(connection, root, args):
    table, predicate, params, ranking, excerpt, groups = search_parts(root, args)
    sort = getattr(args, 'sort', 'relevance')
    orders = {'relevance': 'rank, r.id',
              'collected_desc': "julianday(json_extract(r.document, '$.collected_at')) DESC, r.id",
              'collected_asc': "julianday(json_extract(r.document, '$.collected_at')) ASC, r.id",
              'visual_recent': "CASE WHEN json_extract(r.document, '$.screen_count') > 0 THEN 0 ELSE 1 END, julianday(json_extract(r.document, '$.collected_at')) DESC, r.id"}
    if sort not in orders:
        raise ValueError('Unsupported reference sort')
    total = connection.execute(f"SELECT count(*) FROM {table}{predicate}", params).fetchone()[0]
    rows = connection.execute(f"SELECT r.document, {ranking} AS rank, {excerpt} AS excerpt FROM {table}{predicate} ORDER BY {orders[sort]} LIMIT ? OFFSET ?",
                              [*params, args.limit, args.offset]).fetchall()
    results = []
    for row in rows:
        record = json.loads(row["document"])
        result = {k: record[k] for k in ("id", "source", "kind", "product", "platform", "title", "breadcrumb", "tags", "auto_tags", "context_tags", "screen_count", "collected_at", "captured_label", "source_url", "site_url", "site_id", "page_type")}
        result["local_viewer"] = str(root / record["local_viewer"]) if record.get("local_viewer") else None
        result["notes_available"] = len(record["notes"])
        result["documents_available"] = len(record.get("documents", []))
        if record.get('site_entity'):
            result['site_entity'] = {k: record['site_entity'][k] for k in ('entity_id', 'name', 'relation', 'design_document_ids')}
            result['site_entity']['entity_type']=record['site_entity'].get('entity_type','site')
        for field in ('design_id','author','author_url','source_metrics','palette','related_reference_ids','site_identity_status'):
            if field in record:result[field]=record[field]
        result["matched_excerpt"] = row["excerpt"]
        results.append(result)
    return {"query": args.query, "match": "any" if args.any else "all", "expanded_query": params[0] if groups else None,
            "total": total, "returned": len(results), "offset": args.offset,
            "match_basis": "Keyword matches in title/product/breadcrumb/tags/linked notes/documents; relevance is not a visual quality rating. --tag filters exclude ancestor tags.",
            "results": results}


def show(connection, root, args):
    rows = connection.execute("SELECT document FROM refs WHERE id=?", (args.id,)).fetchall()
    if not rows:
        rows = connection.execute("SELECT document FROM refs WHERE substr(id, -length(?))=? LIMIT 2", (":" + args.id, ":" + args.id)).fetchall()
    if len(rows) != 1:
        raise ValueError("Reference not found or ID is ambiguous; use the full id returned by search")
    record = json.loads(rows[0]["document"])
    screens = record.pop("screens")
    indices = [n - 1 for n in args.step] if args.step else list(range(args.offset, min(len(screens), args.offset + args.limit)))
    if any(i < 0 or i >= len(screens) for i in indices):
        raise ValueError("Requested step is outside this reference")
    record["screens"] = [dict(screens[i], step=i + 1, path=str(root / screens[i]["path"])) for i in indices]
    record["returned_screens"] = len(indices)
    record["library_root"] = str(root)
    record["documents"] = [dict(document, path=str(root / document["path"])) for document in record.get("documents", [])]
    for key in ("local_viewer", "local_metadata"):
        if record.get(key):
            record[key] = str(root / record[key])
    if record.get('site_entity'):
        for key in ('local_viewer', 'analysis_path', 'rule_viewer'):
            if record['site_entity'].get(key):
                record['site_entity'][key] = str(root / record['site_entity'][key])
    relation_path=(root/record.get('collection','')/'relations.json').resolve()
    if relation_path.is_relative_to(root.resolve()) and relation_path.is_file() and relation_path.stat().st_size<2_000_000:
        record['cross_source_searches']=[r for r in json.loads(relation_path.read_text(encoding="utf-8"))
                                       if record['id']==r.get('primary_id') or record['id'] in r.get('related_ids',[])]
    record["interpretation_rule"] = "Inspect selected local images before making visual claims. Recorded steps are not click counts; source hierarchy is not a verified transition graph."
    return record


def ingest(root, path):
    # Publication, index replacement and rollback are one writer transaction.
    # UI reads recheck freshness under this same lock before rebuilding.
    with index_lock(root):
        return _ingest(root, path)


def _ingest(root, path):
    records = [normalize(record, root, taxonomy(root)) for record in jsonl_records(path)]
    sources = {record["source"] for record in records}
    if len(sources) != 1:
        raise ValueError("An import must contain at least one record, all from one source")
    raw = path.read_bytes()
    target = root / "imports" / sources.pop() / (hashlib.sha256(raw).hexdigest()[:24] + ".jsonl")
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    if existed and target.read_bytes() != raw:
        raise ValueError("Import filename collision")
    if not existed:
        # Publish a complete batch atomically without replacing an existing one.
        # Registry readers must never see a half-written JSONL file.
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.import-', delete=False) as file:
            temporary = Path(file.name)
            file.write(raw); file.flush(); os.fsync(file.fileno())
        try: os.link(temporary, target)
        finally: temporary.unlink(missing_ok=True)
    try:
        stats = _build(root)
    except Exception:
        if not existed:
            target.unlink(missing_ok=True)
        raise
    return {"imported_file": target.relative_to(root).as_posix(), "already_present": existed, "index": stats}


def initialize(root):
    """Create only missing configuration; never overwrite source material."""
    defaults = {
        "catalog/sources.json": {"schema_version": VERSION, "inputs": [
            {"format": "jsonl", "glob": "imports/**/*.jsonl"},
            {"format": "app-archive", "glob": "archives/*/selection.json"}]},
        "catalog/taxonomy.json": {"tags": []},
        "catalog/site-aliases.json": {"aliases": [], "domain_aliases": [],
                                      "canonical_names": {}, "search_aliases": {}, "related_products": []},
    }
    created = []
    for name, value in defaults.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            created.append(name)
        except FileExistsError:
            pass
    graph = root / "knowledge/design-rules"
    if not (graph / "rules.sqlite").exists() or not (graph / "catalog.json").exists():
        from design_graph import build_graph
        build_graph(root)
    stats = build(root)
    return {"library_root": str(root), "created": created, "index": stats,
            "note": "No reference data is bundled. Import your own JSONL records or add DESIGN.md manifests; see references/ documentation."}


def bounded_int(value):
    number = int(value)
    if not 1 <= number <= 50:
        raise argparse.ArgumentTypeError("Expected a number from 1 to 50")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="User-owned data library, outside this skill")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create an empty library without overwriting existing data")
    commands.add_parser("build", help="Rebuild atomically from local source records")
    commands.add_parser("stats", help="Show index coverage and freshness")
    commands.add_parser("tags", help="List bilingual query vocabulary")
    site = commands.add_parser('site', help='Find matching DESIGN.md documents, UX cases, and evidence for one site')
    site.add_argument('query')
    importer = commands.add_parser("import", help="Validate and ingest one local JSONL batch")
    importer.add_argument("file", type=Path)
    checker = commands.add_parser("check-site", help="Read-only site/landing acquisition preflight against current metadata")
    for key in ("site-url", "site-id", "product", "source", "revision", "captured-label"):
        checker.add_argument("--" + key, default="")
    checker.add_argument("--platform", default="web")
    checker.add_argument("--page-type", default="landing")
    checker.add_argument("--seen-site-url", action="append", default=[],
                         help="Repeat for sites already selected in another ranking cohort")
    finder = commands.add_parser("search", help="Compact keyword search; all concepts match by default")
    finder.add_argument("query", nargs="?", default="")
    for key in ("source", "platform", "kind", "product"):
        finder.add_argument("--" + key)
    finder.add_argument('--entity', help='Canonical site ID or unambiguous name; spans source aliases and platforms')
    finder.add_argument("--tag", action="append", default=[])
    finder.add_argument("--any", action="store_true", help="Explicitly broaden to any query concept")
    finder.add_argument("--limit", type=bounded_int, default=8)
    finder.add_argument("--offset", type=int, default=0)
    detail = commands.add_parser("show", help="Retrieve one record and selected image paths")
    detail.add_argument("id")
    detail.add_argument("--step", type=int, action="append", default=[])
    detail.add_argument("--offset", type=int, default=0)
    detail.add_argument("--limit", type=bounded_int, default=3)
    args = parser.parse_args()
    started = time.perf_counter()
    try:
        root = library_root(args.root)
        if args.command != "init":
            require_library(root)
        if getattr(args, "offset", 0) < 0:
            raise ValueError("offset must be non-negative")
        if args.command == "init":
            result = initialize(root)
        elif args.command == "build":
            result = build(root)
        elif args.command == "tags":
            result = {"tags": taxonomy(root)}
        elif args.command == "import":
            result = ingest(root, args.file)
        elif args.command == "check-site":
            result = check_site(root, args)
        elif args.command == 'site':
            from site_registry import ensure, find
            if ensure(root) is None:
                raise ValueError('Run design_graph.py --root <data-directory> build to create the graph, even for an empty corpus')
            result = find(root, args.query)
        else:
            connection, rebuilt = open_index(root)
            with closing(connection):
                if args.command == "stats":
                    result = json.loads(connection.execute("SELECT value FROM metadata WHERE key='stats'").fetchone()[0])
                elif args.command == "search":
                    result = search(connection, root, args)
                else:
                    result = show(connection, root, args)
            result["index_rebuilt"] = rebuilt
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error, ImportError) as error:
        print(encode({"error": str(error)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
