from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.authorization import resolve_authorization
from accounts.models import PermissionGrant, Principal
from products.models import (
    ClaimMatrixItem,
    ClaimMatrixVersion,
    EvidenceLibraryVersion,
    ObjectiveProfileVersion,
    Product,
    ProductClaimVersion,
    ProductProfilePolicyLink,
    ProductProfileVersion,
)
from releasegate.models import (
    AccountEnvironmentBinding,
    CapabilityState,
    ChannelAccount,
    PolicyDefinition,
    PolicyVersion,
    RuntimeEnvironment,
)
from workflow.models import TaskContractPolicyLink, TaskContractVersion


PROFILE_VALUES = {
    "market_code": "US",
    "language_code": "en",
    "audience": {"business_mode": "B2C", "primary_market": "US", "primary_language": "English"},
    "core_value_proposition": "Evidence-informed supplements and practical wellness education.",
    "brand_voice": {"tone": ["clear", "measured", "helpful", "evidence-informed"]},
    "product_facts": {"pilot": "PUKO", "objective": "visibility_and_seo_proof"},
    "prohibited_expressions": ["cure", "treat", "prevent disease", "guaranteed result"],
    "objective_profile_key": "VISIBILITY_AND_SEO_PROOF",
}
POLICY_RULES = [{"rule_code": "exact_release_context", "required": True}]
CONTRACT_VALUES = {
    "title": "PUKO manual content contract",
    "dor_criteria": [{"key": "inputs_complete", "required": True}],
    "dod_criteria": [{"key": "primary_deliverable", "required": True}],
    "release_gate_criteria": [{"key": "exact_release_context", "required": True}],
    "success_criteria": [{"key": "manual_publication_proof", "required": True}],
}


@dataclass(frozen=True)
class HumanSpec:
    key: str
    username: str
    password_env: str
    role: str
    display_name: str
    group_name: str
    fallback_password_env: str | None = None


# These are bootstrap templates, not runtime role shortcuts.  Every capability
# is materialized as an explicit, scoped PermissionGrant; authorization still
# validates the Principal's persisted acting role and resolves the Grant.
ROLE_PRODUCT_GRANT_TEMPLATES = {
    Principal.Role.OWNER: (
        PermissionGrant.Action.VIEW,
        PermissionGrant.Action.EDIT,
        PermissionGrant.Action.COLLECT_READ_ONLY,
        PermissionGrant.Action.CREATE_TASK,
        PermissionGrant.Action.ASSIGN_TASK,
        PermissionGrant.Action.CANCEL_TASK,
        PermissionGrant.Action.COMPLETE_TASK,
        PermissionGrant.Action.REVIEW,
    ),
    Principal.Role.OPERATIONS_ADMIN: (
        PermissionGrant.Action.VIEW,
        PermissionGrant.Action.EDIT,
        PermissionGrant.Action.COLLECT_READ_ONLY,
        PermissionGrant.Action.CREATE_TASK,
        PermissionGrant.Action.ASSIGN_TASK,
        PermissionGrant.Action.CANCEL_TASK,
        PermissionGrant.Action.COMPLETE_TASK,
        PermissionGrant.Action.REVIEW,
    ),
    Principal.Role.OPERATOR: (
        PermissionGrant.Action.VIEW,
        PermissionGrant.Action.EDIT,
        PermissionGrant.Action.COLLECT_READ_ONLY,
    ),
}
DAILY_COLLECTION_PLATFORMS = (
    "PINTEREST",
    "QUORA",
    "TIKTOK",
    "SHOPIFY",
    "GOOGLE_SEARCH",
    "GOOGLE_SEARCH_CONSOLE",
    "GOOGLE_ANALYTICS_4",
)


class Command(BaseCommand):
    help = "Create an idempotent local PUKO Dogfood base context without embedding credentials or Tasks."

    def add_arguments(self, parser):
        parser.add_argument("--owner-username", default="owner")
        parser.add_argument("--admin-username")
        parser.add_argument("--operator-username")
        # Legacy capability-identity options remain available so existing
        # local databases and acceptance fixtures can be replayed unchanged.
        parser.add_argument("--reviewer-username")
        parser.add_argument("--publisher-username")
        parser.add_argument("--rule-evaluator-username")
        parser.add_argument(
            "--full-demo",
            action="store_true",
            help=(
                "Request owner, admin, operator, and a rule evaluator. Each canonical staff identity receives "
                "its own explicit, bounded account-scoped Publisher capability. "
                "New human identities require their distinct BOOTSTRAP_*_PASSWORD environment variables."
            ),
        )
        parser.add_argument(
            "--strict-separation-demo",
            action="store_true",
            help=(
                "Use the legacy four-human acceptance fixture with separate reviewer and publisher "
                "capability identities. This preserves stronger duty separation without adding roles."
            ),
        )

    def _requested_identities(self, options) -> tuple[list[HumanSpec], str | None]:
        defaults = {
            "admin_username": "admin",
            "operator_username": "operator",
            "reviewer_username": "reviewer",
            "publisher_username": "publisher",
            "rule_evaluator_username": "rule-evaluator",
        }
        if options["strict_separation_demo"]:
            for key in ("operator_username", "reviewer_username", "publisher_username", "rule_evaluator_username"):
                options[key] = options[key] or defaults[key]
        elif options["full_demo"]:
            for key in ("admin_username", "operator_username", "rule_evaluator_username"):
                options[key] = options[key] or defaults[key]

        humans = [
            HumanSpec(
                key="owner",
                username=options["owner_username"],
                password_env="BOOTSTRAP_OWNER_PASSWORD",
                role=Principal.Role.OWNER,
                display_name="PUKO Owner",
                group_name="Owner",
            )
        ]
        optional = (
            ("admin", "admin_username", "BOOTSTRAP_ADMIN_PASSWORD", Principal.Role.OPERATIONS_ADMIN,
             "PUKO Operations Admin", "Operations Admin", "BOOTSTRAP_REVIEWER_PASSWORD"),
            ("operator", "operator_username", "BOOTSTRAP_OPERATOR_PASSWORD", Principal.Role.OPERATOR,
             "PUKO Operator", "Operator", None),
            ("reviewer", "reviewer_username", "BOOTSTRAP_REVIEWER_PASSWORD", Principal.Role.OPERATIONS_ADMIN,
             "PUKO Review-capable Operations Admin", "Operations Admin", None),
            ("publisher", "publisher_username", "BOOTSTRAP_PUBLISHER_PASSWORD", Principal.Role.OPERATOR,
             "PUKO Publishing Operator", "Operator", None),
        )
        for key, option, password_env, role, display_name, group_name, fallback_password_env in optional:
            username = options.get(option)
            if username:
                humans.append(
                    HumanSpec(
                        key,
                        username,
                        password_env,
                        role,
                        display_name,
                        group_name,
                        fallback_password_env,
                    )
                )
        return humans, options.get("rule_evaluator_username")

    @staticmethod
    def _password_from_environment(spec: HumanSpec) -> tuple[str, str] | None:
        for name in (spec.password_env, spec.fallback_password_env):
            if name and os.getenv(name):
                return name, os.environ[name]
        return None

    def _preflight_identities(self, humans: list[HumanSpec], rule_evaluator_username: str | None) -> None:
        usernames = [spec.username for spec in humans]
        if rule_evaluator_username:
            usernames.append(rule_evaluator_username)
        if any(not username.strip() for username in usernames) or len(usernames) != len(set(usernames)):
            raise CommandError("Every requested Dogfood identity must have a distinct, non-empty username.")

        new_passwords: list[tuple[str, str]] = []
        for spec in humans:
            principal = Principal.objects.filter(username=spec.username).first()
            if principal is not None:
                if principal.principal_type != Principal.PrincipalType.HUMAN_USER or not principal.can_authenticate:
                    raise CommandError(f"Existing {spec.key} account '{spec.username}' is not an active human Principal.")
                if principal.auth_provider == "internal" and not principal.has_usable_password():
                    raise CommandError(
                        f"Existing internal {spec.key} account '{spec.username}' has no usable password; "
                        "bootstrap will not reset credentials implicitly."
                    )
                if spec.key == "owner" and principal.is_superuser:
                    continue
                if principal.role != spec.role:
                    raise CommandError(
                        f"Existing {spec.key} account '{spec.username}' has role {principal.role}, expected {spec.role}."
                    )
                continue
            password_entry = self._password_from_environment(spec)
            if password_entry is None:
                accepted_names = " or ".join(
                    name for name in (spec.password_env, spec.fallback_password_env) if name
                )
                raise CommandError(
                    f"New {spec.key} account '{spec.username}' requires {accepted_names}; "
                    "no human account was created."
                )
            password_env, password = password_entry
            candidate = Principal(
                username=spec.username,
                display_name=spec.display_name,
                principal_type=Principal.PrincipalType.HUMAN_USER,
                role=spec.role,
            )
            try:
                validate_password(password, user=candidate)
            except ValidationError as error:
                raise CommandError(
                    f"{password_env} does not satisfy the configured password policy: "
                    f"{' '.join(error.messages)}"
                ) from error
            new_passwords.append((password_env, password))

        password_values = [value for _, value in new_passwords]
        if len(password_values) != len(set(password_values)):
            names = ", ".join(name for name, _ in new_passwords)
            raise CommandError(f"New human identities must use distinct password values from: {names}.")

        if rule_evaluator_username:
            evaluator = Principal.objects.filter(username=rule_evaluator_username).first()
            if evaluator is not None and (
                evaluator.principal_type not in {
                    Principal.PrincipalType.SERVICE_ACCOUNT,
                    Principal.PrincipalType.SYSTEM,
                }
                or evaluator.principal_status != Principal.PrincipalStatus.ACTIVE
                or not evaluator.is_active
            ):
                raise CommandError(
                    f"Existing rule evaluator '{rule_evaluator_username}' is not an active service Principal."
                )

    def _ensure_human(self, spec: HumanSpec) -> Principal:
        principal = Principal.objects.filter(username=spec.username).first()
        if principal is None:
            password_entry = self._password_from_environment(spec)
            if password_entry is None:
                raise CommandError(f"Password environment disappeared before creating '{spec.username}'.")
            _, password = password_entry
            # Growth OS authority is expressed by exact PermissionGrants, not
            # Django's unrestricted admin bypass.  Even the business Owner is
            # therefore a normal authenticated Principal.
            principal = Principal.objects.create_user(
                username=spec.username,
                password=password,
                display_name=spec.display_name,
                principal_type=Principal.PrincipalType.HUMAN_USER,
                role=spec.role,
            )
        elif spec.key == "owner" and principal.is_superuser and principal.role != Principal.Role.OWNER:
            principal.role = Principal.Role.OWNER
            principal.save(update_fields=["role"])
        group, _ = Group.objects.get_or_create(name=spec.group_name)
        principal.groups.add(group)
        return principal

    def _ensure_rule_evaluator(self, username: str | None) -> Principal | None:
        if not username:
            return None
        principal = Principal.objects.filter(username=username).first()
        if principal is None:
            principal = Principal(
                username=username,
                display_name="PUKO Rule Evaluator",
                principal_type=Principal.PrincipalType.SERVICE_ACCOUNT,
                role=Principal.Role.OPERATOR,
            )
            principal.set_unusable_password()
            principal.save()
        return principal

    def _ensure_profile_context(self, product: Product, owner: Principal):
        objective_values = {
            "primary_objectives": ["EXPOSURE", "SEO", "GEO", "ACCOUNT_VISIT"],
            "secondary_objectives": ["CONTENT_QUALITY", "AUDIENCE_FIT"],
            "retained_metrics": ["PRODUCT_VIEW", "ADD_TO_CART", "CHECKOUT", "PURCHASE", "REVENUE"],
            "priority_rules": {"daily_operations": "EXTERNAL_DEMAND_FIRST"},
            "strategy_boundaries": {"commercial_outcomes_are_not_demand": True},
        }
        objective, created = ObjectiveProfileVersion.objects.get_or_create(
            objective_key=PROFILE_VALUES["objective_profile_key"],
            version_number=1,
            defaults={"created_by_principal": owner, **objective_values},
        )
        if not created and any(getattr(objective, key) != value for key, value in objective_values.items()):
            raise CommandError("Existing visibility ObjectiveProfileVersion differs from the Daily Operations seed.")
        if not objective.is_sealed:
            objective.seal(principal=owner)

        claim = ProductClaimVersion.objects.filter(
            product=product,
            claim_key="NO_DISEASE_TREATMENT_CLAIM",
            version_number=1,
        ).first()
        if claim is None:
            claim = ProductClaimVersion.objects.create(
                product=product,
                claim_key="NO_DISEASE_TREATMENT_CLAIM",
                version_number=1,
                claim_type=ProductClaimVersion.ClaimType.PROHIBITED,
                market_code=product.market_code,
                platform_code="",
                evidence_level=ProductClaimVersion.EvidenceLevel.UNSUPPORTED,
                wording="Do not claim that the product cures, treats, or prevents disease.",
                created_by_principal=owner,
            )
        elif (
            claim.claim_type != ProductClaimVersion.ClaimType.PROHIBITED
            or claim.market_code != product.market_code
            or claim.platform_code
            or claim.evidence_level != ProductClaimVersion.EvidenceLevel.UNSUPPORTED
        ):
            raise CommandError("Existing prohibited ProductClaimVersion differs from the Daily Operations seed.")
        matrix, matrix_created = ClaimMatrixVersion.objects.get_or_create(
            product=product,
            version_number=1,
            defaults={
                "market_code": product.market_code,
                "language_code": product.language_code,
                "created_by_principal": owner,
            },
        )
        if not matrix_created and (
            matrix.market_code != product.market_code or matrix.language_code != product.language_code
        ):
            raise CommandError("Existing ClaimMatrixVersion differs from the Product market/language.")
        if not matrix.is_sealed:
            ClaimMatrixItem.objects.get_or_create(
                claim_matrix_version=matrix,
                product_claim_version=claim,
                defaults={"created_by_principal": owner},
            )
            matrix.seal(principal=owner)
        if not matrix.items.filter(product_claim_version=claim).exists():
            raise CommandError("The sealed ClaimMatrixVersion is missing the prohibited-claim item.")

        library, library_created = EvidenceLibraryVersion.objects.get_or_create(
            product=product,
            version_number=1,
            defaults={
                "market_code": product.market_code,
                "language_code": product.language_code,
                "created_by_principal": owner,
            },
        )
        if not library_created and (
            library.market_code != product.market_code or library.language_code != product.language_code
        ):
            raise CommandError("Existing EvidenceLibraryVersion differs from the Product market/language.")
        if not library.is_sealed:
            # Link-first V1 deliberately permits an empty controlled library;
            # evidence is added later as immutable external references.
            library.seal(principal=owner)
        return objective, matrix, library

    def _ensure_profile(
        self,
        product: Product,
        owner: Principal,
        policy_version: PolicyVersion,
    ) -> ProductProfileVersion:
        objective, matrix, library = self._ensure_profile_context(product, owner)
        profile = (
            ProductProfileVersion.objects.filter(
                product=product,
                objective_profile_version=objective,
                claim_matrix_version=matrix,
                evidence_library_version=library,
            )
            .order_by("-version_number")
            .first()
        )
        if profile is None:
            latest_number = (
                ProductProfileVersion.objects.filter(product=product)
                .order_by("-version_number")
                .values_list("version_number", flat=True)
                .first()
                or 0
            )
            profile = ProductProfileVersion.objects.create(
                product=product,
                version_number=latest_number + 1,
                objective_profile_version=objective,
                claim_matrix_version=matrix,
                evidence_library_version=library,
                created_by_principal=owner,
                **PROFILE_VALUES,
            )
        else:
            mismatched = [name for name, value in PROFILE_VALUES.items() if getattr(profile, name) != value]
            if mismatched:
                raise CommandError(
                    f"Existing exact PUKO profile v{profile.version_number} differs in: {', '.join(mismatched)}."
                )
        if not profile.is_sealed:
            ProductProfilePolicyLink.objects.get_or_create(
                product_profile_version=profile,
                policy_version=policy_version,
                policy_role=ProductProfilePolicyLink.PolicyRole.APPLICABLE,
                defaults={"created_by_principal": owner},
            )
            profile.seal(owner)
        elif not profile.policy_links.filter(
            policy_version=policy_version,
            policy_role=ProductProfilePolicyLink.PolicyRole.APPLICABLE,
        ).exists():
            raise CommandError("The exact sealed Product Profile is missing its required Policy link.")
        return profile

    def _ensure_policy(self, owner: Principal) -> PolicyVersion:
        definition, created = PolicyDefinition.objects.get_or_create(
            policy_code="V1_RELEASE_INTEGRITY",
            defaults={
                "name": "V1 release integrity",
                "description": "Fail-closed exact-context checks for manual Dogfood publication.",
                "is_mandatory": True,
                "status": PolicyDefinition.Status.ACTIVE,
                "created_by_principal": owner,
                "updated_by_principal": owner,
            },
        )
        if not created and (
            not definition.is_mandatory or definition.status != PolicyDefinition.Status.ACTIVE
        ):
            raise CommandError("Existing V1_RELEASE_INTEGRITY policy is not active and mandatory.")
        version = PolicyVersion.objects.filter(policy_definition=definition, version_number=1).first()
        if version is None:
            version = PolicyVersion.objects.create(
                policy_definition=definition,
                version_number=1,
                rules=POLICY_RULES,
                created_by_principal=owner,
                recorded_by_principal=owner,
            )
        elif version.rules != POLICY_RULES or version.effective_until is not None:
            raise CommandError("Existing V1_RELEASE_INTEGRITY policy v1 differs from the Dogfood seed.")
        return version

    def _ensure_contract(
        self, profile: ProductProfileVersion, policy_version: PolicyVersion, owner: Principal
    ) -> TaskContractVersion:
        contract = TaskContractVersion.objects.filter(
            product_profile_version=profile,
            version_number=1,
        ).first()
        if contract is None:
            contract = TaskContractVersion.objects.create(
                product_profile_version=profile,
                version_number=1,
                sealed_at=timezone.now(),
                created_by_principal=owner,
                **CONTRACT_VALUES,
            )
        else:
            mismatched = [name for name, value in CONTRACT_VALUES.items() if getattr(contract, name) != value]
            if mismatched:
                raise CommandError(f"Existing PUKO contract v1 differs in: {', '.join(mismatched)}.")
        link, _ = TaskContractPolicyLink.objects.get_or_create(
            task_contract_version=contract,
            policy_version=policy_version,
            defaults={"required": True, "created_by_principal": owner},
        )
        if not link.required:
            raise CommandError("Existing PUKO contract policy link is not marked required.")
        return contract

    def _ensure_release_context(self, owner: Principal):
        channel, created = ChannelAccount.objects.get_or_create(
            account_code="puko-us",
            defaults={
                "platform_code": "TIKTOK",
                "external_account_ref": "local:puko-us",
                "display_name": "PUKO US Local Dogfood",
                "status": ChannelAccount.Status.ACTIVE,
                "created_by_principal": owner,
                "updated_by_principal": owner,
            },
        )
        if not created and (
            channel.platform_code != "TIKTOK"
            or channel.external_account_ref != "local:puko-us"
            or channel.status != ChannelAccount.Status.ACTIVE
        ):
            raise CommandError("Existing puko-us ChannelAccount conflicts with the local Dogfood seed.")

        environment, created = RuntimeEnvironment.objects.get_or_create(
            environment_code="local-dogfood",
            defaults={
                "environment_type": RuntimeEnvironment.EnvironmentType.STAGING,
                "identity_namespace": "local-dogfood-identities",
                "database_namespace": "local-dogfood-database",
                "object_storage_namespace": "local-dogfood-objects",
                "status": RuntimeEnvironment.Status.ACTIVE,
                "created_by_principal": owner,
                "updated_by_principal": owner,
            },
        )
        if not created and (
            environment.environment_type != RuntimeEnvironment.EnvironmentType.STAGING
            or environment.status != RuntimeEnvironment.Status.ACTIVE
        ):
            raise CommandError("Existing local-dogfood RuntimeEnvironment conflicts with the seed.")

        binding = AccountEnvironmentBinding.objects.filter(
            channel_account=channel,
            runtime_environment=environment,
            binding_version=1,
        ).first()
        if binding is None:
            binding = AccountEnvironmentBinding.objects.create(
                channel_account=channel,
                runtime_environment=environment,
                binding_version=1,
                identity_reference="local:manual-publisher:puko-us",
                created_by_principal=owner,
                recorded_by_principal=owner,
            )
        elif (
            binding.status != AccountEnvironmentBinding.Status.ACTIVE
            or binding.identity_reference != "local:manual-publisher:puko-us"
        ):
            raise CommandError("Existing local Dogfood account/environment binding conflicts with the seed.")
        if not binding.is_current_at():
            raise CommandError("Local Dogfood account/environment binding is not the current binding.")

        capability = CapabilityState.objects.filter(
            account_environment_binding=binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state_version=1,
        ).first()
        if capability is None:
            capability = CapabilityState.objects.create(
                account_environment_binding=binding,
                capability_code=CapabilityState.MANUAL_PUBLISH,
                state_version=1,
                state=CapabilityState.State.OPEN,
                reason="Local Dogfood manual publishing only.",
                created_by_principal=owner,
                recorded_by_principal=owner,
            )
        elif capability.state != CapabilityState.State.OPEN:
            raise CommandError("Existing local Dogfood manual-publish CapabilityState is not OPEN.")
        if not capability.is_current_open_at():
            raise CommandError("Local Dogfood manual-publish CapabilityState is not current and OPEN.")
        return channel, environment, binding, capability

    def _ensure_grant(
        self,
        *,
        principal: Principal,
        action: str,
        owner: Principal,
        scope_kind: str,
        product: Product | None = None,
        platform_code: str = "",
        account_ref: str = "",
    ) -> PermissionGrant:
        now = timezone.now()
        if action == PermissionGrant.Action.PUBLISH and scope_kind != PermissionGrant.ScopeKind.ACCOUNT:
            raise CommandError("Bootstrap PUBLISH capability must use an explicit ACCOUNT scope.")
        if action in {
            PermissionGrant.Action.PUBLISH,
            PermissionGrant.Action.MANAGE_ACCOUNT,
            PermissionGrant.Action.EMERGENCY_STOP,
        }:
            required_risk = PermissionGrant.RiskLevel.HIGH
        elif action in {
            PermissionGrant.Action.VIEW,
            PermissionGrant.Action.COLLECT_READ_ONLY,
        }:
            required_risk = PermissionGrant.RiskLevel.LOW
        else:
            required_risk = PermissionGrant.RiskLevel.MEDIUM
        scope_filter = {
            PermissionGrant.ScopeKind.GLOBAL: Q(
                product__isnull=True,
                platform_code="",
                account_ref="",
                surface_ref="",
            ),
            PermissionGrant.ScopeKind.PRODUCT: Q(product=product),
            PermissionGrant.ScopeKind.PLATFORM: Q(platform_code=platform_code),
            PermissionGrant.ScopeKind.ACCOUNT: Q(account_ref=account_ref),
        }[scope_kind]
        grant = PermissionGrant.objects.filter(
            scope_filter,
            principal=principal,
            scope_kind=scope_kind,
            action=action,
            effect=PermissionGrant.Effect.ALLOW,
            grant_status=PermissionGrant.GrantStatus.ACTIVE,
            valid_from__lte=now,
        ).filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now)).order_by("created_at", "id").first()
        if grant is not None and grant.risk_level != required_risk:
            raise CommandError(
                f"Existing {principal.username}/{action} Grant has risk {grant.risk_level}; "
                f"expected {required_risk}. Revoke it explicitly before bootstrapping a replacement."
            )
        if grant is None:
            grant = PermissionGrant.objects.create(
                principal=principal,
                scope_kind=scope_kind,
                product=product if scope_kind == PermissionGrant.ScopeKind.PRODUCT else None,
                platform_code=platform_code if scope_kind == PermissionGrant.ScopeKind.PLATFORM else "",
                account_ref=account_ref if scope_kind == PermissionGrant.ScopeKind.ACCOUNT else "",
                action=action,
                effect=PermissionGrant.Effect.ALLOW,
                risk_level=required_risk,
                valid_from=now - timedelta(minutes=1),
                valid_until=now + timedelta(days=30),
                granted_by_principal=owner,
            )

        decision = resolve_authorization(
            principal=principal,
            acting_role=principal.role,
            action=action,
            scope_kind=scope_kind,
            product=product,
            platform_code=platform_code,
            account_ref=account_ref,
        )
        if not decision.allowed or decision.grant is None:
            raise CommandError(
                f"Cannot establish {principal.username}/{action}: central authorization returned {decision.reason}."
            )
        return decision.grant

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.IS_LOCAL:
            raise CommandError(
                "bootstrap_dogfood is a local-only fixture command and is disabled outside local development. "
                "Create Staging/Production identities through the approved deployment provisioning path."
            )

        humans, rule_evaluator_username = self._requested_identities(options)
        self._preflight_identities(humans, rule_evaluator_username)

        principals = {spec.key: self._ensure_human(spec) for spec in humans}
        owner = principals["owner"]
        rule_evaluator = self._ensure_rule_evaluator(rule_evaluator_username)
        Group.objects.get_or_create(name="Operations Admin")
        Group.objects.get_or_create(name="Operator")

        product, created = Product.objects.get_or_create(
            product_code="PUKO",
            defaults={
                "name": "PUKO Nutrition",
                "market_code": "US",
                "language_code": "en",
                "created_by_principal": owner,
                "updated_by_principal": owner,
            },
        )
        if not created and (product.market_code != "US" or product.language_code != "en"):
            raise CommandError("Existing PUKO Product is not the frozen US/en Dogfood product.")
        policy_version = self._ensure_policy(owner)
        profile = self._ensure_profile(product, owner, policy_version)
        current_profile = product.current_profile_version
        current_is_legacy_seed = bool(
            current_profile
            and not all(
                (
                    current_profile.objective_profile_version_id,
                    current_profile.claim_matrix_version_id,
                    current_profile.evidence_library_version_id,
                )
            )
            and all(getattr(current_profile, name) == value for name, value in PROFILE_VALUES.items())
        )
        if product.current_profile_version_id is None or current_is_legacy_seed:
            product.current_profile_version = profile
            product.updated_by_principal = owner
            product.full_clean()
            product.save(update_fields=["current_profile_version", "updated_by_principal", "updated_at"])
        elif product.current_profile_version_id != profile.id:
            current_profile = product.current_profile_version
            current_state = "sealed" if current_profile.is_sealed else "draft"
            self.stdout.write(
                self.style.WARNING(
                    f"Preserved existing current Product profile v{current_profile.version_number} "
                    f"({current_state}); bootstrap seed profile v{profile.version_number} was not restored as current."
                )
            )

        contract = self._ensure_contract(profile, policy_version, owner)
        channel, environment, binding, capability = self._ensure_release_context(owner)

        # Only the three canonical staff identities receive role-template
        # grants. Legacy reviewer/publisher identities remain narrow
        # capability fixtures; replaying an old database must not silently
        # expand their authority merely because they carry an existing role.
        for key in ("owner", "admin", "operator"):
            principal = principals.get(key)
            if principal is None:
                continue
            for action in ROLE_PRODUCT_GRANT_TEMPLATES[principal.role]:
                self._ensure_grant(
                    principal=principal,
                    action=action,
                    owner=owner,
                    scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                    product=product,
                )

        for key, risk_action in (
            ("owner", PermissionGrant.Action.MANAGE_ACCOUNT),
            ("admin", PermissionGrant.Action.MANAGE_ACCOUNT),
        ):
            principal = principals.get(key)
            if principal is not None:
                self._ensure_grant(
                    principal=principal,
                    action=risk_action,
                    owner=owner,
                    scope_kind=PermissionGrant.ScopeKind.GLOBAL,
                )

        # Governance is a real Daily Operations work queue.  The create and
        # decision services require exact GLOBAL Grants, so bootstrap them
        # explicitly instead of inferring authority from role names.
        for key, actions in (
            (
                "owner",
                (
                    PermissionGrant.Action.VIEW,
                    PermissionGrant.Action.EDIT,
                    PermissionGrant.Action.APPROVE,
                ),
            ),
            (
                "admin",
                (
                    PermissionGrant.Action.VIEW,
                    PermissionGrant.Action.EDIT,
                    PermissionGrant.Action.APPROVE,
                ),
            ),
            (
                "operator",
                (
                    PermissionGrant.Action.VIEW,
                    PermissionGrant.Action.EDIT,
                ),
            ),
        ):
            principal = principals.get(key)
            if principal is not None:
                for action in actions:
                    self._ensure_grant(
                        principal=principal,
                        action=action,
                        owner=owner,
                        scope_kind=PermissionGrant.ScopeKind.GLOBAL,
                    )

        for key in ("owner", "admin", "operator"):
            principal = principals.get(key)
            if principal is not None:
                for platform_code in DAILY_COLLECTION_PLATFORMS:
                    self._ensure_grant(
                        principal=principal,
                        action=PermissionGrant.Action.COLLECT_READ_ONLY,
                        owner=owner,
                        scope_kind=PermissionGrant.ScopeKind.PLATFORM,
                        platform_code=platform_code,
                    )

        # Non-secret connector descriptors are part of the local fixture, not
        # network credentials and not a live API call.
        from dailyops.services import ensure_default_sources

        ensure_default_sources(principal=owner, acting_role=owner.role)

        reviewer = principals.get("reviewer")
        if reviewer is not None:
            for action in (PermissionGrant.Action.REVIEW, PermissionGrant.Action.EDIT):
                self._ensure_grant(
                    principal=reviewer,
                    action=action,
                    owner=owner,
                    scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                    product=product,
                )

        # Publisher is an independently scoped high-risk capability, never a
        # fourth role and never inferred from a role template.  The streamlined
        # three-human demo gives Owner, Admin, and Operator separate exact
        # grants so higher-tier staff can also execute ordinary publishing work
        # without turning the role name into a runtime authorization shortcut.
        # The strict/legacy fixture retains its separate Publisher identity.
        publisher = principals.get("publisher")
        if options["full_demo"] and not options["strict_separation_demo"]:
            for key in ("owner", "admin", "operator"):
                principal = principals.get(key)
                if principal is not None:
                    self._ensure_grant(
                        principal=principal, action=PermissionGrant.Action.PUBLISH, owner=owner,
                        scope_kind=PermissionGrant.ScopeKind.ACCOUNT, product=product,
                        platform_code=channel.platform_code, account_ref=channel.account_code,
                    )
        if publisher is not None:
            self._ensure_grant(
                principal=publisher, action=PermissionGrant.Action.PUBLISH, owner=owner,
                scope_kind=PermissionGrant.ScopeKind.ACCOUNT, product=product,
                platform_code=channel.platform_code, account_ref=channel.account_code,
            )
        if rule_evaluator:
            self._ensure_grant(
                principal=rule_evaluator, action=PermissionGrant.Action.REVIEW, owner=owner,
                scope_kind=PermissionGrant.ScopeKind.PRODUCT, product=product,
            )

        participant_names = ", ".join(sorted(principals.keys() | ({"rule-evaluator"} if rule_evaluator else set())))
        current_profile = product.current_profile_version
        self.stdout.write(
            self.style.SUCCESS(
                f"Dogfood base ready: {product.product_code}, seed profile v{profile.version_number}, "
                f"current profile v{current_profile.version_number}, "
                f"policy v{policy_version.version_number}, contract v{contract.version_number}, "
                f"account {channel.account_code}, environment {environment.environment_code}, "
                f"binding v{binding.binding_version}, capability {capability.state}; principals: {participant_names}. "
                "Staff roles: Owner, Operations Admin, Operator. Reviewer and Publisher are scoped capabilities. "
                "No Task was created."
            )
        )
