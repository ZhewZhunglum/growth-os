# Growth OS - Dogfood V1 Implementation Pack

Status: **Runtime scope frozen; source code not yet received or verified**  
Working name only: the final product name has not been selected.  
Snapshot date: 2026-08-18 (Asia/Shanghai)

## Purpose

This folder converts the approved business and data-contract discussions into a bounded implementation and acceptance package. It is the reference used to review developer delivery, run Dogfood, and decide whether Production is Go or No-Go.

It does not contain production credentials, platform cookies, API keys, database passwords, or other secrets. Secrets must never be added to this folder.

## Document order

Start with `README-中文说明.md` for a plain-language Chinese overview.

1. `00-DOGFOOD-V1-RUNTIME-FREEZE.md` - authoritative V1 runtime boundary and invariants.
2. `01-V1-ACCEPTANCE-MATRIX.md` - five required end-to-end paths and evidence.
3. `02-DELIVERY-EVIDENCE-AND-DEPLOYMENT-GATES.md` - source-code delivery, Staging, disaster recovery, and Production gates.
4. `03-POST-CLOSURE-BACKLOG.md` - ordered work after the minimum vertical loop.
5. `04-DECISION-LOG-2026-08-18.md` - decisions and unresolved items from the development meeting.
6. `05-CODE-DELIVERY-MAPPING-TEMPLATE.md` - template for mapping delivered code to the frozen contract.

## Authority and change discipline

- The Runtime Freeze takes priority over later informal suggestions.
- New ideas go to the backlog or Blueprint by default.
- V1 may change only for a demonstrated P0 issue affecting security, data integrity, or the required acceptance paths.
- A date is a target, not a release criterion. Production requires every mandatory gate to pass.
- Developer-reported completion percentages are informational until the code, migrations, tests, and clean-environment startup are independently reproduced.

## Current implementation status

- Business Blueprint: substantially defined.
- Dogfood V1 Runtime scope: frozen.
- Developer source delivery: pending.
- Verified implementation, automated tests, Staging, and Production: not yet established.
