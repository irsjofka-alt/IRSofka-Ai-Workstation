# Skill Catalog Template (`brain/skills/SKILLS.md`)

Copy this file to `~/.ai-station/brain/skills/SKILLS.md` on the target host and populate the catalog
entries. As with memory: rules and templates are shared across machines, not private data.

## Two Distinct Formats (Do Not Mix)

**1. Workstation Skills** — operational notes, one topic per file:

```
skills/<category>/<topic>.md
   coding/  sysadmin/  visual/  analysis/  learned/
```

Read via the `SKILLS.md` index; not directly registered as CLI tool packs.

**2. Engine-Loaded Skills** — directories containing frontmatter-enabled `SKILL.md` and numbered reference files:

```
skills/<skill-name>/
   SKILL.md                      Frontmatter: name, description, when_to_use + table of contents
   01-<topic>.md                 Read when...
   02-<topic>.md
   references/<evidence>.md      Source quotations (keep separate from instructions)
   scripts/<tool>.py             Scripts utilized by this skill
```

Numbering must be two digits and sequential. Skill readers load `SKILL.md` completely, and load
numbered files on demand. Unnumbered files are treated as supplementary reference documentation.

**Past Gotcha:** Unquoted `description:` fields containing colons (e.g. `description: Contains verified limits: ...`)
corrupt YAML frontmatter parsing, causing the pack to **silently disappear** from engine lists without
error messages. Always quote string descriptions when containing special characters.

## Entry Points per Engine

A unified skill tree read by all engines, constructed from symlinks:

```
~/.ai-station/engines/agents/skills/<pack-name>/SKILL.md
   ^ Canonical source remains in ~/.ai-station/brain/skills/, never raw copies
```

| Engine | Loaded From | Status |
|---|---|---|
| Qoder CLI | `~/.agents/skills/` (`loadFromAgentsDirectory`, enabled by default) | Verified: recognized across active sessions without restarts |
| Antigravity / Gemini | `~/.gemini/config/plugins/station-rules/skills/` | Verified: recognized by global plugins |
| Local models | Do not read skill directories; prompts assembled by `tools/cross_verify.py` | |

`~/.agents` is a symlink to `~/.ai-station/engines/agents`, registered in `config/home_shims.json`.
The guard `hooks/self_preservation.py` permits symlink creation targeting workstation paths while
blocking arbitrary folder creation.

When adding new packs: create the directory in `brain/skills/` containing `SKILL.md`, then symlink
into `engines/agents/skills/`. Never place skills directly inside engine state folders.

## Autonomous Self-Learning Protocol

`tools/incident_recorder.py` tracks failure patterns grouped by `error_signature`.
Upon reaching 5 repeated occurrences:
1. Generates `skills/learned/<signature>.md`,
2. Registers the skill in table `skills_inventory` (`auto_learned = TRUE`),
3. Marks the incident `resolved`,
4. Appends the entry to this index.

Automated skills must not be deleted without record; if inaccurate, revise their content and document
the rationale within the file.

## Selective Loading Rules

1. Never load the entire catalog into context. One task = one skill.
2. Legacy skills from previous hosts serve as reference knowledge; obsolete file paths within them
   must not be accessed (consult `memory/legacy/LEGACY_MAP.md`).
3. An unindexed skill is an undiscoverable skill. Always maintain the catalog.
4. Contents of `skills/` are never committed to public git. The repository tracks only this template.
