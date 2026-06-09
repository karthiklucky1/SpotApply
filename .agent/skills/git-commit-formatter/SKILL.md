---
name: git-commit-formatter
description: "Analyzes staged workspace changes to format and propose a structured Git commit message following the Conventional Commits specification."
---

# Git Commit Formatter Skill

## Goal
To analyze staged git diffs and workspace updates to generate precise, structured commit messages adhering strictly to the Conventional Commits v1.0.0 specification.

## Instructions

### 1. Structural Format
Every commit message must follow this structure:
```
<type>(<scope>): <subject>

[optional body]

[optional footer(s)]
```

### 2. Determine Commit Type
Choose the most accurate type based on the modifications:
- **feat**: A new feature or capability.
- **fix**: A bug fix.
- **docs**: Documentation updates (e.g., changes to README, markdown skills, or docstrings).
- **style**: Changes that do not affect the meaning of the code (formatting, missing semi-colons, white-space cleanup).
- **refactor**: A code change that neither fixes a bug nor adds a feature (e.g., modularizing code, optimizing structure).
- **perf**: A code change that improves performance.
- **test**: Adding missing tests or correcting existing tests.
- **build**: Changes that affect the build system or external dependencies (e.g., requirements.txt, setup.py).
- **ci**: Changes to CI configuration files and scripts.
- **chore**: Other changes that don't modify src or test files (e.g., .gitignore updates).

### 3. Identify Scope
- The scope must be a noun representing a specific section of the codebase affected by the changes.
- Wrap it in parentheses after the type. Examples: `(dashboard)`, `(autofill)`, `(database)`, `(matching)`.
- If the changes span multiple areas or represent a system-wide update, scope can be omitted.

### 4. Subject Line Guidelines
- Write the subject line in the **imperative, present tense** mood (e.g., "add dashboard filter" instead of "added dashboard filter" or "adds dashboard filter").
- Use lowercase.
- Do not add a trailing period (`.`).
- Keep it under 50 characters if possible (absolute max 72).

### 5. Commit Body & Footer
- Use the body to explain the **motivation** behind the change and the **rationale** (the "why"), rather than just describing the code itself.
- Wrap the body lines at 72 characters.
- If there is a breaking change, append a `!` after the type/scope (e.g., `feat(auth)!: ...`) and start the footer section with `BREAKING CHANGE: <explanation>`.

---

## Examples

### 1. Feature addition (with scope)
```
feat(autofill): add support for SmartRecruiters forms

Implement custom Playwright form fillers for SmartRecruiters ATS portals.
Detect and parse input fields for name, email, resume upload, and custom 
screening questions.
```

### 2. Bug fix (no scope)
```
fix: resolve database concurrency race during matching

Add SELECT FOR UPDATE lock to job queries in matching pipeline to prevent
duplicate application creations when concurrent runners trigger.
```

### 3. Breaking Change (with footer)
```
refactor(db)!: upgrade job model to SQLModel v2

Migrate all database schemas to SQLModel v2. This changes the underlying 
metadata formats and requires database re-initialization.

BREAKING CHANGE: The local jobs.db database must be deleted and re-created
using `python -m app.db.init_db`.
```

---

## Constraints
- **Diff Matching:** The commit message must strictly match the changes shown in `git diff --cached`. Do not describe files or changes that are not staged.
- **No Plural Imperatives:** Do not use words like "fixes", "adds", "commits". Use "fix", "add", "commit".
