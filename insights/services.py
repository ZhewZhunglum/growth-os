from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import LearningVersion


def decide_learning(*, learning: LearningVersion, decision: str, actor_principal) -> LearningVersion:
    """Append a human decision; a Learning never mutates policy or execution state."""

    if decision not in {LearningVersion.Status.APPROVED, LearningVersion.Status.REJECTED}:
        raise ValidationError({"decision": "Learning may only be approved or rejected by a human."})
    if actor_principal.principal_type != "HUMAN_USER":
        raise ValidationError({"actor_principal": "AI and service principals may only propose Learning."})
    with transaction.atomic():
        tip = LearningVersion.objects.select_for_update().filter(
            learning_key=learning.learning_key,
            product=learning.product,
        ).order_by("-version_number").first()
        if tip is None or tip.pk != learning.pk:
            raise ValidationError("Only the current Learning version may receive a decision.")
        return LearningVersion.objects.create(
            learning_key=learning.learning_key,
            version_number=learning.version_number + 1,
            product=learning.product,
            title=learning.title,
            conclusion=learning.conclusion,
            recommended_action=learning.recommended_action,
            confidence=learning.confidence,
            status=decision,
            supersedes_version=learning,
            created_by_principal=actor_principal,
        )
