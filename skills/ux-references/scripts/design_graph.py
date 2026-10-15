#!/usr/bin/env python3
"""Source-backed design-rule graph. Query commands use only Python's standard library."""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from urllib.parse import urlsplit

from library import library_root, local_file, require_library
CATEGORIES = {
    "color": ("색상·색 조합", "색 색상 컬러 팔레트 배경 강조색 대비 color palette contrast"),
    "typography": ("폰트·크기 비율", "폰트 글꼴 글자 타이포그래피 제목 본문 크기 비율 font typography size ratio"),
    "spacing": ("간격·여백", "간격 여백 패딩 마진 밀도 spacing padding margin rhythm density"),
    "layout": ("배치·그리드", "배치 레이아웃 정렬 그리드 콘텐츠 폭 반응형 layout grid alignment responsive width"),
    "components": ("컴포넌트", "컴포넌트 버튼 카드 입력 폼 component button card input form"),
    "shape": ("형태·모서리", "모서리 라운드 반경 형태 radius rounded shape"),
    "elevation": ("그림자·레이어", "그림자 깊이 레이어 shadow elevation layer"),
    "motion": ("모션·상호작용", "모션 애니메이션 전환 interaction animation motion transition"),
    "principles": ("원칙·판단 기준", "원칙 분위기 철학 의도 판단 기준 principle philosophy mood rationale"),
    "accessibility": ("접근성", "접근성 포커스 키보드 accessibility focus keyboard"),
}
TOKEN_ROOTS = {"colors": "color", "color": "color", "typography": "typography", "fonts": "typography",
               "spacing": "spacing", "layout": "layout", "grid": "layout", "components": "components",
               "rounded": "shape", "radius": "shape", "radii": "shape", "shadows": "elevation",
               "shadow": "elevation", "elevation": "elevation", "motion": "motion", "animation": "motion",
               "transitions": "motion", "accessibility": "accessibility"}
LEAF_ALIASES = {"fontSize": "size", "fontWeight": "weight", "fontFamily": "family",
                "lineHeight": "line-height", "letterSpacing": "letter-spacing",
                "backgroundColor": "bg", "textColor": "fg", "borderRadius": "radius"}
QUERY_PHRASES = {"폰트 사이즈 비율": "type-ratio", "폰트 크기 비율": "type-ratio", "크기 비율": "type-ratio",
                 "색상 조합": "color-pair", "색 조합": "color-pair", "폰트 조합": "font-pair", "글꼴 조합": "font-pair", "글꼴 목록": "font-set"}
QUERY_ALIASES = {"폰트": "typography", "타이포": "typography", "간격": "spacing", "여백": "spacing",
                 "배치": "layout", "레이아웃": "layout", "색상": "color", "색": "color", "대비": "contrast",
                 "버튼": "button", "카드": "card", "모서리": "radius", "미니멀": "minimal",
                 "금융": "fintech", "개발자": "developer", "생산성": "productivity", "교육": "education",
                 "패딩": "padding", "마진": "margin", "사이즈": "size", "한국": "korea"}


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def key(value):
    return re.sub(r"[^a-z0-9가-힣]+", "", value.casefold())


def canonical(path):
    parts = path.removeprefix("tokens.").split(".")
    parts[0] = {"color": "colors", "fonts": "typography"}.get(parts[0], parts[0])
    return ".".join(LEAF_ALIASES.get(part, part) for part in parts)


def frontmatter(markdown):
    """Keep source unchanged; recover valid token blocks if an unrelated description has invalid YAML."""
    import yaml
    lines = markdown.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, [], 0, []
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        return {}, [], 0, ["Unclosed YAML frontmatter; prose-only extraction"]
    body = "\n".join(lines[1:end])
    warnings = []
    blocks = []
    try:
        data = yaml.safe_load(body) or {}
        node = yaml.compose(body, Loader=yaml.SafeLoader)
        if not isinstance(data, dict) or not isinstance(node, yaml.MappingNode):
            return {}, [], end + 1, ["Frontmatter is not a mapping"]
        blocks = [(name.value, value, 2) for name, value in node.value]
    except yaml.YAMLError as error:
        warnings.append("Invalid complete YAML; isolated top-level blocks parsed: " + str(error).splitlines()[0])
        data = {}
        starts = [i for i, line in enumerate(lines[1:end]) if re.match(r"^[A-Za-z_][\w.-]*:", line)]
        for position, start in enumerate(starts):
            stop = starts[position + 1] if position + 1 < len(starts) else end - 1
            fragment = "\n".join(lines[start + 1:stop + 1])
            try:
                parsed = yaml.safe_load(fragment)
                node = yaml.compose(fragment, Loader=yaml.SafeLoader)
                if isinstance(parsed, dict) and isinstance(node, yaml.MappingNode):
                    data.update(parsed)
                    blocks.extend((name.value, value, start + 2) for name, value in node.value)
            except yaml.YAMLError:
                continue
    return data, blocks, end + 1, warnings


def scalar(node):
    import yaml
    if node.tag.endswith(":null"):
        return None
    if node.tag.endswith(":bool"):
        return node.value.casefold() in {"yes", "true", "on"}
    if node.tag.endswith((":int", ":float")):
        try:
            number = Decimal(node.value.replace("_", ""))
            if number.is_finite():
                return int(number) if number == int(number) else float(number)
        except Exception:
            pass
    if not isinstance(node, yaml.ScalarNode):
        raise ValueError("Expected scalar")
    return node.value


def leaves(node, prefix, offset, ancestors=()):
    import yaml
    if id(node) in ancestors:
        return
    if isinstance(node, yaml.MappingNode):
        for name, child in {name.value: (name, child) for name, child in node.value}.values():
            yield from leaves(child, prefix + "." + name.value, offset, (*ancestors, id(node)))
    elif isinstance(node, yaml.SequenceNode):
        if all(isinstance(child, yaml.ScalarNode) for child in node.value):
            yield prefix, [scalar(child) for child in node.value], node.start_mark.line + offset, node.end_mark.line + offset
        else:
            for i, child in enumerate(node.value):
                yield from leaves(child, prefix + "." + str(i), offset, (*ancestors, id(node)))
    elif isinstance(node, yaml.ScalarNode):
        yield prefix, scalar(node), node.start_mark.line + offset, node.end_mark.line + offset


def heading_category(heading):
    for category, pattern in [
        ("accessibility", r"accessib|접근성"), ("typography", r"typograph|font|type scale|타이포|폰트"),
        ("color", r"color|palette|색상"), ("spacing", r"spacing|rhythm|여백|간격"),
        ("layout", r"layout|grid|responsive|structure|배치|레이아웃"),
        ("shape", r"radius|rounded|corner|shape"), ("elevation", r"shadow|elevation|depth"),
        ("motion", r"motion|animation|transition|interaction"),
        ("components", r"component|button|card|input|navigation"),
        ("principles", r"philosoph|principle|atmosphere|theme|mood|personality|direction|brand")]:
        if re.search(pattern, heading, re.I):
            return category
    return None


def extract(document, markdown, blocks, body_start, detail):
    lines = markdown.splitlines()
    ast = detail.get("referenceAst", {})
    evidence = ast.get("evidence") or {}
    claims = {c["claimPath"]: c for c in evidence.get("claims", [])}
    surfaces = {s["id"]: s for s in evidence.get("surfaces", [])}
    rules = []

    def add(path, value, category, start, end, kind="token", scope="document-unspecified"):
        if value is None or value == "" or len(encode(value)) > 5000:
            return
        start, end = max(1, start), min(len(lines), max(start, end))
        claim = claims.get(path, {})
        surface = surfaces.get(claim.get("surfaceId"), {})
        rule = {"id": "rule:" + digest(document["id"] + "\0" + path)[:24],
                "document_id": document["id"], "brand_id": document["brand_id"],
                "source": document["source"], "category": category, "kind": kind,
                "property": canonical(path) if kind == "token" else path, "source_property": path,
                "value": value, "context": {"scope": scope, "surface_id": claim.get("surfaceId"),
                "surface_kind": surface.get("kind"), "surface_url": surface.get("url"),
                "captured_at": claim.get("capturedAt")},
                "quality_declared": document["quality_declared"],
                "evidence": {"path": document["path"], "start_line": start, "end_line": end,
                             "source_url": document["source_url"], "sha256": document["sha256"],
                             "upstream_claim": claim or None},
                "extraction": "source-token" if kind == "token" else "source-text-excerpt"}
        rules.append(rule)

    for name, node, offset in blocks:
        prefix = name
        candidates = list(leaves(node, prefix, offset))
        for path, value, start, end in candidates:
            parts = path.removeprefix("tokens.").split(".")
            category = TOKEN_ROOTS.get(parts[0])
            if not category:
                continue
            scope = ".".join(parts[:2]) if parts[0] == "components" and len(parts) > 1 else "document-unspecified"
            add(path, value, category, start, end, scope=scope)

    headings = []
    paragraph = []
    fenced = False

    def flush():
        if not paragraph:
            return
        section = " / ".join(name for _, name in headings)
        category = heading_category(section)
        statement = "\n".join(text for _, text in paragraph).strip()
        if category and 20 <= len(statement) <= 3500 and not re.fullmatch(r"[\s|:-]+", statement):
            add(f"prose.{category}.{paragraph[0][0]}", statement, category,
                paragraph[0][0], paragraph[-1][0], "prose", section)
        paragraph.clear()

    for i, line in enumerate(lines[body_start:], body_start + 1):
        if re.match(r"^\s*(```|~~~)", line):
            flush(); fenced = not fenced; continue
        if fenced:
            continue
        match = re.match(r"^(#{1,6})\s+(.+)", line)
        if match:
            flush()
            level, label = len(match[1]), match[2]
            headings = [(n, name) for n, name in headings if n < level] + [(level, label)]
        elif not line.strip():
            flush()
        elif re.match(r"^\s*(?:[-*+] |\d+\. |\|)", line):
            flush(); paragraph.append((i, line)); flush()
        else:
            paragraph.append((i, line))
    flush()
    return rules


def numeric_size(value, allow_zero=False):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and (value > 0 or (allow_zero and value == 0)):
        return float(value), "unspecified"
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(px|rem|em|pt)\s*", value)
        if match and (float(match[1]) > 0 or (allow_zero and float(match[1]) == 0)):
            return float(match[1]), match[2]
    return None


def rgb(value):
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?", value):
        return None
    value = value[1:]
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]


def contrast(first, second):
    def luminance(color):
        channels = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in color]
        return sum(a * b for a, b in zip(channels, (0.2126, 0.7152, 0.0722)))
    a, b = sorted((luminance(first), luminance(second)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def derive(document, rules):
    """Derive only inspectable relationships; never turn arithmetic into a universal design rule."""
    by_property = {r["property"]: r for r in rules if r["kind"] == "token"}
    edges, derived = [], []
    for rule in list(by_property.values()):
        if isinstance(rule["value"], str):
            for token in re.findall(r"\{([\w.-]+)\}", rule["value"]):
                target = by_property.get(canonical(token))
                if target:
                    edges.append({"from": rule["id"], "relation": "references_token", "to": target["id"]})
                else:
                    rule.setdefault("unresolved_references", []).append(token)

    def resolve(rule, seen=()):
        if rule["id"] in seen:
            return None
        value = rule["value"]
        match = re.fullmatch(r"\{([\w.-]+)\}", value) if isinstance(value, str) else None
        if not match:
            return value, [rule["id"]]
        target = by_property.get(canonical(match[1]))
        result = resolve(target, (*seen, rule["id"])) if target else None
        return (result[0], [rule["id"], *result[1]]) if result else None

    def add(name, category, value, inputs, note, scope):
        identity = "derived:" + digest(document["id"] + "\0" + name)[:24]
        item = {"id": identity, "document_id": document["id"], "brand_id": document["brand_id"],
                "source": document["source"], "category": category, "kind": "derived", "property": name,
                "value": value, "context": {"scope": scope}, "quality_declared": document["quality_declared"],
                "inputs": list(dict.fromkeys(inputs)), "derivation_note": note,
                "extraction": "deterministic-derivation-not-source-assertion"}
        derived.append(item)
        edges.extend({"from": identity, "relation": "derived_from", "to": item_id} for item_id in item["inputs"])

    # Pair colors only inside an explicitly named component, or with compatible source surface context.
    pairs = []
    for prefix in sorted({r["property"].rsplit(".", 1)[0] for r in rules if r["property"].startswith("components.")}):
        if prefix + ".bg" in by_property and prefix + ".fg" in by_property:
            pairs.append((prefix, by_property[prefix + ".bg"], by_property[prefix + ".fg"]))
    for bg_name, fg_name in [("canvas", "foreground"), ("canvas", "ink"), ("primary", "on-primary"), ("canvas", "body")]:
        bg, fg = by_property.get("colors." + bg_name), by_property.get("colors." + fg_name)
        if bg and fg:
            sa, sb = bg["context"].get("surface_id"), fg["context"].get("surface_id")
            if not sa or not sb or sa == sb:
                pairs.append(("colors." + bg_name + "+" + fg_name, bg, fg))
    for scope, background, foreground in pairs:
        bg, fg = resolve(background), resolve(foreground)
        if bg and fg and rgb(bg[0]) and rgb(fg[0]):
            add("color-pair." + scope, "color", {"background": bg[0], "foreground": fg[0],
                "contrast_ratio": contrast(rgb(bg[0]), rgb(fg[0]))}, bg[1] + fg[1],
                "Opaque sRGB arithmetic from source values. Context and text size still require review; no accessibility pass is asserted.", scope)

    sizes = {r["property"]: r for r in rules if r["property"].startswith("typography.") and r["property"].endswith(".size")}
    body = next((sizes[name] for name in ("typography.body.size", "typography.body-md.size", "typography.body-base.size", "typography.base.size") if name in sizes), None)
    if body and numeric_size(body["value"]):
        base, unit = numeric_size(body["value"])
        for path, rule in sizes.items():
            parsed = numeric_size(rule["value"])
            if path == body["property"] or not parsed or parsed[1] != unit:
                continue
            first, second = rule["context"].get("surface_id"), body["context"].get("surface_id")
            if first and second and first != second:
                continue
            add("type-ratio." + path, "typography", {"text_role": path, "size": rule["value"],
                "body_role": body["property"], "body_size": body["value"], "ratio": parsed[0] / base, "unit": unit},
                [rule["id"], body["id"]], "Ratio of matching explicit units or two untyped numeric values. Verify untyped units in context; responsive/cross-unit conversion is not guessed.", "source-type-scale")

    families = [r for r in rules if r["kind"] == "token" and r["property"].startswith("typography.") and
                (r["property"].endswith(".family") or r["property"].startswith("typography.family."))]
    if families:
        add("font-set", "typography", {r["property"]: r["value"] for r in families}, [r["id"] for r in families],
            "Font roles/fallbacks as named by the source. A sans/mono inventory is not automatically a heading/body pairing.", "source-font-roles")
    heading_font = next((by_property[p] for p in ("typography.family.heading", "typography.family.display", "typography.h1.family", "typography.display-xl.family", "typography.display-lg.family", "typography.heading.family", "typography.title.family") if p in by_property), None)
    body_font = next((by_property[p] for p in ("typography.family.body", "typography.body.family", "typography.body-md.family", "typography.body-base.family") if p in by_property), None)
    if heading_font and body_font:
        first, second = heading_font["context"].get("surface_id"), body_font["context"].get("surface_id")
        heading_value, body_value = resolve(heading_font), resolve(body_font)
        if heading_value and body_value and not (first and second and first != second):
            value = {"heading_role": heading_font["property"], "heading_family": heading_value[0],
                     "body_role": body_font["property"], "body_family": body_value[0]}
            inputs = heading_value[1] + body_value[1]
            for label, font in (("heading", heading_font), ("body", body_font)):
                weight = by_property.get(font["property"].rsplit(".", 1)[0] + ".weight")
                if weight:
                    value[label + "_weight"] = weight["value"]; inputs.append(weight["id"])
            add("font-pair.heading+body", "typography", value, inputs,
                "Pairing only where the source explicitly names heading/display and body roles; no roles inferred from a generic sans/mono list.", "explicit-font-roles")
    spacing = [r for r in rules if r["kind"] == "token" and r["property"].startswith("spacing.") and numeric_size(r["value"], True)]
    by_unit = defaultdict(list)
    for rule in spacing:
        by_unit[numeric_size(rule["value"], True)[1]].append(rule)
    for unit, items in by_unit.items():
        ordered = sorted(items, key=lambda r: numeric_size(r["value"], True)[0])
        add("spacing-scale." + unit, "spacing", {"unit": unit, "steps": [{"role": r["property"], "value": r["value"]} for r in ordered]},
            [r["id"] for r in ordered], "Observed source scale, not an inferred mandatory grid or base unit.", "source-spacing-scale")
    return derived, edges


def load_documents(root):
    documents, originals, brand_names, host_keys = [], {}, {}, {}
    manifests = []
    # A source may be any user-supplied provider; no private collection is required.
    sources = sorted({p.parent.parent for p in (root / "sources").glob("*/*/manifest.json")})
    brand_homepages = {}
    for source_dir in sources:
        source = source_dir.name
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", source):
            raise ValueError(f"Source directory must be a lowercase slug: {source}")
        available = sorted(source_dir.glob("*/manifest.json"))
        manifest_path = local_file(root, available[-1].relative_to(root).as_posix())
        manifest = read(manifest_path)
        if manifest.get("complete") is not True:
            raise ValueError(f"Incomplete source snapshot: {manifest_path}")
        datetime.fromisoformat(manifest["collected_at"].replace("Z", "+00:00"))
        if manifest.get("license_path"):
            local_file(root, manifest["license_path"])
        manifests.append(manifest_path.relative_to(root).as_posix())
        for entry in manifest["documents"]:
            if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", entry["slug"]):
                raise ValueError("Document slug must be lowercase words separated by hyphens or dots")
            source_url = entry["source_url"]
            if urlsplit(source_url).scheme not in {"http", "https"} or not urlsplit(source_url).hostname:
                raise ValueError("Document source_url must be an HTTP(S) URL")
            path = local_file(root, entry["path"])
            markdown = path.read_bytes().decode("utf-8")
            if digest(markdown) != entry["sha256"]:
                raise ValueError(f"Source hash changed: {path}")
            metadata, blocks, body_start, warnings = frontmatter(markdown)
            detail = read(local_file(root, entry["metadata_path"])) if entry.get("metadata_path") else {}
            catalog = read(local_file(root, (path.parent / "catalog-entry.json").relative_to(root).as_posix())) if (path.parent / "catalog-entry.json").exists() else {}
            heading = next((line.removeprefix("# ") for line in markdown.splitlines() if line.startswith("# ")), entry["slug"])
            name = catalog.get("name") or metadata.get("name") or re.sub(r"^Design System (?:Inspired by|for)\s+", "", heading, flags=re.I)
            name = re.sub(r"[- ]design[- ]analysis$", "", str(name), flags=re.I)
            homepage = str(metadata.get("homepage") or "")
            homepage_host = (urlsplit(homepage).hostname or "").casefold().removeprefix("www.")
            name_matches = brand_names.get(key(name), set())
            host_matches = host_keys.get(homepage_host, set()) if homepage_host else set()
            match_basis = "source-brand-id"
            if len(host_matches) == 1:
                brand_id = next(iter(host_matches))
                match_basis = "exact-homepage-host"
            elif len(name_matches) == 1 and not (homepage_host and
                    brand_homepages[next(iter(name_matches))] not in {"", homepage_host}):
                brand_id = next(iter(name_matches))
                match_basis = "normalized-display-name"
            else:
                brand_id = "brand:" + entry["slug"]
                if brand_id in brand_homepages:
                    brand_id = "brand:" + source + "-" + entry["slug"]
                if brand_id in brand_homepages:
                    raise ValueError("Conflicting document identity: " + brand_id)
            brand_homepages.setdefault(brand_id, homepage_host)
            brand_names.setdefault(key(name), set()).add(brand_id)
            if homepage_host:
                host_keys.setdefault(homepage_host, set()).add(brand_id)
            commit = (manifest.get("upstream_commit") or {}).get("sha", "")
            revision = digest(markdown + (encode(detail.get("referenceAst", {})) if detail else commit))[:20]
            body = "\n".join(markdown.splitlines()[body_start:])
            description = str(detail.get("mood") or metadata.get("description") or "")
            quality = entry.get("quality_declared", "ungraded")
            document = {
                "id": f"{source}:{entry['slug']}:{revision}", "source": source, "source_slug": entry["slug"],
                "brand_id": brand_id, "brand": name, "homepage": homepage, "country": catalog.get("country") or metadata.get("country", ""),
                "aliases": [metadata['display_name_kr']] if isinstance(metadata.get('display_name_kr'), str) else [],
                "brand_match_basis": match_basis,
                "industry": catalog.get("category") or metadata.get("category", ""), "description": description,
                "path": entry["path"], "source_url": source_url, "catalog_url": manifest.get("catalog_url", ""),
                "metadata_path": entry.get("metadata_path"), "sha256": entry["sha256"],
                "content_family": "text:" + digest(re.sub(r"\s+", " ", body).strip())[:24],
                "revision": revision, "collected_at": manifest["collected_at"],
                "captured_label": entry.get("captured_label", ""), "quality_declared": quality,
                "quality_note": "Provider-assigned status; not an independent verification by this archive.",
                "license_path": manifest.get("license_path", ""), "parser_warnings": warnings,
                "token_source_note": (metadata.get("tokens") or {}).get("note", "") if isinstance(metadata.get("tokens"), dict) else "",
            }
            documents.append(document)
            originals[document["id"]] = markdown, blocks, body_start, detail
    return documents, originals, manifests


def variations(rules):
    groups = defaultdict(list)
    for rule in rules:
        if rule["kind"] == "token":
            groups[(rule["brand_id"], rule["property"])].append(rule)
    result, edges = [], []
    for (brand, property_name), items in groups.items():
        if len({r["source"] for r in items}) < 2:
            continue
        values = defaultdict(list)
        for rule in items:
            value = rule["value"]
            normalized = encode(rgb(value)) if rgb(value) else encode(value)
            values[normalized].append(rule["id"])
        if len(values) < 2:
            continue
        identity = "variation:" + digest(brand + ":" + property_name)[:24]
        result.append({"id": identity, "brand_id": brand, "property": property_name,
                       "rule_ids": [r["id"] for r in items], "distinct_values": len(values),
                       "status": "context-review-required",
                       "note": "Different source values; may reflect dates, surfaces, themes or analysis methods. Not an established contradiction."})
        edges.extend({"from": identity, "relation": "has_variant", "to": r["id"]} for r in items)
    return result, edges


def build_graph(root):
    output = root / "knowledge/design-rules"
    started = datetime.now(timezone.utc).isoformat()
    documents, originals, manifests = load_documents(root)
    rules, edges, brands = [], [], {}
    for document in documents:
        brands.setdefault(document["brand_id"], {"id": document["brand_id"], "name": document["brand"], "homepage": document["homepage"]})
        extracted = extract(document, *originals[document["id"]])
        calculated, relations = derive(document, extracted)
        rules.extend(extracted + calculated)
        edges.extend(relations)
        edges.extend([
            {"from": document["id"], "relation": "describes_brand", "to": document["brand_id"]},
            {"from": document["id"], "relation": "collected_from", "to": "source:" + document["source"]},
            {"from": document["id"], "relation": "shares_text_family", "to": document["content_family"]},
        ])
        for rule in extracted + calculated:
            edges.append({"from": document["id"], "relation": "contains_rule", "to": rule["id"]})
            edges.append({"from": rule["id"], "relation": "belongs_to_dimension", "to": "dimension:" + rule["category"]})
    variable_groups, relations = variations(rules)
    edges.extend(relations)
    # Brand links are navigation aids; an app archive screenshot does not verify a DESIGN.md claim.
    visual_records = []
    aliases_path = root / 'catalog/site-aliases.json'
    if aliases_path.exists():
        from site_registry import Resolver, ux_cases, match_case
        aliases=read(aliases_path)
        resolver=Resolver({'brands':list(brands.values()),'documents':documents},aliases)
        for identity, brand in brands.items():
            brand['site_entity_viewer']='catalog/sites/'+identity.replace(':','--')+'.html'
        for case in ux_cases(root):
            matched=match_case(case,resolver,brands)
            identity=matched.get('entity_id')
            if identity in brands:
                if not case['screens']:
                    continue
                visual_id = 'visual:' + case['id']
                visual_records.append({'id': visual_id, 'reference_id': case['record_ids'][0],
                                       'reference_count': len(case['record_ids']), 'product': case['product'],
                                       'platform': case['platform'], 'source_url': case['source_url'],
                                       'source': case.get('source', 'app-archive'),
                                       'path': case['screens'][0]['path'], 'local_viewer': case['local_viewer'],
                                       'entity_viewer': brands[identity]['site_entity_viewer'], 'match': matched,
                                       'flow_count': case['flow_count'], 'screen_count': case['screen_count'],
                                       'captured_label': case['captured_label'], 'brand_id': identity})
                edges.append({'from': identity, 'relation': 'has_visual_reference_not_rule_proof', 'to': visual_id})
    families = Counter(d["content_family"] for d in documents)
    stats = {"schema_version": 1, "built_at": started, "documents": len(documents), "brands": len(brands),
             "rules": len(rules), "edges": len(edges), "sources": dict(Counter(d["source"] for d in documents)),
             "rule_kinds": dict(Counter(r["kind"] for r in rules)), "categories": dict(Counter(r["category"] for r in rules)),
             "provider_quality": dict(Counter(d["quality_declared"] for d in documents)),
             "source_value_variations": len(variable_groups), "shared_body_families": sum(count > 1 for count in families.values()),
             "documents_with_parser_warnings": sum(bool(d["parser_warnings"]) for d in documents),
             "rules_with_unresolved_token_references": sum(bool(r.get("unresolved_references")) for r in rules),
             "linked_archive_visuals": sum(v.get('source', 'app-archive') == 'app-archive' for v in visual_records),
             "linked_ux_visuals": len(visual_records), "manifests": manifests,
             "note": "No DESIGN.md sources yet; add a local manifest under sources/<source>/<snapshot>/manifest.json" if not documents else "Source-backed rules; see evidence before adoption"}
    nodes = ([dict(b, type="brand") for b in brands.values()] +
             [dict(d, type="document") for d in documents] +
             [dict(r, type="rule") for r in rules] +
             [dict(v, type="variation") for v in variable_groups] +
             [dict(v, type="visual-reference") for v in visual_records] +
             [{"id": "source:" + source, "type": "source", "name": source} for source in stats["sources"]] +
             [{"id": "dimension:" + k, "type": "dimension", "label": v[0]} for k, v in CATEGORIES.items()] +
             [{"id": identity, "type": "text-family", "documents": count} for identity, count in families.items()])
    node_ids = {node["id"] for node in nodes}
    if len(node_ids) != len(nodes) or any(e["from"] not in node_ids or e["to"] not in node_ids for e in edges):
        raise ValueError("Graph has duplicate IDs or unresolved edges")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".graph-", dir=output) as temp:
        db_path = Path(temp) / "rules.sqlite"
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.executescript("""
                CREATE TABLE documents(id TEXT PRIMARY KEY, brand_id TEXT, source TEXT, data TEXT);
                CREATE TABLE rules(id TEXT PRIMARY KEY, brand_id TEXT, document_id TEXT, source TEXT,
                    category TEXT, kind TEXT, property TEXT, data TEXT);
                CREATE INDEX by_brand ON rules(brand_id, category, kind);
                CREATE TABLE edges(origin TEXT, relation TEXT, target TEXT);
                CREATE INDEX edge_origin ON edges(origin, relation);
                CREATE INDEX edge_target ON edges(target, relation);
                CREATE VIRTUAL TABLE rule_search USING fts5(property, value, context, brand, dimension, tokenize='unicode61');
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
            """)
            doc_by_id = {d["id"]: d for d in documents}
            for d in documents:
                connection.execute("INSERT INTO documents VALUES(?,?,?,?)", (d["id"], d["brand_id"], d["source"], encode(d)))
            for rowid, rule in enumerate(rules, 1):
                connection.execute("INSERT INTO rules VALUES(?,?,?,?,?,?,?,?)", tuple(rule[k] for k in ("id", "brand_id", "document_id", "source", "category", "kind", "property")) + (encode(rule),))
                d = doc_by_id[rule["document_id"]]
                connection.execute("INSERT INTO rule_search(rowid,property,value,context,brand,dimension) VALUES(?,?,?,?,?,?)",
                                   (rowid, rule["property"], encode(rule["value"]), encode(rule["context"]),
                                    d["brand"] + " " + str(d["industry"]) + " " + str(d["country"]) + " " + d["description"][:1000], CATEGORIES[rule["category"]][1]))
            connection.executemany("INSERT INTO edges VALUES(?,?,?)", [(e["from"], e["relation"], e["to"]) for e in edges])
            connection.execute("INSERT INTO metadata VALUES('stats',?)", (encode(stats),))
            connection.execute("INSERT INTO metadata VALUES('brands',?)", (encode(list(brands.values())),))
            connection.execute("INSERT INTO metadata VALUES('variations',?)", (encode(variable_groups),))
            connection.execute("INSERT INTO metadata VALUES('visual_references',?)", (encode(visual_records),))
        with closing(sqlite3.connect(db_path)) as check:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Graph database integrity check failed")
        db_path.replace(output / "rules.sqlite")
    for filename, value in [("graph.json", {"schema_version": 1, "stats": stats, "nodes": nodes, "edges": edges}),
                            ("catalog.json", {"stats": stats, "brands": list(brands.values()), "documents": documents,
                                              "variations": variable_groups, "visual_references": visual_records}),
                            ("stats.json", stats)]:
        temporary = output / ("." + filename + ".tmp")
        temporary.write_text(encode(value) + "\n", encoding="utf-8")
        temporary.replace(output / filename)
    return stats


def connection(root):
    path = root / "knowledge/design-rules/rules.sqlite"
    if not path.exists():
        raise ValueError("Build the graph first: python3 scripts/design_graph.py --root <data-directory> build")
    result = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    result.row_factory = sqlite3.Row
    return result


def query_expression(query):
    for original, replacement in QUERY_PHRASES.items():
        query = query.replace(original, replacement)
    terms = [QUERY_ALIASES.get(t, t) for t in re.findall(r"[^\W_]+(?:-[^\W_]+)*", query.casefold())]
    return " AND ".join('"' + term.replace('"', '""') + '"*' for term in terms)


def search(db, query="", brand=None, source=None, category=None, kind=None, limit=8, offset=0):
    expression = query_expression(query)
    where, params = [], []
    for name, value in (("source", source), ("category", category), ("kind", kind)):
        if value:
            where.append(f"r.{name}=?"); params.append(value)
    if brand:
        brand = QUERY_ALIASES.get(brand, brand)
        brands = json.loads(db.execute("SELECT value FROM metadata WHERE key='brands'").fetchone()[0])
        matches = [b["id"] for b in brands if key(brand) in {key(b["name"]), key(b["id"].removeprefix("brand:"))}]
        if not matches:
            return {"total": 0, "results": [], "reason": "Unknown brand; inspect stats or the catalog"}
        where.append("r.brand_id IN (" + ",".join("?" for _ in matches) + ")"); params.extend(matches)
    table = "rules r"
    rank = "0.0"
    if expression:
        table += " JOIN rule_search ON rule_search.rowid=r.rowid"
        where.insert(0, "rule_search MATCH ?"); params.insert(0, expression)
        rank = "bm25(rule_search,5,4,2,1,2)"
    predicate = " WHERE " + " AND ".join(where) if where else ""
    total = db.execute(f"SELECT count(*) FROM {table}{predicate}", params).fetchone()[0]
    rows = db.execute(f"SELECT r.data,{rank} AS score FROM {table}{predicate} ORDER BY score,CASE r.kind WHEN 'derived' THEN 0 WHEN 'token' THEN 1 ELSE 2 END,r.id LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
    results = []
    for row in rows:
        r = json.loads(row["data"])
        item = {k: r[k] for k in ("id", "brand_id", "source", "category", "kind", "property", "quality_declared")}
        encoded = encode(r["value"])
        item["value"] = r["value"] if len(encoded) <= 1200 else encoded[:1200] + "…"
        item["value_truncated"] = len(encoded) > 1200
        item["context"] = r["context"]
        item["evidence"] = r.get("evidence")
        item["inputs"] = r.get("inputs", [])
        item["unresolved_references"] = r.get("unresolved_references", [])
        results.append(item)
    return {"query": query, "total": total, "returned": len(results), "results": results,
            "scope": "Source statements and explicitly labeled calculations. Source labels are not independent verification."}


def show(db, identity, root):
    row = db.execute("SELECT data FROM rules WHERE id=?", (identity,)).fetchone()
    if not row:
        raise ValueError("Rule not found; use an ID returned by search")
    rule = json.loads(row[0])
    rule["document"] = json.loads(db.execute("SELECT data FROM documents WHERE id=?", (rule["document_id"],)).fetchone()[0])
    rule["document"]["absolute_path"] = str(root / rule["document"]["path"])
    rule["outgoing_relations"] = [dict(r) for r in db.execute("SELECT relation,target FROM edges WHERE origin=?", (identity,))]
    if rule.get("inputs"):
        rule["input_rules"] = [json.loads(db.execute("SELECT data FROM rules WHERE id=?", (item,)).fetchone()[0]) for item in rule["inputs"]]
    return rule


def draft(db, brief, identities, root):
    selected = [show(db, identity, root) for identity in dict.fromkeys(identities)]
    if not selected:
        raise ValueError("Select at least one rule with --rule; no design is selected automatically")
    properties = defaultdict(list)
    for item in selected:
        properties[item["property"]].append(item)
    conflicts = [name for name, items in properties.items() if len({encode(r["value"]) for r in items}) > 1]
    lines = ["# 프로젝트 디자인 규칙 초안", "", "상태: 초안 · 적용 전 맥락과 렌더링 검토 필요", "", "## 프로젝트 목적", "", brief,
             "", "## 선택한 근거와 결정", "", "다음 값은 선택한 레퍼런스의 근거다. 프로젝트에 사용할 값·이유·예외를 각각 결정한다."]
    for index, item in enumerate(selected, 1):
        document = item["document"]
        lines.extend(["", f"### D-{index:02d} · {item['property']}", "", f"- 분류: {CATEGORIES[item['category']][0]}",
                      f"- 근거 ID: `{item['id']}`", f"- 출처: {document['brand']} / {document['source']}",
                      f"- 출처 문서: [{document['brand']}]({document['source_url']})", f"- 원문 파일: `{document['absolute_path']}`",
                      f"- 원래 맥락: {encode(item['context'])}", f"- 출처 등급: {item['quality_declared']} (제공자 분류)",
                      "- 근거 값:", "", "```json", json.dumps(item["value"], ensure_ascii=False, indent=2), "```",
                      "", "- 프로젝트에서 채택할 값: 미결정", "- 채택/수정 이유: 미결정", "- 적용 화면·상태·반응형 범위: 미결정", "- 예외·검증 방법: 미결정"])
        if item.get("inputs"):
            lines.append("- 계산 입력 규칙: " + ", ".join(f"`{value}`" for value in item["inputs"]))
    lines.extend(["", "## 충돌·일관성 검토", "", *(["- 서로 다른 값이 선택된 속성: " + ", ".join(conflicts)] if conflicts else
                  ["- 선택한 동일 이름 속성에서 기계적으로 탐지된 값 차이는 없음. 이것이 전체 호환성을 보증하지는 않는다."]),
                  "- 색 역할과 상태색, 글자 크기에 따른 대비를 확인한다.", "- 제목/본문 폰트의 실제 가용성과 한글 지원을 확인한다.",
                  "- 폰트 크기 단위와 비율, 간격 단계, 콘텐츠 폭이 함께 작동하는지 확인한다.",
                  "- 컴포넌트 상태와 좁은 화면에서 렌더링을 검토한 뒤 프로젝트 규칙으로 채택한다.", ""])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="User-owned data library, outside this skill")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build")
    commands.add_parser("stats")
    finder = commands.add_parser("search")
    finder.add_argument("query", nargs="?", default="")
    for name in ("brand", "source", "kind"):
        finder.add_argument("--" + name)
    finder.add_argument("--category", choices=CATEGORIES)
    finder.add_argument("--limit", type=int, default=8)
    detail = commands.add_parser("show"); detail.add_argument("id")
    compare = commands.add_parser("compare"); compare.add_argument("brand")
    packet = commands.add_parser("packet"); packet.add_argument("--brand", required=True)
    packet.add_argument("--source")
    composer = commands.add_parser("draft")
    composer.add_argument("--brief", required=True)
    composer.add_argument("--rule", action="append", required=True)
    composer.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        root = library_root(args.root)
        require_library(root)
        if args.command == "build":
            result = build_graph(root)
        else:
            with closing(connection(root)) as db:
                if args.command == "stats":
                    result = json.loads(db.execute("SELECT value FROM metadata WHERE key='stats'").fetchone()[0])
                elif args.command == "search":
                    if not 1 <= args.limit <= 50:
                        raise ValueError("limit must be 1–50")
                    result = search(db, args.query, args.brand, args.source, args.category, args.kind, args.limit)
                elif args.command == "show":
                    result = show(db, args.id, root)
                elif args.command == "compare":
                    matches = search(db, brand=args.brand, limit=1)
                    brand_id = matches["results"][0]["brand_id"] if matches["results"] else None
                    data = json.loads(db.execute("SELECT value FROM metadata WHERE key='variations'").fetchone()[0])
                    result = {"brand": args.brand, "variants": [v for v in data if v["brand_id"] == brand_id],
                              "meaning": "Differences require source/date/surface review; do not average or silently overwrite them."}
                elif args.command == "packet":
                    result = {"brand": args.brand, "dimensions": {category: search(db, brand=args.brand, source=args.source, category=category, limit=3)["results"] for category in CATEGORIES},
                              "adoption": "Reference packet only. Choose project-specific roles and values, record reasons and exceptions, resolve variations, then validate the rendered result before adopting a DESIGN.md."}
                    brand_ids = {r['brand_id'] for rows in result['dimensions'].values() for r in rows}
                    visuals = db.execute("SELECT value FROM metadata WHERE key='visual_references'").fetchone()
                    result['ux_references'] = [v for v in json.loads(visuals[0]) if v['brand_id'] in brand_ids] if visuals else []
                else:
                    content = draft(db, args.brief, args.rule, root)
                    if args.output:
                        args.output.parent.mkdir(parents=True, exist_ok=True)
                        with args.output.open("x", encoding="utf-8") as file:
                            file.write(content)
                        result = {"draft": str(args.output.resolve()), "selected_rules": len(set(args.rule)), "status": "draft-not-adopted"}
                    else:
                        print(content)
                        return 0
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, ImportError) as error:
        print(encode({"error": str(error)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
