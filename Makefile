# Installs into ~/.local by default:  make install / make uninstall
PREFIX  ?= $(HOME)/.local
DESTDIR ?=
PYTHON  ?= /usr/bin/python3

APP_ID   = io.github.jayyo.MeetRec
BINDIR   = $(PREFIX)/bin
DATADIR  = $(PREFIX)/share
PKGDIR   = $(DATADIR)/meetrec
ICONDIR  = $(DATADIR)/icons/hicolor
LANGS    = $(basename $(notdir $(wildcard po/*.po)))
MO       = $(foreach l,$(LANGS),locale/$(l)/LC_MESSAGES/meetrec.mo)

.PHONY: all check pot install uninstall clean

all: $(MO)

locale/%/LC_MESSAGES/meetrec.mo: po/%.po
	@mkdir -p $(dir $@)
	msgfmt --check -o $@ $<

check:
	$(PYTHON) -m pytest -q

pot:
	xgettext --language=Python --keyword=_ --from-code=UTF-8 --no-location \
	  --sort-output --package-name=meetrec -o po/meetrec.pot meetrec/*.py

install: all
	install -d $(DESTDIR)$(PKGDIR)/meetrec $(DESTDIR)$(BINDIR)
	install -m 644 meetrec/*.py $(DESTDIR)$(PKGDIR)/meetrec/
	install -m 755 bin/meetrec $(DESTDIR)$(BINDIR)/
	sed -i '1s|.*|#!$(PYTHON)|' $(DESTDIR)$(BINDIR)/meetrec
	install -d $(DESTDIR)$(DATADIR)/applications $(DESTDIR)$(ICONDIR)/scalable/apps $(DESTDIR)$(ICONDIR)/symbolic/apps
	sed 's|@BINDIR@|$(BINDIR)|' data/$(APP_ID).desktop.in > $(DESTDIR)$(DATADIR)/applications/$(APP_ID).desktop
	install -m 644 data/$(APP_ID).svg $(DESTDIR)$(ICONDIR)/scalable/apps/
	install -m 644 data/$(APP_ID)-symbolic.svg $(DESTDIR)$(ICONDIR)/symbolic/apps/
	$(foreach l,$(LANGS),install -D -m 644 locale/$(l)/LC_MESSAGES/meetrec.mo \
	  $(DESTDIR)$(DATADIR)/locale/$(l)/LC_MESSAGES/meetrec.mo;)

uninstall:
	rm -rf $(DESTDIR)$(PKGDIR)
	rm -f $(DESTDIR)$(BINDIR)/meetrec
	rm -f $(DESTDIR)$(DATADIR)/applications/$(APP_ID).desktop
	rm -f $(DESTDIR)$(ICONDIR)/scalable/apps/$(APP_ID).svg $(DESTDIR)$(ICONDIR)/symbolic/apps/$(APP_ID)-symbolic.svg
	$(foreach l,$(LANGS),rm -f $(DESTDIR)$(DATADIR)/locale/$(l)/LC_MESSAGES/meetrec.mo;)

clean:
	rm -rf locale .pytest_cache
