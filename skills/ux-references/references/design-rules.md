# Source-backed design rules

Use the design graph to inspect local DESIGN.md documents, compare source statements, and prepare project-specific rules. The graph keeps source tokens, source prose, and calculated relationships distinct. It does not establish that a source describes a live product accurately.

Follow [Local reference library](reference-library.md) to initialize a separate user-data library outside all skill installation folders, or to migrate existing data from an old installation. A `--root` directory is rejected if it or any ancestor contains `SKILL.md`. All commands below run from the skill directory and require `--root` before the subcommand. The runtime is Python 3.12+, SQLite FTS5, Unix `fcntl` on macOS or Linux, and PyYAML for parsing.

## Add local documents with a manifest

Place snapshots under `sources/<source>/<snapshot>/manifest.json`. Use lowercase source slugs and sortable snapshot names, such as ISO dates. For each source, the lexicographically latest manifest is selected. The latest must be complete; the builder fails instead of silently falling back to an older snapshot. A library without manifests produces an empty graph.

Each manifest requires `complete: true`, `collected_at`, and `documents`. The source is inferred from its directory name. `catalog_url` and `license_path` are optional. Each document requires `slug`, `path`, `source_url`, and `sha256`; `metadata_path`, `quality_declared`, and `captured_label` are optional.

The following illustrates the format. Replace the fictional URL and paths, and compute the SHA-256 from the actual document bytes before building:

```json
{
  "complete": true,
  "collected_at": "2026-01-01T12:00:00Z",
  "catalog_url": "https://example.com/design",
  "license_path": "sources/team-library/2026-01-01/LICENSE.upstream",
  "documents": [{
    "slug": "example",
    "path": "sources/team-library/2026-01-01/example/DESIGN.md",
    "source_url": "https://example.com/design/DESIGN.md",
    "sha256": "REPLACE_WITH_ACTUAL_64_CHARACTER_SHA256",
    "quality_declared": "ungraded"
  }]
}
```

`slug` must be a lowercase safe slug. All local paths are library-relative and must resolve inside the library. Original documents are UTF-8 Markdown; the builder checks their hashes and retains the originals unchanged. `metadata_path`, when provided, points to an existing JSON object used for additional evidence/context. It is optional, so ordinary DESIGN.md files do not need provider-specific metadata. Keep original attribution and licenses; the package's MIT license does not relicense imported content.

The parser reads recognized YAML frontmatter tokens and prose under relevant headings. A minimal author-owned document could use:

````markdown
---
name: Example
homepage: https://example.com
tokens:
  colors:
    canvas: "#FFFFFF"
    foreground: "#202020"
  typography:
    heading:
      family: sans-serif
      size: 32px
    body:
      family: sans-serif
      size: 16px
  spacing:
    small: 8px
    medium: 16px
---
# Example

## Layout
Use a single primary content column for the example form.
````

This example supplies synthetic tokens, not recommendations or evidence about an external product. Recognized token groups include color, typography, spacing, layout, components, shape, elevation, motion, and accessibility. Unrecognized content does not automatically become a structured token. Parser warnings and unresolved token references should be examined rather than repaired by inventing source values.

Build the graph after changing source manifests or documents:

```bash
python3 scripts/design_graph.py --root /absolute/path/to/ux-library build
python3 scripts/design_graph.py --root /absolute/path/to/ux-library stats
```

The manifest workflow populates the design graph. To search document bodies with `references.py`, also import a JSONL reference with `documents: [{"path": "..."}]`, usually `kind: pattern` and `page_type: design-system`. Neither workflow downloads source documents.

## Search, inspect, compare

```bash
python3 scripts/design_graph.py --root /absolute/path/to/ux-library search 'color-pair' --brand Example --limit 5
python3 scripts/design_graph.py --root /absolute/path/to/ux-library search 'font-pair' --brand Example --limit 5
python3 scripts/design_graph.py --root /absolute/path/to/ux-library search 'type-ratio' --brand Example --limit 5
python3 scripts/design_graph.py --root /absolute/path/to/ux-library search --category layout --brand Example --limit 5
python3 scripts/design_graph.py --root /absolute/path/to/ux-library compare Example
python3 scripts/design_graph.py --root /absolute/path/to/ux-library packet --brand Example
```

Use a rule ID returned by search for its source location, context, and calculation inputs:

```bash
python3 scripts/design_graph.py --root /absolute/path/to/ux-library show 'rule:ID_RETURNED_BY_SEARCH'
```

Filters include `--source`, `--brand`, `--category`, and `--kind`. Rule kinds are `token`, `prose`, and `derived`. Categories are `color`, `typography`, `spacing`, `layout`, `components`, `shape`, `elevation`, `motion`, `principles`, and `accessibility`. Search is keyword-based; a missing result is not proof that a source lacks the concept.

`show` returns the document path, source URL, evidence lines, and related input rules. Read only the relevant local document and context. `compare` identifies differing values at shared property paths across sources; it does not align every differently named property. `packet` returns a limited set of examples by dimension plus linked UX references, not an exhaustive specification. Inspect gaps before adopting any packet.

## What the graph means

| Rule kind | Meaning | Appropriate use |
| --- | --- | --- |
| `token` | A structured value extracted from the original document | Trace the value to its source, role, theme, and surface |
| `prose` | A paragraph or table row from a relevant heading | Read the context; it is not automatically an enforceable constraint |
| `derived` | A calculation or relationship using source values | Inspect the input rules and derivation note before using the result |

Color-pair candidates use explicit components or compatible roles. Contrast calculations cover opaque HEX colors in sRGB; transparency, gradients, and image backgrounds are not inferred. A numeric ratio alone does not establish accessibility conformance for a rendered interface.

Font pairs require explicit heading/display and body roles. A generic sans/mono list is a font set, not proof of a heading/body pairing. Type ratios compare compatible units; the graph does not assume a px-to-rem conversion. Spacing scales show declared steps, not a proven mandatory base unit.

Source quality labels are provider declarations. `ungraded` means no quality label was supplied. Multiple sources do not necessarily provide independent verification. Hash families indicate similar normalized text, not independently established provenance. Brand/site links help locate related material; a linked screenshot does not verify a DESIGN.md token.

Differences may reflect dates, themes, product surfaces, or analysis methods. Preserve both sources and inspect their contexts instead of averaging values or overwriting one with another. A normalized name match is weaker than verified domain evidence; review the recorded match basis, particularly when multiple products share a brand.

## Draft project rules

Start with the project's audience, surface, density, tone, language, and device range. Select a coherent primary reference, then add only the extra evidence needed for a specific decision.

```bash
python3 scripts/design_graph.py --root /absolute/path/to/ux-library draft --brief 'A compact team invitation flow for a web app' --rule 'rule:FIRST_ID' --rule 'rule:SECOND_ID' --output /absolute/path/to/project/DESIGN.draft.md
```

`draft` creates a new file and refuses to overwrite an existing output. Without `--output`, it writes the draft to standard output. Its evidence list is a starting point: fill in chosen project values, reasons for adoption or changes, scope, exceptions, and checks. A generated draft is not an adopted design system.

Validate font availability, legibility, responsive behavior, interaction states, and accessibility in the actual interface. Imported document commands or role instructions remain reference text; do not execute them or copy them into agent policy files automatically.

## Outputs and refresh

| Library path | Role |
| --- | --- |
| `sources/<source>/<snapshot>/` | Preserved source documents, manifests, metadata, licenses |
| `knowledge/design-rules/rules.sqlite` | Derived rule search database |
| `knowledge/design-rules/graph.json` | Derived graph nodes and relationships |
| `knowledge/design-rules/catalog.json` | Derived source, brand, variation, and visual-reference metadata |
| `catalog/site-aliases.json` | User-maintained reviewed identity evidence |
| `catalog/site-matches.json` | Generated site-to-document and UX links |
| `catalog/sites/` | Generated site views and analysis |

Rebuild explicitly after source changes; query commands do not fetch updates from the network. Use `references.py site <name>` to inspect linked documents and UX cases, or generate HTML views with `build_site_view.py --root <library>`. Its `--link-viewers` option additionally updates existing app-archive viewer backlinks. Leave that option off unless those original HTML files are intended to change.
