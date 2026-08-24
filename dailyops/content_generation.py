from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.authorization import resolve_authorization
from contentops.models import ContentAsset, ContentAssetVersion, canonical_sha256
from dailyops.content_schemas import (
    CONTENT_DRAFT_SCHEMA,
    PLATFORM_CONTENT_TYPES,
    render_complete_content,
)
from integrations.ai.providers import AIProvider, DryRunAIProvider
from integrations.ai.types import AIExecutionStatus, AIMessage, AIRequest, StructuredOutputSpec
from integrations.connectors.types import Platform
from intelligence.models import DataDomain, DecisionState, ExternalEvidenceItem, TaskCompilationContext
from products.models import ProductClaimVersion
from releasegate.models import PolicyVersion
from releasegate.services import KNOWN_V1_RULES
from workflow.models import Task


CONTENT_TARGET_PLATFORMS = frozenset(
    {Platform.TIKTOK, Platform.PINTEREST, Platform.QUORA, Platform.SHOPIFY}
)
TEMPLATE_VERSION = "daily-content-v1"
GENERATION_RULE_VERSION = "external-demand-claims-policy-v1"


@dataclass(frozen=True, slots=True)
class ContentDraftResult:
    asset_version: ContentAssetVersion
    created: bool


def _require_exact_edit_grant(*, task: Task, principal, acting_role: str, permission_grant) -> None:
    if permission_grant.action != "EDIT":
        raise PermissionDenied("CONTENT_GENERATION_REQUIRES_EDIT_GRANT")
    decision = resolve_authorization(
        principal=principal,
        acting_role=acting_role,
        action="EDIT",
        scope_kind="PRODUCT",
        product=task.product_id,
    )
    if not decision.allowed or decision.grant is None or decision.grant.pk != permission_grant.pk:
        raise PermissionDenied("CONTENT_GENERATION_EDIT_GRANT_INVALID")


def _subcommand_id(command_id: uuid.UUID, label: str) -> uuid.UUID:
    return uuid.uuid5(command_id, label)


def _context_for_task(task_id) -> TaskCompilationContext:
    try:
        return TaskCompilationContext.objects.select_related(
            "task",
            "product",
            "product_profile_version",
            "task_contract_version",
            "objective_profile_version",
            "claim_matrix_version",
            "evidence_library_version",
            "channel_plan__initiative__opportunity__topic",
            "channel_plan__initiative__opportunity__demand_assessment",
            "capability_state",
        ).get(task_id=task_id)
    except TaskCompilationContext.DoesNotExist as exc:
        raise ValidationError("这项任务缺少已封存的生成上下文，不能生成内容。") from exc


def _latest_generated_content(task_id) -> ContentAssetVersion | None:
    return ContentAssetVersion.objects.select_related("content_asset").filter(
        content_asset__task_id=task_id,
        content_asset__asset_key="publishable-content",
        representation_kind=ContentAssetVersion.RepresentationKind.INLINE_TEXT,
    ).order_by("-version_number", "-created_at", "-id").first()


def _platform(context: TaskCompilationContext) -> Platform:
    try:
        platform = Platform(context.channel_plan.platform_code)
    except ValueError as exc:
        raise ValidationError("任务的平台不在 Daily Operations V1 的七个平台范围内。") from exc
    if platform not in CONTENT_TARGET_PLATFORMS:
        raise ValidationError(
            "Google Search、GSC 和 GA4 是研究/数据来源，不是发帖目标；请为 TikTok、Pinterest、Quora 或 Shopify 创建内容任务。"
        )
    return platform


def _valid_evidence(context: TaskCompilationContext, *, lock: bool = False) -> list[ExternalEvidenceItem]:
    assessment = context.channel_plan.initiative.opportunity.demand_assessment
    if assessment.data_domain != DataDomain.EXTERNAL_DEMAND:
        raise ValidationError("内容生成只能使用 External Demand 数据域。")
    if assessment.decision_state != DecisionState.APPROVED:
        raise ValidationError("内容生成要求已经人工通过的需求判断。")
    evidence_ids = assessment.evidence_links.order_by("evidence_item_id").values_list(
        "evidence_item_id", flat=True
    )
    queryset = ExternalEvidenceItem.objects.filter(
        pk__in=evidence_ids,
        data_domain=DataDomain.EXTERNAL_DEMAND,
        invalidation_event__isnull=True,
    ).select_related("source", "collection_run")
    if lock:
        queryset = queryset.select_for_update()
    now = timezone.now()
    items = [item for item in queryset.order_by("id") if item.expires_at is None or item.expires_at > now]
    if not items:
        raise ValidationError("没有仍然有效的外部需求证据，系统不会无依据生成发布内容。")
    return items


def _allowed_claims(context: TaskCompilationContext, platform: Platform) -> tuple[set[str], list[str]]:
    now = timezone.now()
    allowed: set[str] = set()
    blocked_wordings: list[str] = []
    items = context.claim_matrix_version.items.select_related("product_claim_version").all()
    for item in items:
        claim = item.product_claim_version
        if claim.platform_code and claim.platform_code != platform.value:
            continue
        if claim.valid_from > now or (claim.valid_until and claim.valid_until <= now):
            continue
        if claim.claim_type == ProductClaimVersion.ClaimType.ALLOWED and (
            claim.evidence_level != ProductClaimVersion.EvidenceLevel.UNSUPPORTED
        ):
            allowed.add(claim.claim_key)
        else:
            blocked_wordings.append(claim.wording)
    return allowed, blocked_wordings


def _profile_prohibited_expressions(context: TaskCompilationContext) -> list[str]:
    values = context.product_profile_version.prohibited_expressions
    if not isinstance(values, list):
        raise ValidationError("产品配置中的禁用表达格式无效。")
    normalized: list[str] = []
    for value in values:
        if isinstance(value, str):
            text = value
        elif isinstance(value, dict):
            text = value.get("expression") or value.get("wording") or value.get("text") or ""
        else:
            text = ""
        if str(text).strip():
            normalized.append(str(text).strip())
    return normalized


def _validate_generation_policy_set(
    context: TaskCompilationContext,
    *,
    lock: bool = False,
) -> list[dict[str, Any]]:
    """Fail closed unless every exact required policy rule is understood.

    V1 policy rules are deterministic codes.  Content generation can prove the
    sealed exact-release-context rule; any future required rule is blocked until
    a reviewed evaluator is implemented.  The complete mandatory set is still
    recalculated at Release Gate and immediately before final publication.
    """

    snapshot_by_id = {
        str(item["id"]): str(item["manifest_sha256"])
        for item in context.policy_set_snapshot
    }
    queryset = PolicyVersion.objects.filter(pk__in=snapshot_by_id)
    if lock:
        queryset = queryset.select_for_update()
    policies = list(queryset.order_by("id"))
    if len(policies) != len(snapshot_by_id):
        raise ValidationError("已封存的适用规则版本不完整，不能生成内容。")

    checked: list[dict[str, Any]] = []
    for policy in policies:
        if snapshot_by_id.get(str(policy.pk)) != policy.manifest_sha256:
            raise ValidationError("已封存的适用规则哈希不匹配，不能生成内容。")
        rule_results: list[dict[str, Any]] = []
        for rule in policy.normalized_rules():
            supported = rule["rule_code"] in KNOWN_V1_RULES
            if rule["required"] and not supported:
                raise ValidationError(
                    {"policy": f"存在尚未实现的必选规则：{rule['rule_code']}"}
                )
            rule_results.append(
                {
                    "rule_code": rule["rule_code"],
                    "required": rule["required"],
                    "result": "PASS" if supported else "SKIPPED_OPTIONAL",
                }
            )
        checked.append(
            {
                "policy_version_id": str(policy.pk),
                "manifest_sha256": policy.manifest_sha256,
                "rules": rule_results,
            }
        )
    return checked


def _validate_output(
    *,
    output: Mapping[str, Any],
    context: TaskCompilationContext,
    platform: Platform,
    evidence: list[ExternalEvidenceItem],
) -> str:
    if output.get("platform") != platform.value:
        raise ValidationError("生成结果的平台与任务平台不一致。")
    if output.get("content_type") != PLATFORM_CONTENT_TYPES[platform.value]:
        raise ValidationError("生成结果的内容类型与平台模板不一致。")
    if output.get("language_code") != context.product_profile_version.language_code:
        raise ValidationError("生成结果语言与已封存的产品配置不一致。")

    exact_evidence_ids = {str(item.pk) for item in evidence}
    used_evidence_ids = [str(item) for item in output.get("evidence_ids", [])]
    if not used_evidence_ids or len(used_evidence_ids) != len(set(used_evidence_ids)):
        raise ValidationError("生成结果必须引用至少一条且不得重复的确切外部证据。")
    if not set(used_evidence_ids).issubset(exact_evidence_ids):
        raise ValidationError("生成结果引用了任务需求链之外或已经作废的证据。")

    allowed_claims, blocked_claim_wordings = _allowed_claims(context, platform)
    used_claims = {str(item) for item in output.get("claim_keys", [])}
    unknown_claims = sorted(used_claims - allowed_claims)
    if unknown_claims:
        raise ValidationError({"claim_keys": f"未获允许的产品主张：{', '.join(unknown_claims)}"})

    rendered = render_complete_content(dict(output))
    normalized = rendered.casefold()
    for expression in [*_profile_prohibited_expressions(context), *blocked_claim_wordings]:
        if expression and expression.casefold() in normalized:
            raise ValidationError({"content": f"内容包含禁用或不受支持的表达：{expression}"})
    return rendered


def _evidence_manifest(evidence: list[ExternalEvidenceItem]) -> list[dict[str, str]]:
    return [
        {
            "id": str(item.pk),
            "provenance_sha256": item.provenance_sha256,
            "source_id": str(item.source_id),
        }
        for item in sorted(evidence, key=lambda item: str(item.pk))
    ]


def validate_inline_content_evidence_manifest(
    *,
    asset_version: ContentAssetVersion,
    context: TaskCompilationContext | None = None,
    lock: bool = False,
) -> list[dict[str, str]]:
    """Require an inline version to reference the exact current evidence set.

    Content versions remain immutable historical facts, but an invalidation or
    expiry makes an older version ineligible for revision and submission. The
    current set is resolved only through the sealed TaskCompilationContext's
    approved External Demand assessment.
    """

    if asset_version.representation_kind != ContentAssetVersion.RepresentationKind.INLINE_TEXT:
        raise ValidationError("只有系统内正文需要核对外部需求证据。")
    context = context or _context_for_task(asset_version.content_asset.task_id)
    if context.task_id != asset_version.content_asset.task_id:
        raise ValidationError("内容版本与已封存的任务生成上下文不一致。")

    metadata = asset_version.metadata
    manifest = metadata.get("evidence_manifest")
    if not isinstance(manifest, list) or not manifest:
        raise ValidationError("内容版本缺少确切的外部需求证据清单，请重新生成内容。")
    if metadata.get("evidence_manifest_sha256") != canonical_sha256(manifest):
        raise ValidationError("内容版本的外部需求证据清单校验失败，请重新生成内容。")
    if (
        metadata.get("task_compilation_context_id") != str(context.pk)
        or metadata.get("task_compilation_input_sha256") != context.input_payload_sha256
    ):
        raise ValidationError("内容版本不是基于这项任务已封存的生成上下文，请重新生成内容。")

    current_manifest = _evidence_manifest(_valid_evidence(context, lock=lock))
    if manifest != current_manifest:
        raise ValidationError(
            "该内容版本引用的外部需求证据已经作废、过期或发生变化，请重新生成内容后再继续。"
        )
    return current_manifest


def _current_generated_content_or_none(
    *, task_id, context: TaskCompilationContext, lock: bool = False
) -> ContentAssetVersion | None:
    latest = _latest_generated_content(task_id)
    if latest is None:
        return None
    try:
        validate_inline_content_evidence_manifest(
            asset_version=latest,
            context=context,
            lock=lock,
        )
    except ValidationError:
        # Keep stale immutable history, but allow a clean replacement generated
        # from the exact current manifest.
        return None
    return latest


def _default_output(
    *, context: TaskCompilationContext, platform: Platform, evidence: list[ExternalEvidenceItem]
) -> dict[str, Any]:
    profile = context.product_profile_version
    opportunity = context.channel_plan.initiative.opportunity
    topic = opportunity.topic
    first = evidence[0]
    source_line = first.excerpt.strip() or first.title.strip() or opportunity.recommendation.strip()
    value = profile.core_value_proposition.strip()
    language = profile.language_code.lower()
    if language.startswith("zh"):
        hook = f"围绕“{topic.label}”，用户真正关心的是什么？"
        body = (
            f"外部信号显示：{source_line}\n\n"
            f"这次内容要解决的问题：{context.task.description or opportunity.recommendation}\n\n"
            f"可采用的产品角度：{value}\n\n"
            "请在发布前按品牌语气补充真实体验、画面或示例，并删除任何无法由证据支持的表述。"
        )
        cta = "如果你也遇到这个问题，先收藏并告诉我们你最想解决的具体场景。"
        notes = "离线测试草稿：人工核对事实、主张、平台长度和视觉素材后再送审。"
    else:
        hook = f"What are people really trying to solve around {topic.label}?"
        body = (
            f"External demand signal: {source_line}\n\n"
            f"The job of this content: {context.task.description or opportunity.recommendation}\n\n"
            f"Approved product angle to develop: {value}\n\n"
            "Before publishing, add a genuine example or visual and remove any wording that the evidence does not support."
        )
        cta = "Save this and tell us the exact situation you want help with next."
        notes = "Offline test draft. A human must verify facts, claims, platform length, and creative assets before review."
    return {
        "platform": platform.value,
        "content_type": PLATFORM_CONTENT_TYPES[platform.value],
        "title": context.task.title,
        "hook": hook,
        "body": body,
        "call_to_action": cta,
        "hashtags": ["PUKO", platform.value.replace("_", "")],
        "production_notes": notes,
        "claim_keys": [],
        "evidence_ids": [str(item.pk) for item in evidence],
        "language_code": profile.language_code,
    }


def _request(
    *, context: TaskCompilationContext, platform: Platform, evidence: list[ExternalEvidenceItem], command_id
) -> AIRequest:
    evidence_payload = [
        {
            "id": str(item.pk),
            "title": item.title,
            "excerpt": item.excerpt,
            "facts": item.facts,
            "external_url": item.external_url,
        }
        for item in evidence
    ]
    profile = context.product_profile_version
    input_payload = {
        "task": {"id": str(context.task_id), "title": context.task.title, "description": context.task.description},
        "platform": platform.value,
        "product_profile": {
            "id": str(profile.pk),
            "language_code": profile.language_code,
            "audience": profile.audience,
            "core_value_proposition": profile.core_value_proposition,
            "brand_voice": profile.brand_voice,
            "product_facts": profile.product_facts,
            "prohibited_expressions": profile.prohibited_expressions,
        },
        "evidence": evidence_payload,
        "claim_matrix_version_id": str(context.claim_matrix_version_id),
        "policy_set": context.policy_set_snapshot,
    }
    return AIRequest(
        messages=(
            AIMessage(
                role="system",
                content=(
                    "Create one complete, editable content draft. Use only the supplied External Demand evidence. "
                    "Do not invent product claims. Return the exact structured schema; this is a proposal for human review."
                ),
            ),
            AIMessage(role="user", content=json.dumps(input_payload, ensure_ascii=False, sort_keys=True)),
        ),
        output=StructuredOutputSpec(name="daily_content_draft_v1", schema=CONTENT_DRAFT_SCHEMA),
        operation_key=f"content-draft:{command_id}",
        max_output_tokens=8_000,
        temperature=0.2,
        metadata={
            "task_compilation_context_id": str(context.pk),
            "task_compilation_input_sha256": context.input_payload_sha256,
        },
    )


def generate_task_content_draft(
    *,
    task: Task,
    command_id: uuid.UUID,
    principal,
    acting_role: str,
    permission_grant,
    provider: AIProvider | None = None,
) -> ContentDraftResult:
    """Generate and persist one immutable inline content version.

    The default provider is deterministic and never performs network I/O.  A
    live provider must be explicitly injected by a separately authorized
    composition root.
    """

    command_id = uuid.UUID(str(command_id))
    _require_exact_edit_grant(
        task=task,
        principal=principal,
        acting_role=acting_role,
        permission_grant=permission_grant,
    )
    version_command_id = _subcommand_id(command_id, "content-version")
    replay = ContentAssetVersion.objects.filter(creation_command_id=version_command_id).first()
    if replay is not None:
        if (
            replay.content_asset.task_id != task.pk
            or replay.created_by_principal_id != principal.pk
            or replay.created_by_acting_role != acting_role
            or replay.created_under_grant_id != permission_grant.pk
        ):
            raise ValidationError("该 command_id 已被另一项内容生成操作使用。")
        return ContentDraftResult(asset_version=replay, created=False)
    context = _context_for_task(task.pk)
    context.full_clean()
    if _current_generated_content_or_none(task_id=task.pk, context=context) is not None:
        raise ValidationError("这项任务已经有完整内容草稿；请修改并另存为新版本，不要重复生成。")

    platform = _platform(context)
    evidence = _valid_evidence(context)
    _validate_generation_policy_set(context)
    default_output = _default_output(context=context, platform=platform, evidence=evidence)
    provider = provider or DryRunAIProvider(default_output, model="content-draft-offline-v1")
    request = _request(context=context, platform=platform, evidence=evidence, command_id=command_id)
    result = provider.generate(request)

    with transaction.atomic():
        locked_task = Task.objects.select_for_update().get(pk=task.pk)
        if locked_task.current_state != Task.State.IN_PROGRESS:
            raise ValidationError("只有正在执行的任务才能生成发布内容。")
        if locked_task.current_assignee_principal_id != principal.pk:
            raise PermissionDenied("ONLY_CURRENT_ASSIGNEE_CAN_GENERATE_CONTENT")
        context = _context_for_task(locked_task.pk)
        context.full_clean()
        if _current_generated_content_or_none(
            task_id=locked_task.pk,
            context=context,
            lock=True,
        ) is not None:
            raise ValidationError("这项任务已经有完整内容草稿；请修改并另存为新版本，不要重复生成。")
        platform = _platform(context)
        evidence = _valid_evidence(context, lock=True)
        checked_policies = _validate_generation_policy_set(context, lock=True)
        rendered = _validate_output(
            output=result.output,
            context=context,
            platform=platform,
            evidence=evidence,
        )
        evidence_manifest = _evidence_manifest(evidence)
        exact_output = dict(result.output)
        generation_rules = {
            "version": GENERATION_RULE_VERSION,
            "external_demand_only": True,
            "exclude_invalidated_evidence": True,
            "claim_matrix_version_id": str(context.claim_matrix_version_id),
            "policy_set_sha256": context.policy_set_sha256,
            "generation_policy_checks": checked_policies,
        }
        metadata = {
            "source": "generated-inline-content",
            "platform": platform.value,
            "content_type": exact_output["content_type"],
            "title": exact_output["title"],
            "hook": exact_output["hook"],
            "call_to_action": exact_output["call_to_action"],
            "hashtags": exact_output["hashtags"],
            "production_notes": exact_output["production_notes"],
            "claim_keys": exact_output["claim_keys"],
            "template_key": f"{platform.value.lower()}-publishable-content",
            "template_version": TEMPLATE_VERSION,
            "generation_rule_snapshot_sha256": canonical_sha256(generation_rules),
            "generation_rules": generation_rules,
            "evidence_manifest": evidence_manifest,
            "evidence_manifest_sha256": canonical_sha256(evidence_manifest),
            "task_compilation_context_id": str(context.pk),
            "task_compilation_input_sha256": context.input_payload_sha256,
            "generated_for_principal_id": str(principal.pk),
            "product_profile_version_id": str(context.product_profile_version_id),
            "objective_profile_version_id": str(context.objective_profile_version_id),
            "claim_matrix_version_id": str(context.claim_matrix_version_id),
            "evidence_library_version_id": str(context.evidence_library_version_id),
            "policy_set_sha256": context.policy_set_sha256,
            "provider": result.provider,
            "model": result.model,
            "execution_status": result.status.value,
            "request_fingerprint": result.request_fingerprint,
            "provider_request_id": result.provider_request_id or "",
            "usage": {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "total_tokens": result.usage.total_tokens,
                "estimated_cost_usd": result.usage.estimated_cost_usd,
            },
        }
        asset = ContentAsset.objects.select_for_update().filter(
            task=locked_task,
            asset_key="publishable-content",
        ).first()
        if asset is None:
            asset = ContentAsset.create_idempotent(
                task=locked_task,
                asset_key="publishable-content",
                title=f"{platform.value.title()} publishable content",
                asset_kind=ContentAsset.AssetKind.COPY,
                description="Complete editable content prepared from the sealed Daily Operations context.",
                command_id=_subcommand_id(command_id, "content-asset"),
                actor_principal=principal,
                acting_role=acting_role,
                permission_grant=permission_grant,
                recorded_by_principal=principal,
            )
        version = ContentAssetVersion.create_next(
            content_asset=asset,
            representation_kind=ContentAssetVersion.RepresentationKind.INLINE_TEXT,
            inline_content=rendered,
            mime_type="text/plain; charset=utf-8",
            metadata=metadata,
            command_id=version_command_id,
            actor_principal=principal,
            acting_role=acting_role,
            permission_grant=permission_grant,
            recorded_by_principal=principal,
        )
        return ContentDraftResult(asset_version=version, created=True)


@transaction.atomic
def revise_task_content_draft(
    *,
    task: Task,
    source_version: ContentAssetVersion,
    inline_content: str,
    command_id: uuid.UUID,
    principal,
    acting_role: str,
    permission_grant,
) -> ContentDraftResult:
    """Create a new immutable human-edited version; never rewrite AI output."""

    command_id = uuid.UUID(str(command_id))
    _require_exact_edit_grant(
        task=task,
        principal=principal,
        acting_role=acting_role,
        permission_grant=permission_grant,
    )
    normalized = inline_content.strip()
    replay = ContentAssetVersion.objects.filter(creation_command_id=command_id).first()
    if replay is not None:
        if (
            replay.content_asset_id != source_version.content_asset_id
            or replay.representation_kind != ContentAssetVersion.RepresentationKind.INLINE_TEXT
            or replay.inline_content != normalized
            or replay.created_by_principal_id != principal.pk
            or replay.created_by_acting_role != acting_role
            or replay.created_under_grant_id != permission_grant.pk
            or replay.metadata.get("supersedes_asset_version_id") != str(source_version.pk)
        ):
            raise ValidationError("该 command_id 已被另一项内容修改操作使用。")
        return ContentDraftResult(asset_version=replay, created=False)

    locked_task = Task.objects.select_for_update().get(pk=task.pk)
    if locked_task.current_state != Task.State.IN_PROGRESS:
        raise ValidationError("只有正在执行的任务才能保存新的内容版本。")
    if locked_task.current_assignee_principal_id != principal.pk:
        raise PermissionDenied("ONLY_CURRENT_ASSIGNEE_CAN_EDIT_CONTENT")
    if source_version.content_asset.task_id != locked_task.pk:
        raise ValidationError("要修改的内容版本不属于这项任务。")
    latest = source_version.content_asset.versions.order_by("-version_number").first()
    if latest is None or latest.pk != source_version.pk:
        raise ValidationError("这不是最新内容版本，请刷新后再修改。")
    if source_version.representation_kind != ContentAssetVersion.RepresentationKind.INLINE_TEXT:
        raise ValidationError("只有系统内正文可以在这里创建新版本。")
    if not normalized:
        raise ValidationError("发布内容不能为空。")

    context = _context_for_task(locked_task.pk)
    platform = _platform(context)
    current_manifest = validate_inline_content_evidence_manifest(
        asset_version=source_version,
        context=context,
        lock=True,
    )
    _, blocked_claim_wordings = _allowed_claims(context, platform)
    casefolded = normalized.casefold()
    for expression in [*_profile_prohibited_expressions(context), *blocked_claim_wordings]:
        if expression and expression.casefold() in casefolded:
            raise ValidationError({"inline_content": f"内容包含禁用或不受支持的表达：{expression}"})

    metadata = dict(source_version.metadata)
    metadata.update(
        {
            "source": "human-edited-inline-content",
            "supersedes_asset_version_id": str(source_version.pk),
            "edited_by_principal_id": str(principal.pk),
            "provider": "human",
            "model": "human-edit",
            "execution_status": AIExecutionStatus.SUCCEEDED.value,
            "evidence_manifest": current_manifest,
            "evidence_manifest_sha256": canonical_sha256(current_manifest),
        }
    )
    version = ContentAssetVersion.create_next(
        content_asset=source_version.content_asset,
        representation_kind=ContentAssetVersion.RepresentationKind.INLINE_TEXT,
        inline_content=normalized,
        mime_type="text/plain; charset=utf-8",
        metadata=metadata,
        command_id=command_id,
        actor_principal=principal,
        acting_role=acting_role,
        permission_grant=permission_grant,
        recorded_by_principal=principal,
    )
    return ContentDraftResult(asset_version=version, created=True)
