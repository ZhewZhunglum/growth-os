from django.contrib.auth.backends import ModelBackend

from accounts.models import Principal


class PrincipalStatusBackend(ModelBackend):
    """Allow interactive password login only for active human Principals."""

    def user_can_authenticate(self, user):
        return (
            getattr(user, "principal_type", None) == Principal.PrincipalType.HUMAN_USER
            and bool(getattr(user, "can_authenticate", False))
            and super().user_can_authenticate(user)
        )

