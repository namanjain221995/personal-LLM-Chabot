# Rollback Runbook

How to roll back a feature in the Techsara production org using this repo's snapshots.

## 0. Ground rules

- **Prod is read-only for automation.** Every command in this runbook that deploys is
  executed by a **human**, after a named approver signs off (checklist in §5).
- **Always validate first**: run every deploy with `--dry-run` before the real thing.
- Roll back **only the affected components**, never the whole `force-app`.
- All commands below run from the repo root (`git` commands) or from inside
  `Prod Org Data` (`sf` commands). Quote paths — they contain spaces.

## 1. Find the restore point

```bash
git fetch origin --tags
git tag --sort=-creatordate -n1 | head -20        # newest tags + their messages
git log --oneline --decorate -15 main             # recent snapshots
```

- `deploy/<date>-<feature>` = state right AFTER that feature was deployed.
  Its **predecessor** commit/tag = state right BEFORE it → that's your restore point.
- `week-N` branches = coarse navigation ("the org ~N weeks ago").
- Compare two points to see what a deploy actually changed:

```bash
git diff <prev-tag>..<deploy-tag> --stat -- "Prod Org Data/force-app"
git diff <prev-tag>..<deploy-tag> -- "Prod Org Data/force-app/main/default/classes/Foo.cls"
```

## 2. Partial rollback of one feature (modified/changed components)

```bash
# 2.1 List exactly what the bad deploy changed
git diff --name-only <prev-tag>..<deploy-tag> -- "Prod Org Data/force-app"

# 2.2 On a temp branch, restore ONLY those paths to their pre-deploy state
git checkout -b rollback/<feature> main
git checkout <prev-tag> -- "Prod Org Data/force-app/main/default/classes/Foo.cls" \
                           "Prod Org Data/force-app/main/default/triggers/Bar.trigger"

# 2.3 Validate against prod (no changes made — checkOnly)
cd "Prod Org Data"
sf project deploy start --source-dir force-app/main/default/classes/Foo.cls \
                        --source-dir force-app/main/default/triggers/Bar.trigger \
                        --target-org Techsara --dry-run --test-level RunLocalTests

# 2.4 ONLY after approval (§5): same command without --dry-run
```

After the rollback deploy succeeds, **trigger the backup job** (manual run,
`FEATURE_NAME=rollback-<feature>`) so `main` reflects reality again.

## 3. The destructive-changes trap (components ADDED by the bad deploy)

Re-deploying old versions does **not** remove components that were *added* after the
restore point — a new field, class, or flow stays in prod. Handle them explicitly:

```bash
# 3.1 What exists now that didn't exist at the restore point?
git diff --name-only --diff-filter=A <prev-tag>..<deploy-tag> -- "Prod Org Data/force-app"
```

Map each file to type + fullName, e.g.:

| File | Type | Member |
|---|---|---|
| `classes/NewThing.cls` | ApexClass | `NewThing` |
| `objects/Account/fields/X__c.field-meta.xml` | CustomField | `Account.X__c` |
| `flows/New_Flow.flow-meta.xml` | Flow | `New_Flow` |

Create `destructiveChanges.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Package xmlns="http://soap.sforce.com/2006/04/metadata">
    <types>
        <members>NewThing</members>
        <name>ApexClass</name>
    </types>
    <types>
        <members>Account.X__c</members>
        <name>CustomField</name>
    </types>
</Package>
```

and an empty `package-empty.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Package xmlns="http://soap.sforce.com/2006/04/metadata">
    <version>67.0</version>
</Package>
```

Validate, then (after approval) execute:

```bash
sf project deploy start --manifest package-empty.xml \
    --post-destructive-changes destructiveChanges.xml \
    --target-org Techsara --dry-run --test-level RunLocalTests
```

**Warnings**: deleting a CustomField **destroys its data** (recoverable from the recycle
bin for only 15 days); deleting Apex runs tests; components referenced elsewhere will
block deletion. Name every component in the approval.

## 4. High-risk component types — extra care

| Type | Risk |
|---|---|
| **Profiles** | Retrieved profile content depends on what else was retrieved with it; redeploying an old profile can silently strip permissions/FLS granted since. Prefer rolling back via Permission Sets, or hand-edit the profile diff down to the exact lines. |
| **Settings** | Org-wide switches (sharing, security, features). Diff line-by-line; deploy only the one settings file you mean to change. |
| **ConnectedApp / ExternalClientApp** | Consumer secrets are never retrieved; redeploying can invalidate integrations. `Jenkins_CICD` and `Production_Org_Read_only` live here — breaking them breaks CI/CD and this very backup pipeline. |
| **Flows** | Old flow versions may reactivate differently; check active-version semantics after deploy. |
| **Experience sites** | The two LWR sites are NOT in this backup at all (API limitation) — rollback for them is manual in Setup. |

Rule of thumb: these types stay `--dry-run`-only until a second reviewer approves the exact diff.

## 5. Approval checklist (fill in the ticket before the real deploy)

- [ ] Incident/ticket link:
- [ ] Restore point (tag/commit):
- [ ] Exact component list (from §2.1 / §3.1):
- [ ] Dry-run deploy ID + result:
- [ ] Data-loss assessment (any CustomField deletions?):
- [ ] Approver name + timestamp:
- [ ] Post-deploy verification steps (what to click/check in prod):
- [ ] Backup job re-run afterwards (`FEATURE_NAME=rollback-<feature>`):
