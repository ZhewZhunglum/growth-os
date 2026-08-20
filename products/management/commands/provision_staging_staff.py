from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.authorization import resolve_authorization
from accounts.models import PermissionGrant, Principal
from products.models import Product
from releasegate.models import (
    AccountEnvironmentBinding,
    CapabilityState,
    ChannelAccount,
    RuntimeEnvironment,
)
from workflow.models import TaskContractVersion


PRODUCT_MANAGEMENT_ACTIONS = (
    PermissionGrant.Action.EDIT,
    PermissionGrant.Action.CREATE_TASK,
    PermissionGrant.Action.ASSIGN_TASK,
    PermissionGrant.Action.CANCEL_TASK,
    PermissionGrant.Action.COMPLETE_TASK,
    PermissionGrant.Action.REVIEW,
)


@dataclass(frozen=True, slots=True)
class StaffSpec:
    key: str
    username: str
    display_name: str
    role: str
    password_environment: str
    actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PublishContext:
    channel: ChannelAccount
    binding: AccountEnvironmentBinding
    capability: CapabilityState


class Command(BaseCommand):
    help = (
        "Create or verify the three least-privilege Staging staff identities and their exact grants. "
        "This command is disabled in Local and Production."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Commit the validated plan. Without --apply the command performs a rollback-only dry run.",
        )
        parser.add_argument("--product-code", default="PUKO")
        parser.add_argument("--owner-username", default="owner")
        parser.add_argument("--admin-username", default="admin")
        parser.add_argument("--operator-username", default="operator")
        parser.add_argument(
            "--publish-account-code",
            default="",
            help=(
                "Optional existing ACTIVE ChannelAccount code. When supplied, the Operator receives "
                "one account-scoped HIGH-risk PUBLISH grant after its current Staging binding and OPEN "
                "manual-publish capability are verified."
            ),
        )

    @staticmethod
    def _normalized_identifier(value: str, option_name: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise CommandError(f"{option_name} must not be blank.")
        return normalized

    def _staff_specs(self, options) -> tuple[StaffSpec, ...]:
        specs = (
            StaffSpec(
                key="owner",
                username=self._normalized_identifier(options["owner_username"], "--owner-username"),
                display_name="Staging Owner",
                role=Principal.Role.OWNER,
                password_environment="STAGING_OWNER_PASSWORD",
                actions=PRODUCT_MANAGEMENT_ACTIONS,
            ),
            StaffSpec(
                key="admin",
                username=self._normalized_identifier(options["admin_username"], "--admin-username"),
                display_name="Staging Operations Admin",
                role=Principal.Role.OPERATIONS_ADMIN,
                password_environment="STAGING_ADMIN_PASSWORD",
                actions=PRODUCT_MANAGEMENT_ACTIONS,
            ),
            StaffSpec(
                key="operator",
                username=self._normalized_identifier(options["operator_username"], "--operator-username"),
                display_name="Staging Operator",
                role=Principal.Role.OPERATOR,
                password_environment="STAGING_OPERATOR_PASSWORD",
                actions=(PermissionGrant.Action.EDIT,),
            ),
        )
        folded = [spec.username.casefold() for spec in specs]
        if len(folded) != len(set(folded)):
            raise CommandError("Owner, Admin, and Operator usernames must identify three distinct accounts.")
        return specs

    @staticmethod
    def _read_password(spec: StaffSpec) -> str:
        direct_value = os.getenv(spec.password_environment)
        file_name = f"{spec.password_environment}_FILE"
        file_value = os.getenv(file_name)
        if direct_value is not None:
            raise CommandError(
                f"{spec.password_environment} is not accepted in Staging; use the temporary {file_name} Secret file."
            )
        if file_value is None:
            raise CommandError(
                f"{file_name} must be supplied through a temporary Staging Secret file."
            )

        password_path = Path(file_value)
        try:
            file_status = password_path.stat()
            if not stat.S_ISREG(file_status.st_mode):
                raise CommandError(f"{file_name} must identify a regular file.")
            if os.name != "nt" and stat.S_IMODE(file_status.st_mode) & 0o077:
                raise CommandError(
                    f"{file_name} must not be readable or writable by group/other users."
                )
            value = password_path.read_text(encoding="utf-8").rstrip("\r\n")
        except CommandError:
            raise
        except OSError as error:
            raise CommandError(f"Unable to read {file_name}.") from error
        if not value or any(character in value for character in ("\x00", "\r", "\n")):
            raise CommandError(f"{file_name} must contain one non-empty single-line password.")
        return value

    @classmethod
    def _passwords_for_new_set(cls, specs: tuple[StaffSpec, ...]) -> dict[str, str]:
        passwords: dict[str, str] = {}
        minimum = max(12, settings.PASSWORD_MIN_LENGTH)
        for spec in specs:
            value = cls._read_password(spec)
            if len(value) < minimum:
                raise CommandError(
                    f"{spec.password_environment} does not satisfy the Staging minimum of {minimum} characters."
                )
            candidate = Principal(
                username=spec.username,
                display_name=spec.display_name,
                role=spec.role,
                principal_type=Principal.PrincipalType.HUMAN_USER,
            )
            try:
                validate_password(value, user=candidate)
            except ValidationError as error:
                raise CommandError(
                    f"{spec.password_environment} does not satisfy the configured password policy: "
                    f"{' '.join(error.messages)}"
                ) from error
            passwords[spec.key] = value

        if len(set(passwords.values())) != len(passwords):
            raise CommandError("Owner, Admin, and Operator must use three distinct password values.")
        return passwords

    @staticmethod
    def _existing_principal(spec: StaffSpec) -> Principal | None:
        matches = list(Principal.objects.select_for_update().filter(username__iexact=spec.username))
        if len(matches) > 1:
            raise CommandError(f"Multiple case-insensitive Principal matches exist for '{spec.username}'.")
        if not matches:
            return None
        principal = matches[0]
        conflicts: list[str] = []
        if principal.username != spec.username:
            conflicts.append("username casing")
        if principal.role != spec.role:
            conflicts.append("role")
        if principal.principal_type != Principal.PrincipalType.HUMAN_USER:
            conflicts.append("principal_type")
        if principal.principal_status != Principal.PrincipalStatus.ACTIVE or not principal.is_active:
            conflicts.append("active status")
        if principal.auth_provider != "internal":
            conflicts.append("auth_provider")
        if principal.is_staff or principal.is_superuser:
            conflicts.append("Django staff/superuser flags")
        if not principal.has_usable_password():
            conflicts.append("usable password")
        if conflicts:
            raise CommandError(
                f"Existing Principal '{spec.username}' conflicts in: {', '.join(conflicts)}. "
                "The provisioning command will not rewrite an identity."
            )
        return principal

    def _preflight_principals(self, specs: tuple[StaffSpec, ...]) -> dict[str, Principal | None]:
        existing = {spec.key: self._existing_principal(spec) for spec in specs}
        existing_count = sum(principal is not None for principal in existing.values())
        if existing_count not in {0, len(specs)}:
            present = ", ".join(sorted(key for key, principal in existing.items() if principal is not None))
            missing = ", ".join(sorted(key for key, principal in existing.items() if principal is None))
            raise CommandError(
                "Staging staff is only provisioned as one complete three-account set. "
                f"Existing: {present}; missing: {missing}. Resolve the partial set explicitly before retrying."
            )
        return existing

    @staticmethod
    def _load_product_context(product_code: str) -> tuple[Product, TaskContractVersion]:
        product = (
            Product.objects.select_for_update()
            .select_related("current_profile_version")
            .filter(product_code=product_code)
            .first()
        )
        if product is None:
            raise CommandError(f"Existing Product '{product_code}' was not found; this command does not seed products.")
        if product.product_status != Product.ProductStatus.ACTIVE:
            raise CommandError(f"Product '{product_code}' is not ACTIVE.")
        profile = product.current_profile_version
        if profile is None or profile.product_id != product.pk or not profile.is_sealed:
            raise CommandError(f"Product '{product_code}' has no current sealed ProductProfileVersion.")
        contract = (
            TaskContractVersion.objects.filter(
                product_profile_version=profile,
                sealed_at__isnull=False,
            )
            .order_by("-version_number", "-created_at")
            .first()
        )
        if contract is None:
            raise CommandError(
                f"Current profile v{profile.version_number} for Product '{product_code}' has no sealed TaskContractVersion."
            )
        return product, contract

    @staticmethod
    def _load_publish_context(account_code: str) -> PublishContext:
        channel = ChannelAccount.objects.select_for_update().filter(account_code=account_code).first()
        if channel is None:
            raise CommandError(f"Existing ChannelAccount '{account_code}' was not found.")
        if channel.status != ChannelAccount.Status.ACTIVE:
            raise CommandError(f"ChannelAccount '{account_code}' is not ACTIVE.")

        now = timezone.now()
        candidates = list(
            AccountEnvironmentBinding.objects.select_related("runtime_environment")
            .filter(
                channel_account=channel,
                status=AccountEnvironmentBinding.Status.ACTIVE,
                valid_from__lte=now,
                runtime_environment__status=RuntimeEnvironment.Status.ACTIVE,
                runtime_environment__environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            )
            .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now))
        )
        current_bindings = [binding for binding in candidates if binding.is_current_at(now)]
        if len(current_bindings) != 1:
            raise CommandError(
                f"ChannelAccount '{account_code}' must have exactly one current ACTIVE Staging binding; "
                f"found {len(current_bindings)}."
            )
        binding = current_bindings[0]
        capability = (
            CapabilityState.objects.filter(
                account_environment_binding=binding,
                capability_code=CapabilityState.MANUAL_PUBLISH,
            )
            .order_by("-state_version")
            .first()
        )
        if capability is None or not capability.is_current_open_at(now):
            raise CommandError(
                f"ChannelAccount '{account_code}' has no current OPEN MANUAL_PUBLISH CapabilityState."
            )
        return PublishContext(channel=channel, binding=binding, capability=capability)

    @staticmethod
    def _create_principal(spec: StaffSpec, password: str) -> Principal:
        principal = Principal(
            username=spec.username,
            display_name=spec.display_name,
            role=spec.role,
            principal_type=Principal.PrincipalType.HUMAN_USER,
            principal_status=Principal.PrincipalStatus.ACTIVE,
            auth_provider="internal",
            is_active=True,
            is_staff=False,
            is_superuser=False,
        )
        principal.set_password(password)
        principal.full_clean()
        principal.save()
        return principal

    @staticmethod
    def _active_exact_grants(
        *, principal: Principal, action: str, scope_kind: str, product: Product | None, account_ref: str
    ) -> list[PermissionGrant]:
        now = timezone.now()
        return list(
            PermissionGrant.objects.select_for_update().filter(
                principal=principal,
                action=action,
                scope_kind=scope_kind,
                product=product,
                platform_code="",
                account_ref=account_ref,
                surface_ref="",
                grant_status=PermissionGrant.GrantStatus.ACTIVE,
            )
            .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now))
            .order_by("valid_from", "created_at", "id")
        )

    @staticmethod
    def _windows_overlap(
        *,
        left_start,
        left_end,
        right_start,
        right_end,
    ) -> bool:
        return (
            (right_end is None or left_start < right_end)
            and (left_end is None or right_start < left_end)
        )

    def _ensure_grant(
        self,
        *,
        principal: Principal,
        grantor: Principal,
        action: str,
        scope_kind: str,
        risk_level: str,
        product: Product | None = None,
        publish_context: PublishContext | None = None,
        allow_create: bool,
    ) -> PermissionGrant:
        account_ref = publish_context.channel.account_code if publish_context else ""
        now = timezone.now()
        active_grants = self._active_exact_grants(
            principal=principal,
            action=action,
            scope_kind=scope_kind,
            product=product if scope_kind == PermissionGrant.ScopeKind.PRODUCT else None,
            account_ref=account_ref,
        )
        current_allows = [
            candidate
            for candidate in active_grants
            if candidate.effect == PermissionGrant.Effect.ALLOW
            and candidate.valid_from <= now
            and (candidate.valid_until is None or candidate.valid_until > now)
        ]
        if len(current_allows) > 1:
            raise CommandError(
                f"Multiple current exact Grants exist for {principal.username}/{action}; revoke duplicates explicitly."
            )
        if current_allows:
            grant = current_allows[0]
            if grant.risk_level != risk_level:
                raise CommandError(
                    f"Existing {principal.username}/{action} Grant has risk {grant.risk_level}; "
                    f"expected {risk_level}. Revoke it explicitly before provisioning."
                )
            if grant.valid_until is None or grant.valid_until - grant.valid_from > timedelta(days=31):
                raise CommandError(
                    f"Existing {principal.username}/{action} Grant is not a bounded Staging grant. "
                    "Revoke it explicitly before provisioning a 30-day replacement."
                )
            target_start = grant.valid_from
            target_end = grant.valid_until
        else:
            if not allow_create:
                if active_grants:
                    raise CommandError(
                        f"Existing {principal.username}/{action} authority has a current/future ACTIVE exact-scope "
                        "Grant but no single current ALLOW Grant. Resolve the scheduled or conflicting Grant "
                        "explicitly before provisioning."
                    )
                raise CommandError(
                    f"Existing Staging staff set is missing required exact {principal.username}/{action} Grant. "
                    "The provisioning command will not silently expand an existing identity's base authority."
                )
            target_start = now
            target_end = now + timedelta(days=30)

        overlapping = [
            candidate
            for candidate in active_grants
            if (not current_allows or candidate.pk != current_allows[0].pk)
            and self._windows_overlap(
                left_start=candidate.valid_from,
                left_end=candidate.valid_until,
                right_start=target_start,
                right_end=target_end,
            )
        ]
        if overlapping:
            raise CommandError(
                f"Current/future ACTIVE exact-scope Grant overlap exists for {principal.username}/{action}; "
                "revoke or reschedule it explicitly before provisioning."
            )

        if not current_allows:
            grant = PermissionGrant(
                principal=principal,
                scope_kind=scope_kind,
                product=product if scope_kind == PermissionGrant.ScopeKind.PRODUCT else None,
                account_ref=account_ref,
                action=action,
                effect=PermissionGrant.Effect.ALLOW,
                risk_level=risk_level,
                valid_from=target_start,
                valid_until=target_end,
                grant_status=PermissionGrant.GrantStatus.ACTIVE,
                granted_by_principal=grantor,
            )
            grant.full_clean()
            grant.save()

        context = {
            "principal": principal,
            "acting_role": principal.role,
            "action": action,
            "scope_kind": scope_kind,
            "product": product,
        }
        if publish_context:
            context.update(
                platform_code=publish_context.channel.platform_code,
                account_ref=publish_context.channel.account_code,
            )
        decision = resolve_authorization(**context)
        if not decision.allowed or decision.grant is None or decision.grant.pk != grant.pk:
            reason = decision.reason if not decision.allowed else "NON_EXACT_GRANT_RESOLUTION"
            raise CommandError(f"Cannot establish exact {principal.username}/{action} authorization: {reason}.")
        return grant

    def handle(self, *args, **options):
        if settings.ENVIRONMENT != "staging":
            raise CommandError(
                "provision_staging_staff is staging-only and is disabled in Local and Production."
            )

        specs = self._staff_specs(options)
        product_code = self._normalized_identifier(options["product_code"], "--product-code")
        account_code = options["publish_account_code"].strip()
        apply_changes = options["apply"]

        with transaction.atomic():
            # Locking the Product serializes repeat provisioning attempts on
            # PostgreSQL and keeps all identity/grant writes in one transaction.
            product, contract = self._load_product_context(product_code)
            publish_context = self._load_publish_context(account_code) if account_code else None
            existing = self._preflight_principals(specs)
            create_set = not any(existing.values())
            passwords = self._passwords_for_new_set(specs) if create_set else {}
            principals = {
                spec.key: existing[spec.key] or self._create_principal(spec, passwords[spec.key])
                for spec in specs
            }
            owner = principals["owner"]

            for spec in specs:
                principal = principals[spec.key]
                for action in spec.actions:
                    self._ensure_grant(
                        principal=principal,
                        grantor=owner,
                        action=action,
                        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                        risk_level=PermissionGrant.RiskLevel.MEDIUM,
                        product=product,
                        allow_create=create_set,
                    )

            if publish_context:
                self._ensure_grant(
                    principal=principals["operator"],
                    grantor=owner,
                    action=PermissionGrant.Action.PUBLISH,
                    scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
                    risk_level=PermissionGrant.RiskLevel.HIGH,
                    product=product,
                    publish_context=publish_context,
                    # Supplying --publish-account-code is the explicit request
                    # to create this separately controlled high-risk grant.
                    allow_create=True,
                )

            if not apply_changes:
                transaction.set_rollback(True)

        publish_summary = f", publish account {account_code}" if publish_context else ", no PUBLISH grant requested"
        if apply_changes:
            operation = "CREATED" if create_set else ("VERIFIED/UPDATED" if account_code else "VERIFIED")
        else:
            operation = "WOULD CREATE" if create_set else ("WOULD VERIFY/UPDATE" if account_code else "WOULD VERIFY")
            operation = f"{operation} (dry run; zero committed writes)"
        self.stdout.write(
            self.style.SUCCESS(
                f"{operation} Staging staff for Product {product.product_code}, profile "
                f"v{product.current_profile_version.version_number}, contract v{contract.version_number}: "
                f"Owner {principals['owner'].username}, Admin {principals['admin'].username}, "
                f"Operator {principals['operator'].username}{publish_summary}. Passwords were not printed or reset."
            )
        )
