from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from integrations.connectors.types import Platform

from accounts.authorization import resolve_authorization
from accounts.models import PermissionGrant, Principal
from products.models import (
    ClaimMatrixItem,
    ClaimMatrixVersion,
    ControlledEvidenceItemVersion,
    EvidenceLibraryItem,
    EvidenceLibraryVersion,
    ObjectiveProfileVersion,
    Product,
    ProductClaimVersion,
    ProductProfileAssetLink,
    ProductProfileVersion,
)
from releasegate.models import (
    AccountEnvironmentBinding,
    CapabilityState,
    ChannelAccount,
    RuntimeEnvironment,
)


@dataclass(frozen=True, slots=True)
class CurrentAccountEnvironmentChange:
    binding: AccountEnvironmentBinding
    created_target_version: bool
    revoked_count: int


def require_product_configuration(actor: Principal, product: Product) -> PermissionGrant:
    decision = resolve_authorization(
        principal=actor,
        acting_role=actor.role,
        action=PermissionGrant.Action.EDIT,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=product,
    )
    if (
        not decision.allowed
        or decision.grant is None
        or decision.grant.scope_kind != PermissionGrant.ScopeKind.PRODUCT
        or decision.grant.product_id != product.pk
    ):
        raise PermissionDenied("需要这个产品当前有效的精确编辑权限。")
    return decision.grant


def require_runtime_configuration(actor: Principal) -> PermissionGrant:
    decision = resolve_authorization(
        principal=actor,
        acting_role=actor.role,
        action=PermissionGrant.Action.MANAGE_ACCOUNT,
        scope_kind=PermissionGrant.ScopeKind.GLOBAL,
    )
    if (
        not decision.allowed
        or decision.grant is None
        or decision.grant.scope_kind != PermissionGrant.ScopeKind.GLOBAL
    ):
        raise PermissionDenied("需要当前有效的全局账号与环境管理权限。")
    if actor.role == Principal.Role.OPERATOR:
        raise PermissionDenied("执行人员不能配置账号或运行环境。")
    return decision.grant


def _next(model, filters: dict, field: str = "version_number") -> int:
    value = model.objects.filter(**filters).aggregate(value=Max(field))["value"]
    return (value or 0) + 1


def _lock_product(product: Product) -> Product:
    return Product.objects.select_for_update().get(pk=product.pk)


@transaction.atomic
def create_objective_profile(*, actor: Principal, product: Product, **values) -> ObjectiveProfileVersion:
    product = _lock_product(product)
    require_product_configuration(actor, product)
    key = values.pop("objective_key").strip()
    version = ObjectiveProfileVersion.objects.create(
        objective_key=key,
        version_number=_next(ObjectiveProfileVersion, {"objective_key": key}),
        created_by_principal=actor,
        **values,
    )
    version.seal(principal=actor)
    return version


@transaction.atomic
def create_claim_matrix(*, actor: Principal, product: Product, claim: dict) -> ClaimMatrixVersion:
    product = _lock_product(product)
    require_product_configuration(actor, product)
    claim = dict(claim)
    claim_key = claim.pop("claim_key").strip()
    claim_version = ProductClaimVersion.objects.create(
        product=product,
        claim_key=claim_key,
        version_number=_next(ProductClaimVersion, {"product": product, "claim_key": claim_key}),
        created_by_principal=actor,
        **claim,
    )
    matrix = ClaimMatrixVersion.objects.create(
        product=product,
        version_number=_next(ClaimMatrixVersion, {"product": product}),
        market_code=product.market_code,
        language_code=product.language_code,
        created_by_principal=actor,
    )
    ClaimMatrixItem.objects.create(
        claim_matrix_version=matrix,
        product_claim_version=claim_version,
        created_by_principal=actor,
    )
    matrix.seal(principal=actor)
    return matrix


@transaction.atomic
def create_evidence_library(*, actor: Principal, product: Product, evidence: dict) -> EvidenceLibraryVersion:
    product = _lock_product(product)
    require_product_configuration(actor, product)
    evidence = dict(evidence)
    evidence_key = evidence.pop("evidence_key").strip()
    item = ControlledEvidenceItemVersion.objects.create(
        product=product,
        evidence_key=evidence_key,
        version_number=_next(
            ControlledEvidenceItemVersion,
            {"product": product, "evidence_key": evidence_key},
        ),
        created_by_principal=actor,
        **evidence,
    )
    library = EvidenceLibraryVersion.objects.create(
        product=product,
        version_number=_next(EvidenceLibraryVersion, {"product": product}),
        market_code=product.market_code,
        language_code=product.language_code,
        created_by_principal=actor,
    )
    EvidenceLibraryItem.objects.create(
        evidence_library_version=library,
        controlled_evidence_item_version=item,
        created_by_principal=actor,
    )
    library.seal(principal=actor)
    return library


@transaction.atomic
def create_product_profile(*, actor: Principal, product: Product, **values) -> ProductProfileVersion:
    product = _lock_product(product)
    require_product_configuration(actor, product)
    objective = values["objective_profile_version"]
    matrix = values["claim_matrix_version"]
    library = values["evidence_library_version"]
    if not objective.is_sealed or not matrix.is_sealed or not library.is_sealed:
        raise ValidationError("产品档案只能引用已经封存的目标、声明和证据版本。")
    if matrix.product_id != product.pk or library.product_id != product.pk:
        raise ValidationError("声明矩阵和证据库必须属于当前产品。")
    return ProductProfileVersion.objects.create(
        product=product,
        version_number=_next(ProductProfileVersion, {"product": product}),
        market_code=product.market_code,
        language_code=product.language_code,
        objective_profile_key=objective.objective_key,
        created_by_principal=actor,
        **values,
    )


@transaction.atomic
def add_profile_evidence_link(
    *, actor: Principal, profile: ProductProfileVersion, evidence: ControlledEvidenceItemVersion, asset_kind: str
) -> ProductProfileAssetLink:
    require_product_configuration(actor, profile.product)
    if profile.is_sealed:
        raise ValidationError("已封存的产品档案不能再加链接，请创建新版本。")
    if evidence.product_id != profile.product_id:
        raise ValidationError("只能添加当前产品的受控证据链接。")
    return ProductProfileAssetLink.objects.create(
        product_profile_version=profile,
        asset_kind=asset_kind,
        controlled_evidence_item_version=evidence,
        created_by_principal=actor,
    )


@transaction.atomic
def seal_and_activate_profile(*, actor: Principal, profile: ProductProfileVersion) -> ProductProfileVersion:
    product = Product.objects.select_for_update().get(pk=profile.product_id)
    profile = ProductProfileVersion.objects.select_related(
        "objective_profile_version", "claim_matrix_version", "evidence_library_version"
    ).get(pk=profile.pk)
    require_product_configuration(actor, product)
    profile.seal(actor)
    product.current_profile_version = profile
    product.updated_by_principal = actor
    product.full_clean()
    product.save(update_fields=["current_profile_version", "updated_by_principal", "updated_at"])
    return profile


@transaction.atomic
def create_channel_account(*, actor: Principal, **values) -> ChannelAccount:
    require_runtime_configuration(actor)
    if values.get("platform_code") not in {platform.value for platform in Platform}:
        raise ValidationError("V1 渠道账号必须属于已确认的七个平台之一。")
    if values.get("status", ChannelAccount.Status.ACTIVE) != ChannelAccount.Status.ACTIVE:
        raise ValidationError("新增渠道账号必须从“使用中”状态开始。暂停或停用需走受控状态流程。")
    return ChannelAccount.objects.create(
        created_by_principal=actor,
        updated_by_principal=actor,
        **values,
    )


@transaction.atomic
def create_runtime_environment(*, actor: Principal, **values) -> RuntimeEnvironment:
    require_runtime_configuration(actor)
    if values.get("status", RuntimeEnvironment.Status.ACTIVE) != RuntimeEnvironment.Status.ACTIVE:
        raise ValidationError("新增使用场景必须从“使用中”状态开始。锁定或停用需走受控状态流程。")
    environment_code = str(values.get("environment_code") or "").strip().lower()
    if (
        environment_code.startswith("local-")
        and values.get("environment_type") == RuntimeEnvironment.EnvironmentType.PRODUCTION
    ):
        raise ValidationError("以 local- 开头的使用场景只能是测试环境，不能标记为正式环境。")
    return RuntimeEnvironment.objects.create(
        created_by_principal=actor,
        updated_by_principal=actor,
        **values,
    )


@transaction.atomic
def create_binding_version(
    *, actor: Principal, channel_account: ChannelAccount, runtime_environment: RuntimeEnvironment,
    identity_reference: str, valid_until=None,
) -> AccountEnvironmentBinding:
    require_runtime_configuration(actor)
    # The account row serializes the first and subsequent versions alike.
    channel_account = ChannelAccount.objects.select_for_update().get(pk=channel_account.pk)
    runtime_environment = RuntimeEnvironment.objects.get(pk=runtime_environment.pk)
    if channel_account.status != ChannelAccount.Status.ACTIVE:
        raise ValidationError("只能给当前有效的渠道账号创建绑定版本。")
    if runtime_environment.status != RuntimeEnvironment.Status.ACTIVE:
        raise ValidationError("只能绑定当前有效的运行环境。")
    latest = AccountEnvironmentBinding.objects.select_for_update().filter(
        channel_account=channel_account,
        runtime_environment=runtime_environment,
    ).order_by("-binding_version").first()
    return AccountEnvironmentBinding.objects.create(
        channel_account=channel_account,
        runtime_environment=runtime_environment,
        binding_version=(latest.binding_version + 1) if latest else 1,
        identity_reference=identity_reference,
        valid_from=timezone.now(),
        valid_until=valid_until,
        supersedes=latest,
        created_by_principal=actor,
        recorded_by_principal=actor,
    )


def _binding_is_effective(binding: AccountEnvironmentBinding | None, *, at) -> bool:
    return bool(
        binding
        and binding.status == AccountEnvironmentBinding.Status.ACTIVE
        and binding.valid_from <= at
        and (binding.valid_until is None or binding.valid_until > at)
    )


@transaction.atomic
def set_current_account_environment(
    *,
    actor: Principal,
    channel_account: ChannelAccount,
    runtime_environment: RuntimeEnvironment,
    identity_reference: str = "",
) -> CurrentAccountEnvironmentChange:
    """Atomically keep exactly one selected usage context for an account.

    Existing facts are never updated or deleted. Other latest ACTIVE pairs get
    a new REVOKED version, while the selected pair is preserved when it is
    already current with the same reference. The account-row lock serializes
    competing switches across every environment pair for that account.
    """

    require_runtime_configuration(actor)
    channel_account = ChannelAccount.objects.select_for_update().get(pk=channel_account.pk)
    runtime_environment = RuntimeEnvironment.objects.get(pk=runtime_environment.pk)
    if channel_account.platform_code not in {platform.value for platform in Platform}:
        raise ValidationError("只能为系统支持的平台账号设置使用场景。")
    if channel_account.status != ChannelAccount.Status.ACTIVE:
        raise ValidationError("只能为当前有效的渠道账号设置使用场景。")
    if runtime_environment.status != RuntimeEnvironment.Status.ACTIVE:
        raise ValidationError("只能选择当前有效的使用场景。")

    rows = list(
        AccountEnvironmentBinding.objects.select_for_update()
        .filter(channel_account=channel_account)
        .order_by("runtime_environment_id", "-binding_version", "-created_at", "-id")
    )
    latest_by_environment: dict[object, AccountEnvironmentBinding] = {}
    for binding in rows:
        latest_by_environment.setdefault(binding.runtime_environment_id, binding)

    now = timezone.now()
    target_latest = latest_by_environment.get(runtime_environment.pk)
    target_is_effective = _binding_is_effective(target_latest, at=now)
    reference = str(identity_reference or "").strip()
    if not reference:
        if not target_is_effective:
            raise ValidationError("新增或重新启用连接时，必须填写连接标识名称。")
        reference = target_latest.identity_reference
    if len(reference) > 255:
        raise ValidationError("连接标识名称不能超过 255 个字符。")

    revoked_count = 0
    for environment_id, latest in latest_by_environment.items():
        if environment_id == runtime_environment.pk or latest.status != AccountEnvironmentBinding.Status.ACTIVE:
            continue
        AccountEnvironmentBinding.objects.create(
            channel_account=channel_account,
            runtime_environment_id=environment_id,
            binding_version=latest.binding_version + 1,
            status=AccountEnvironmentBinding.Status.REVOKED,
            identity_reference=latest.identity_reference,
            valid_from=now,
            valid_until=None,
            supersedes=latest,
            created_by_principal=actor,
            recorded_by_principal=actor,
        )
        revoked_count += 1

    if target_is_effective and target_latest.identity_reference == reference:
        return CurrentAccountEnvironmentChange(
            binding=target_latest,
            created_target_version=False,
            revoked_count=revoked_count,
        )

    target = AccountEnvironmentBinding.objects.create(
        channel_account=channel_account,
        runtime_environment=runtime_environment,
        binding_version=(target_latest.binding_version + 1) if target_latest else 1,
        status=AccountEnvironmentBinding.Status.ACTIVE,
        identity_reference=reference,
        valid_from=now,
        valid_until=None,
        supersedes=target_latest,
        created_by_principal=actor,
        recorded_by_principal=actor,
    )
    return CurrentAccountEnvironmentChange(
        binding=target,
        created_target_version=True,
        revoked_count=revoked_count,
    )


@transaction.atomic
def create_capability_version(
    *, actor: Principal, binding: AccountEnvironmentBinding, capability_code: str,
    state: str, reason: str = "", expires_at=None,
) -> CapabilityState:
    require_runtime_configuration(actor)
    # Lock the stable binding so an empty capability history is also serialized.
    binding = AccountEnvironmentBinding.objects.select_for_update().get(pk=binding.pk)
    if not binding.is_current_at():
        raise ValidationError("只能给当前有效的账号环境绑定追加能力状态。")
    if (
        binding.channel_account.status != ChannelAccount.Status.ACTIVE
        or binding.runtime_environment.status != RuntimeEnvironment.Status.ACTIVE
    ):
        raise ValidationError("账号或运行环境当前不可用，不能打开能力。")
    latest = CapabilityState.objects.select_for_update().filter(
        account_environment_binding=binding,
        capability_code=capability_code,
    ).order_by("-state_version").first()
    return CapabilityState.objects.create(
        account_environment_binding=binding,
        capability_code=capability_code,
        state_version=(latest.state_version + 1) if latest else 1,
        state=state,
        effective_from=timezone.now(),
        expires_at=expires_at,
        reason=reason,
        supersedes=latest,
        created_by_principal=actor,
        recorded_by_principal=actor,
    )
