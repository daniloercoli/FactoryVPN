"""History writes share the transaction that changes session state."""

from api.models.session_event import SessionEvent


def event(db, session, event_type, *, source="api", actor_id=None, details=None):
    db.add(
        SessionEvent(
            session_id=session.id,
            source=source,
            event_type=event_type,
            actor_id=actor_id,
            details=details or {},
        )
    )


def reason_code(reason):
    """Never copy exception text, provider responses or credentials into diagnostics."""
    text = (reason or "").lower()
    if "portal session signed out" in text:
        return "portal_logout"
    if "signed out" in text:
        return "account_logout"
    if "password" in text:
        return "password_changed"
    if "sms provider" in text:
        return "sms_unconfirmed"
    if "session expired" == text:
        return "expired"
    if "portal session" in text:
        return "portal_access_invalid"
    if "sso" in text:
        return "sso_access_invalid"
    if "legacy" in text:
        return "legacy_network_missing"
    return "access_revoked"


def terminal_event(db, session, *, actor_id=None, source="api", reason=None):
    event(
        db,
        session,
        "session_" + session.status.lower(),
        source=source,
        actor_id=actor_id,
        details={"reason": reason or reason_code(session.error_reason)},
    )
