"""Translation helpers. Strings are marked for gettext from day one."""

import gettext as _gettext

DOMAIN = "quotaglance"

_translation = _gettext.translation(DOMAIN, fallback=True)


def _(message: str) -> str:
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translation.ngettext(singular, plural, n)
