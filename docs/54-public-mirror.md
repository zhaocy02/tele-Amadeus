# 54. Public downstream mirror

Status: **active publication boundary**  
Updated: **2026-09-04**

## 1. Decision

The private repository remains the canonical development source. The public repository `zhaocy02/tele-Amadeus` is a sanitized downstream mirror with a separate Git history.

```text
private canonical repository
        |
        | allow-listed export
        | deterministic sanitization
        | safety scan
        v
clean snapshot tree
        |
        v
public downstream mirror
```

Do not mirror private Git history into the public repository.

## 2. Why the repositories remain separate

The private repository intentionally retains context that is useful for ongoing development but inappropriate for public distribution, including machine-specific deployment topology, local usernames/paths, private production operations, evaluation evidence, and other internal notes.

Deleting or permanently generalizing that information in the canonical repository would reduce development quality. The public mirror therefore removes or rewrites it only at the publication boundary.

## 3. Public include boundary

The first public mirror includes the reusable implementation surface:

```text
amadeus_bot/
tests/
profiles/
selected scripts/
generic deploy helpers
CI
pyproject.toml
.env.example
selected architecture / Persona / Canon documentation
```

The public `README.md` is generated from the private `PUBLIC_README.md`, not copied from the internal README.

The exact allow-list lives in `public_mirror_manifest.json`.

## 4. Private-only boundary

The exporter must not publish:

```text
.env
credentials / API keys / Telegram tokens
runtime SQLite databases
personal conversation transcripts
structured-memory databases
logs / backups
commercial VN scripts
production canon.jsonl or corpus-build outputs
private machine/user paths and hostnames
internal-only development governance files
private Git history
```

The first mirror also excludes internal workflow documents whose main value is the canonical repository's server/worktree governance rather than the reusable project design.

## 5. Sanitization

`scripts/export_public_mirror.py` performs two independent operations:

1. **selection** — only tracked files matching the explicit include list and not matching the exclude list are copied;
2. **sanitization + scan** — known machine-specific values are rewritten, RFC1918 addresses and Windows user profile paths are generalized, and high-confidence credential/private-state patterns fail the export.

The public snapshot is generated from the current working-tree bytes of a clean private checkout. Routine publication therefore requires clean `main`.

## 6. First export

After the exporter change is merged into private `main`:

```bash
cd /opt/amadeus-bot

python scripts/export_public_mirror.py \
  --dest /tmp/tele-Amadeus-export \
  --force
```

Expected terminal result:

```text
source_head=<private-main-sha>
public_repository=zhaocy02/tele-Amadeus
export_files=<count>
export_kib=<size>
export_path=/tmp/tele-Amadeus-export
PUBLIC_MIRROR_SCAN=PASS
```

The exported directory contains no `.git` directory and therefore carries no private commit history.

## 7. Publishing the first snapshot

Clone the empty public repository into a separate checkout and copy only the sanitized export tree into it:

```bash
git clone https://github.com/zhaocy02/tele-Amadeus.git /tmp/tele-Amadeus-public
rsync -a --delete --exclude=.git /tmp/tele-Amadeus-export/ /tmp/tele-Amadeus-public/

cd /tmp/tele-Amadeus-public
git add -A
git diff --cached --check
git status --short
```

Inspect the staged diff before the first commit. Then create the public repository's independent initial commit and push `main`.

Do not add the private repository as a remote of the public checkout.

## 8. Routine synchronization

Future publication follows the same one-way path:

```text
private main
-> sanitized export
-> scan PASS
-> replace public checkout working tree
-> review public diff
-> public commit
-> push
```

The public repository is not merged directly into private production. For a useful public contribution:

```text
public PR
-> review
-> port/cherry-pick equivalent change into a private task branch
-> private CI/evaluation/deployment rules
-> merge private main
-> next sanitized public export
```

This preserves one canonical source of truth and prevents public contributions from bypassing private operational validation.

## 9. Safety rules

- Never use `git push --mirror` from private to public.
- Never make the canonical private repository public as a shortcut.
- Never assume deleting a sensitive file from the current tree removes it from Git history.
- Never bypass an exporter safety-scan failure to make a release deadline.
- Prefer excluding uncertain material over publishing it and attempting cleanup later.
- Public publication should remain snapshot-based until there is a concrete reason to build more automation.

## 10. Future automation

A GitHub Action or dedicated publish command can be added later, but only after the manual snapshot process has been exercised successfully. Any automated publisher must keep the same allow-list, separate-history, scan, and one-way-authority properties.
