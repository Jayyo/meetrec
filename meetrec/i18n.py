"""gettext setup. English strings live in the code; po/*.po hold translations.

Compiled catalogs are looked up in, in order:
  $MEETREC_LOCALEDIR
  <checkout>/locale            (`make` builds them there)
  <prefix>/share/locale        (`make install`)
Without any catalog the UI is simply English.
"""

import gettext
import locale
import os
from pathlib import Path

DOMAIN = "meetrec"


def _localedir() -> str | None:
    here = Path(__file__).resolve().parent
    for c in (os.environ.get("MEETREC_LOCALEDIR"),
              here.parent / "locale",                 # checkout
              here.parent.parent / "locale"):         # <prefix>/share/meetrec/..
        if c and Path(c).is_dir():
            return str(c)
    return None


try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    pass

_t = gettext.translation(DOMAIN, localedir=_localedir(), fallback=True)
_ = _t.gettext
