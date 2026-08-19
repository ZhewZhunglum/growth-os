# Post-Closure Backlog

This backlog starts only after the frozen minimum vertical loop passes its acceptance matrix. It does not change the first-priority Runtime scope.

## P1 - Usable Dogfood expansion

### 1. Complete product and compliance context

- Seal exact Objective Profile, Claims Matrix, controlled Evidence Library, brand voice, visual references, and product assets.
- Preserve the exact versions used by old tasks while enforcing current mandatory rules at release time.
- Done when the compiler can explain what the product is, who it serves, what may be claimed, what is prohibited, and which controlled evidence supports each claim.

### 2. Manual or CSV external-demand path

- Import external evidence with URL, source, platform, surface, discovery mechanism, timestamps, and provenance.
- Create Topic, Demand Assessment, and a human-confirmed Product Opportunity.
- Prove that Performance, Commerce, Process, Quick Test, and GEO records cannot enter External Demand.
- Done when one real or sanitized external signal reaches the opportunity queue without internal-data contamination.

### 3. Opportunity to executable task

- Create Initiative and Channel Plan from an approved Product Opportunity.
- Generate natural-language task cards containing Why, steps, DoR, DoD, assets, red lines, and Success Criteria.
- Prevent WATCH or BLOCKED plans from producing publishable work.
- Done when one opportunity generates platform-native tasks bound to exact configuration and policy versions.

### 4. Role-oriented team experience

- Operator: personal work queue and task execution.
- Operations Admin: assignment, review, rework, and blocker handling.
- Owner: opportunity, approval, capability, and progress oversight.
- Add searchable Content Library views without changing asset facts.
- Done when one user can simulate all three roles without relying on separate Codex tasks to reconstruct context.

### 5. Thin Issue workflow

- Report a blocker or safety issue from a Task or Publication.
- Allow Admin triage, assignment, resolution, and reopen through immutable events.
- An Issue cannot directly change a rule.
- Done when an operational problem is handled and auditable entirely inside the system.

### 6. Performance and SEO feedback

- Add manual or CSV collection for Publication, Channel, and Search observations.
- Preserve content age, maturity window, organic/paid scope, numerator, denominator, and provenance.
- Keep `0`, `Missing`, `Unavailable`, and `Blocked` distinct.
- Add basic Content, Channel, and SEO views without claiming causality.
- Done when published work can be compared with an appropriate historical baseline.

### 7. Thin GEO loop

- Use a fixed Prompt Panel and record provider/model, query, market, language, run time, mentions, citations, sources, and sentiment.
- Keep the GEO domain isolated from External Demand, Performance, and Commerce.
- Start with manual or batch import; no automatic cross-provider routing.
- Done when the system can answer whether an AI response mentioned or cited the brand without contaminating demand evidence.

### 8. Human Learning

- Allow the Owner to seal a Learning based on exact evidence and observations.
- Mark a Learning stale or in need of review when its evidence is invalidated.
- A Learning may suggest a later rule proposal but cannot activate or relax a rule.
- Done when validated experience is retained without allowing silent AI policy changes.

## P2 - After stable Dogfood

- Commerce observations: Product View, Add to Cart, Checkout, Purchase, Revenue, currency, refund, and adjustment semantics.
- Process telemetry: DoR incompleteness, blockers, rework loops, time to first action, compiler override rate, and abandonment.
- Dynamic platform-policy monitoring and human rule proposals.
- One connector at a time, only after its idempotency, provenance, rate-limit, retry, and domain-isolation tests pass.
- Expanded dashboards and additional B2C products after the PUKO single-product flow is stable.

Process telemetry evaluates the workflow and compiler. It must not automatically rank, punish, or reward employees.

## Blueprint only - not part of the current implementation

- Full meeting-governance UI and automated Replay/Shadow/Canary/Activation.
- Automatic rule approval, automatic relaxation, or automatic rollback of a safety tightening.
- Automatic publishing, advertising, budget changes, and account creation.
- Complex multi-touch attribution or causal claims.
- Multiple primary assignees, dependency graphs, and automatic task unlocking.
- Full asset-derivation graph and complex carousel editor.
- All-platform connector rollout in one release.
- Automatic multi-model routing and provider fallback.
- Local-model deployment, B2B, Leads, CRM, multitenancy, billing, and agency SaaS functions.

## Scope gate

P1 permits only changes required for usability, security, data integrity, feedback, and launch readiness. Any request that cannot demonstrate that it blocks the current acceptance stage moves to P2 or Blueprint.

