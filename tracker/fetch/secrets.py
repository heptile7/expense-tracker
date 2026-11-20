"""PDF statement passwords, kept in the OS credential store (Windows Credential Manager), never in the DB."""

import logging

log = logging.getLogger(__name__)
SERVICE = "expense-tracker"


def _key(account_id: int) -> str:
    from tracker import profiles

    p = profiles.current()
    return f"pdf-password:{account_id}" if p is None or p.is_owner else f"pdf-password:{p.slug}:{account_id}"


def get_pdf_password(account_id: int) -> str | None:
    try:
        import keyring

        return keyring.get_password(SERVICE, _key(account_id))
    except Exception as exc:  # no keyring backend available
        log.warning("Keyring unavailable: %s", exc)
        return None


def set_pdf_password(account_id: int, password: str) -> None:
    import keyring

    keyring.set_password(SERVICE, _key(account_id), password)


def delete_pdf_password(account_id: int) -> None:
    try:
        import keyring

        keyring.delete_password(SERVICE, _key(account_id))
    except Exception:
        pass


def has_pdf_password(account_id: int) -> bool:
    return get_pdf_password(account_id) is not None
