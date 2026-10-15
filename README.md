# Agent Skills

Reusable skills for working with local reference libraries and independent AI reviews.

| Skill | What it does | Requirements |
| --- | --- | --- |
| [ir-references](skills/ir-references/SKILL.md) | Index pitch decks and documents, analyze speaker notes and language, and keep findings tied to their sources. | Python 3.11+; `pdftotext` for PDFs; your own reference files |
| [ux-references](skills/ux-references/SKILL.md) | Search a local UI/UX reference catalog, explore design rules, and assemble site reference views. | Python 3.12+, SQLite FTS5, PyYAML; macOS or Linux; your own reference library |
| [claude-review](skills/claude-review/SKILL.md) | Prepare a bounded review packet for Claude Code, then verify findings before making changes. | An installed and authenticated Claude Code CLI; access to the selected model |

## Install

With the [Skills CLI](https://github.com/vercel-labs/skills):

```sh
npx skills add nodeving/agent-skills --list
npx skills add nodeving/agent-skills --skill ir-references --skill ux-references --skill claude-review
```

Choose the agent and installation scope in the installer. You can also clone this repository and copy individual folders from `skills/` into your agent's supported skill directory. Keep each skill's scripts and references with its `SKILL.md`.

For the Python tools, use a virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Read the selected skill's setup instructions before its first run. The reference tools require an explicit library path; installation does not include a reference collection.

## Try a skill

- **IR:** “Use `$ir-references` to compare the structure and presentation language in my local pitch decks. Cite the source slide or note for each finding.”
- **UX:** “Use `$ux-references` to find relevant onboarding patterns in my local library and explain the design rules behind them.”
- **Review:** “Use `$claude-review` to review this change against the requirements. Return actionable findings without editing the files.”

The review skill defaults to the `opus` alias with `xhigh` effort. A user-requested model or effort takes precedence. It is explicitly invoked and does not install hooks or change global settings.

## Data and scope

This repository contains instructions, helper code, and synthetic tests. It does not include personal decks, design screenshots, collected catalogs, credentials, or deployment infrastructure.

IR and UX helpers process the reference library you specify. Generated catalogs can contain extracted text and images; keep them outside this repository. The Claude review workflow sends the selected review packet to Claude, so use only material you are permitted to share with that service.

The MIT license covers the code and instructions here. Reference materials you add keep their own copyright and license terms.

## Check the helpers

From the repository root, after installing the Python dependencies:

```sh
python3 -m unittest discover -s skills/ir-references/scripts -p 'test_*.py'
python3 -m unittest discover -s skills/ux-references/tests -p 'test_*.py'
```

These tests use temporary, synthetic libraries. They do not run a paid model review or use your private reference collection.

## Contributing

Keep changes scoped to a demonstrated use case. Add or update a synthetic test when changing executable behavior, and include the commands you ran in the pull request. Do not attach private reference materials to issues or pull requests.

## License

[MIT](LICENSE). Each installable skill includes its own copy of the license notice.
