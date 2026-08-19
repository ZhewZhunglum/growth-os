from django.contrib.auth.backends import ModelBackend


class PrincipalStatusBackend(ModelBackend):
    """Authentication backend guard for deployments that configure it explicitly."""

    def user_can_authenticate(self, user):
        return bool(getattr(user, "can_authenticate", False)) and super().user_can_authenticate(user)

