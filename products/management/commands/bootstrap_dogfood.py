from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta

from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.authorization import resolve_authorization
from accounts.models import PermissionGrant, Principal
from products.models import Product, ProductProfileVersion
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


class Command(BaseCommand):
    help = "Create an idempotent local PUKO Dogfood base context without embedding credentials or Tasks."

    def add_arguments(self, parser):
        parser.add_argument("--owner-username", default="owner")
        parser.add_argument("--operator-username")
        parser.add_argument("--reviewer-username")
        parser.add_argument("--publisher-username")
        parser.add_argument("--rule-evaluator-username")
        parser.add_argument(
            "--full-demo",
            action="store_true",
            help=(
                "Request operator, reviewer, publisher, and rule-evaluator identities. "
                "New human identities require their distinct BOOTSTRAP_*_PASSWORD environment variables."
            ),
        )

    def _requested_identities(self, options) -> tuple[list[HumanSpec], str | None]:
        defaults = {
            "operator_username": "operator",
            "reviewer_username": "reviewer",
            "publisher_username": "publisher",
            "rule_evaluator_username": "rule-evaluator",
        }
        if options["full_demo"]:
            for key, value in defaults.items():
                options[key] = options[key] or value

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
            ("operator", "operator_username", "BOOTSTRAP_OPERATOR_PASSWORD", Principal.Role.OPERATOR,
             "PUKO Operator", "Operator"),
            ("reviewer", "reviewer_username", "BOOTSTRAP_REVIEWER_PASSWORD", Principal.Role.OPERATIONS_ADMIN,
             "PUKO Reviewer", "Operations Admin"),
            ("publisher", "publisher_username", "BOOTSTRAP_PUBLISHER_PASSWORD", Principal.Role.OPERATOR,
             "PUKO Publisher", "Operator"),
        )
        for key, option, password_env, role, display_name, group_name in optional:
            username = options.get(option)
            if username:
                humans.append(HumanSpec(key, username, password_env, role, display_name, group_name))
        return humans, options.get("rule_evaluator_username")

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
                if spec.key == "owner" and principal.is_superuser:
                    continue
                if principal.role != spec.role:
                    raise CommandError(
                        f"Existing {spec.key} account '{spec.username}' has role {principal.role}, expected {spec.role}."
                    )
                continue
            password = os.getenv(spec.password_env)
            if not password:
                raise CommandError(
                    f"New {spec.key} account '{spec.username}' requires {spec.password_env}; "
                    "no human account was created."
                )
            new_passwords.append((spec.password_env, password))

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
            password = os.environ[spec.password_env]
            if spec.key == "owner":
                principal = Principal.objects.create_superuser(
                    username=spec.username,
                    password=password,
                    display_name=spec.display_name,
                    principal_type=Principal.PrincipalType.HUMAN_USER,
                    role=spec.role,
                )
            else:
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

    def _ensure_profile(self, product: Product, owner: Principal) -> ProductProfileVersion:
        profile = ProductProfileVersion.objects.filter(product=product, version_number=1).first()
        if profile is None:
            profile = ProductProfileVersion.objects.create(
                product=product,
                version_number=1,
                created_by_principal=owner,
                **PROFILE_VALUES,
            )
        else:
            mismatched = [name for name, value in PROFILE_VALUES.items() if getattr(profile, name) != value]
            if mismatched:
                raise CommandError(f"Existing PUKO profile v1 differs in: {', '.join(mismatched)}.")
        if not profile.is_sealed:
            profile.seal(owner)
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
        scope_filter = {
            PermissionGrant.ScopeKind.PRODUCT: Q(product=product),
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
        if grant is None:
            grant = PermissionGrant.objects.create(
                principal=principal,
                scope_kind=scope_kind,
                product=product if scope_kind == PermissionGrant.ScopeKind.PRODUCT else None,
                account_ref=account_ref if scope_kind == PermissionGrant.ScopeKind.ACCOUNT else "",
                action=action,
                effect=PermissionGrant.Effect.ALLOW,
                risk_level=(
                    PermissionGrant.RiskLevel.HIGH
                    if action == PermissionGrant.Action.PUBLISH
                    else PermissionGrant.RiskLevel.MEDIUM
                ),
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
        profile = self._ensure_profile(product, owner)
        if product.current_profile_version_id != profile.id:
            product.current_profile_version = profile
            product.updated_by_principal = owner
            product.full_clean()
            product.save(update_fields=["current_profile_version", "updated_by_principal", "updated_at"])

        policy_version = self._ensure_policy(owner)
        contract = self._ensure_contract(profile, policy_version, owner)
        channel, environment, binding, capability = self._ensure_release_context(owner)

        self._ensure_grant(
            principal=owner,
            action=PermissionGrant.Action.EDIT,
            owner=owner,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=product,
        )
        if operator := principals.get("operator"):
            self._ensure_grant(
                principal=operator, action=PermissionGrant.Action.EDIT, owner=owner,
                scope_kind=PermissionGrant.ScopeKind.PRODUCT, product=product,
            )
        if reviewer := principals.get("reviewer"):
            self._ensure_grant(
                principal=reviewer, action=PermissionGrant.Action.REVIEW, owner=owner,
                scope_kind=PermissionGrant.ScopeKind.PRODUCT, product=product,
            )
            # Operations Admin records the immutable review with REVIEW and
            # projects its outcome onto the Task with a separate explicit EDIT
            # grant.  Review authority is never inferred from assignment.
            self._ensure_grant(
                principal=reviewer, action=PermissionGrant.Action.EDIT, owner=owner,
                scope_kind=PermissionGrant.ScopeKind.PRODUCT, product=product,
            )
        if publisher := principals.get("publisher"):
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
        self.stdout.write(
            self.style.SUCCESS(
                f"Dogfood base ready: {product.product_code}, profile v{profile.version_number}, "
                f"policy v{policy_version.version_number}, contract v{contract.version_number}, "
                f"account {channel.account_code}, environment {environment.environment_code}, "
                f"binding v{binding.binding_version}, capability {capability.state}; principals: {participant_names}. "
                "No Task was created."
            )
        )
