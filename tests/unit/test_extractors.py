"""Unit tests for email/phone extraction and deobfuscation."""

from __future__ import annotations

import pytest

from kronikier.extractors import (
    Contact,
    _decode_cfemail,
    extract_contacts,
    extract_emails,
    extract_phones,
)


# ---------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------


class TestEmailExtraction:
    def test_plain_email(self):
        emails = [c.value for c in extract_emails("Reach me at hello@example.com")]
        assert emails == ["hello@example.com"]

    def test_mailto_href(self):
        html = '<a href="mailto:info@foo.ru?subject=Hi">click me</a>'
        emails = [c.value for c in extract_emails(html)]
        assert emails == ["info@foo.ru"]

    def test_html_entity_obfuscation(self):
        html = "Email: support&#64;example&#46;com"
        emails = [c.value for c in extract_emails(html)]
        assert emails == ["support@example.com"]

    def test_square_bracket_at_dot(self):
        html = "Contact: ivan [at] gazprom [dot] ru"
        emails = [c.value for c in extract_emails(html)]
        assert emails == ["ivan@gazprom.ru"]

    def test_parenthesized_at_dot(self):
        html = "Contact: petrov (at) yandex (dot) ru"
        emails = [c.value for c in extract_emails(html)]
        assert emails == ["petrov@yandex.ru"]

    def test_russian_sobaka_tochka(self):
        html = "Пишите: director (собака) romashka (точка) ru"
        emails = [c.value for c in extract_emails(html)]
        assert emails == ["director@romashka.ru"]

    def test_word_at_word_dot_word(self):
        html = "drop me a line: jdoe at protonmail dot com please"
        emails = [c.value for c in extract_emails(html)]
        assert emails == ["jdoe@protonmail.com"]

    def test_fullwidth_at(self):
        html = "<p>contact: weird＠example．com</p>"
        emails = [c.value for c in extract_emails(html)]
        assert emails == ["weird@example.com"]

    def test_cloudflare_email_protection(self):
        # Encodes "ceo@acmewidgets.com" with key 0x4a
        html = (
            '<a class="__cf_email__" '
            'data-cfemail="4a292f250a2b29272f3d232e2d2f3e3964292527">'
            "[email&#160;protected]</a>"
        )
        emails = [c.value for c in extract_emails(html)]
        assert emails == ["ceo@acmewidgets.com"]

    def test_image_filename_with_at_is_not_email(self):
        html = '<img src="logo@2x.png" alt="Logo"> Pixel: sentry@2x.png'
        emails = [c.value for c in extract_emails(html)]
        assert emails == []

    def test_dedup_across_passes(self):
        html = """
        <a href="mailto:info@romashka.ru">Email</a>
        Or write info [at] romashka [dot] ru
        Or <span>info&#64;romashka&#46;ru</span>
        """
        emails = [c.value for c in extract_emails(html)]
        assert emails == ["info@romashka.ru"]

    def test_strips_trailing_punctuation(self):
        html = "see foo@bar.com, thanks."
        emails = [c.value for c in extract_emails(html)]
        assert emails == ["foo@bar.com"]

    def test_no_domain_based_filtering(self):
        """We surface every syntactically valid email — domain doesn't matter.

        OSINT investigators want to see contact emails on free providers
        (mail.ru, gmail.com) and even SaaS-looking domains; filtering on the
        domain throws away the strongest signal for small-business sites.
        """
        html = (
            "Contact: jdoe@gmail.com or business@mail.ru. "
            "Newsletter via newsletter@mailchimp.com. "
            "Also: support@sentry.io"
        )
        emails = {c.value for c in extract_emails(html)}
        assert "jdoe@gmail.com" in emails
        assert "business@mail.ru" in emails
        assert "newsletter@mailchimp.com" in emails
        assert "support@sentry.io" in emails

    def test_no_email_in_pure_text(self):
        emails = list(extract_emails("There is no email on this page."))
        assert emails == []

    def test_www_domain_email_is_rejected(self):
        """Domains starting with ``www.`` are always an artifact of the
        at-obfuscation deobfuscator turning prose like ``"Archive at
        www.example.com"`` into ``"Archive@www.example.com"``. Real
        mailboxes aren't published on ``www.`` subdomains.
        """
        # Direct literal — must be rejected.
        assert list(extract_emails("Contact releases.archive@www.dgap.test"))  == []
        # Prose form that the deobfuscator would otherwise capture.
        emails = [c.value for c in extract_emails(
            "<p>Archive at www.dgap.test for filings.</p>"
        )]
        assert emails == [], emails
        # ``found at www.…`` is the second flavour the user reported.
        emails = [c.value for c in extract_emails(
            "<p>Reports found at www.example.test online.</p>"
        )]
        assert emails == [], emails

    def test_non_www_subdomain_still_extracted(self):
        """Negative control for the ``www.`` filter: a subdomain that
        merely *contains* ``www`` but isn't a leading ``www.`` segment
        must still be accepted.
        """
        emails = [c.value for c in extract_emails(
            "<p>Reach us at info@www-staging.example.test anytime.</p>"
        )]
        assert "info@www-staging.example.test" in emails, emails

    def test_prose_at_pattern_does_not_become_email(self):
        """English prose like ``"support is available at nginx.com"`` was
        being turned into ``available@nginx.com`` by the at-deobfuscator.
        Pass-2 stop-word filter rejects these prose artefacts.
        """
        prose_cases = [
            "Commercial support is available at nginx.com.",
            "Source code is hosted at github.test.",
            "Documentation is found at docs.example.test.",
            "Replicas are located at backup.example.test.",
            "Mirror archived at archive.example.test.",
            "Properly listed at directory.example.test.",
            "Company headquartered at hq.example.test.",
            "Domain registered at registrar.example.test.",
        ]
        for prose in prose_cases:
            emails = [c.value for c in extract_emails(f"<p>{prose}</p>")]
            assert emails == [], f"prose {prose!r} produced emails {emails!r}"

    def test_real_email_with_stopword_local_in_pass1_is_kept(self):
        """A real ``available@somecompany.test`` (mailto: or plain ``@``)
        must NOT be filtered — the stop-word check applies only to Pass-2
        deobfuscation outputs that didn't already surface in Pass 1.
        """
        html = (
            '<a href="mailto:available@somecompany.test">Email us</a>'
            "<p>Sales support is hosted at example.test.</p>"  # this is prose
        )
        emails = [c.value for c in extract_emails(html)]
        assert "available@somecompany.test" in emails, emails
        # The prose form must not have leaked.
        assert "hosted@example.test" not in emails, emails

    def test_legitimate_at_obfuscation_still_works(self):
        """The stop-word filter must not break genuine obfuscated emails
        whose local part isn't a prose stopword (``john at example.com``,
        ``support at example.com``).
        """
        cases = [
            ("Contact: john at example.test", "john@example.test"),
            ("Email support at example.test", "support@example.test"),
            ("Write to ivan (at) gazprom (dot) test", "ivan@gazprom.test"),
        ]
        for prose, expected in cases:
            emails = [c.value for c in extract_emails(f"<p>{prose}</p>")]
            assert expected in emails, f"missing {expected!r} in {emails!r}"

    def test_glued_text_after_inline_tags_does_not_extend_domain(self):
        """The wirecard.com regression: when an email is wrapped in
        inline elements followed immediately by more text (no whitespace
        between the closing tag and the next text node), BeautifulSoup's
        ``get_text(" ")`` injects a separator so the email regex stops
        at the real TLD instead of greedily extending into the next word.

        The JS DOMParser path needs an equivalent fix (separator-injecting
        walker); this test pins the Python behaviour as the reference.
        """
        html = (
            "<a>Asia<span></span>&#064;<span></span>wirecard.test</a>"
            "<br /><br />Company N"
        )
        emails = [c.value for c in extract_emails(html)]
        assert "asia@wirecard.test" in emails, emails
        # The merged form must NOT appear.
        for e in emails:
            assert not e.startswith("asia@wirecard.testcompany"), e

    def test_cfemail_decoder_direct(self):
        # Known good vector: encode "user@a.com" with key 0x12
        key = 0x12
        msg = "user@a.com"
        token = f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in msg)
        assert _decode_cfemail(token) == "user@a.com"

    def test_cfemail_decoder_invalid(self):
        assert _decode_cfemail("nothex") is None
        # Decodes but no '@' → reject
        assert _decode_cfemail("00414243") is None  # decodes to "ABC"


# ---------------------------------------------------------------------------
# Phones
# ---------------------------------------------------------------------------


class TestPhoneExtraction:
    def test_russian_landline_international(self):
        html = "Tel: +7 (495) 123-45-67"
        phones = [c.value for c in extract_phones(html)]
        assert "+74951234567" in phones

    def test_russian_mobile_with_local_8_prefix(self):
        # "8 (916) 555-12-34" in RU defaults to +7 916 555 12 34
        html = "<p>Mobile: 8 (916) 555-12-34</p>"
        phones = [c.value for c in extract_phones(html, default_regions=("RU",))]
        assert "+79165551234" in phones

    def test_us_phone(self):
        html = "Call us: (217) 555-0192 or 1-800-555-0199"
        phones = [c.value for c in extract_phones(html, default_regions=("US",))]
        assert "+12175550192" in phones
        assert "+18005550199" in phones

    def test_belarus_international_format(self):
        # BY mobile, +375-29 prefix.
        html = "Заместитель директора +375-29-722-84-40"
        phones = [c.value for c in extract_phones(html, default_regions=("BY",))]
        assert "+375297228440" in phones

    def test_belarus_local_8_prefix(self):
        # ПТО line — uses BY trunk format 8-0162-...
        html = "ПТО 8-0162-51-12-54"
        phones = [c.value for c in extract_phones(html, default_regions=("BY",))]
        assert "+375162511254" in phones

    def test_belarus_local_8_prefix_inside_tel_href_with_ru_first(self):
        """Regression: an archived BY contact page has ``8-0162-51-12-54``
        inside ``<a href="tel:80162511254">…</a>``. Even when ``RU`` precedes
        ``BY`` in ``default_regions`` (e.g. when called without ccTLD-priming),
        libphonenumber must still classify it as BY — the structural ``0162``
        area code is not a valid RU national prefix.
        """
        html = (
            '<p>Тел./факс: <a href="tel:80162511254">8-0162-51-12-54</a></p>'
            '<p>Моб.: +375 (29) 123-45-67</p>'
        )
        for regions in (("BY", "RU"), ("RU", "BY", "UA", "KZ")):
            phones = [c.value for c in extract_phones(html, default_regions=regions)]
            assert "+375162511254" in phones, f"missing landline with regions={regions}"
            assert "+375291234567" in phones, f"missing mobile with regions={regions}"

    def test_typo_short_number_still_surfaced(self):
        """A site published `+375-33-354518` (one digit short of a real BY
        mobile). libphonenumber's strict ``VALID`` leniency would reject it,
        but for OSINT the typo is still a signal — and the site clearly
        meant *some* BY number. We surface and E.164-normalize it.
        """
        html = "Главный инженер +375-33-354518"
        phones = [c.value for c in extract_phones(html, default_regions=("BY",))]
        assert "+37533354518" in phones

    def test_us_toll_free_855_not_claimed_by_russia(self):
        """Real-world regression. On a US-anchored .com site, the toll-free
        ``(855) 843-7200`` is a clearly US number, but the old Pass-1 logic
        (POSSIBLE leniency, no region) interpreted the leading digits as a
        Russian IDD prefix and yielded ``+78558437200``. With ``+``-required
        Pass 1 plus the ``.com → US`` TLD prefix, US wins as it should.
        """
        from kronikier.pipeline import _regions_for_domain
        html = "<p>Call: (855) 843-7200</p>"
        regions = _regions_for_domain("example.com", ("RU", "BY", "US", "GB"))
        phones = [c.value for c in extract_phones(html, default_regions=regions)]
        assert "+18558437200" in phones
        assert "+78558437200" not in phones

    def test_ru_landline_with_leading_8_not_claimed_by_us(self):
        """Mirror-image regression. On a ``.ru`` site, ``8(863)-218-22-22``
        is the Rostov-area landline ``+7 863 218 22 22``. Old Pass 1 (with
        POSSIBLE leniency and no region) read the leading ``8`` as a
        Russian IDD prefix and parsed the remainder as ``+1 863 218-2222``
        (a Lakeland-FL number). With ``+``-required Pass 1, the trunk-vs-IDD
        ambiguity stays in Pass 2 where ``.ru → RU`` puts Russia first.
        """
        from kronikier.pipeline import _regions_for_domain
        html = "<p>Тел: 8(863)-218-22-22</p>"
        regions = _regions_for_domain("avito-equiv.ru", ("RU", "US"))
        phones = [c.value for c in extract_phones(html, default_regions=regions)]
        assert "+78632182222" in phones
        assert "+18632182222" not in phones

    def test_explicit_plus_prefix_still_extracted_in_pass1(self):
        """The ``+``-required Pass-1 filter must not break the common case
        of internationally-formatted numbers that *do* carry a ``+``.
        """
        html = "<p>+44 20 7946 0958 main line</p>"
        phones = [c.value for c in extract_phones(html, default_regions=("US",))]
        assert "+442079460958" in phones

    def test_dates_with_four_digit_year_are_not_misread_as_phones(self):
        """Dates like ``02.09.2008``, ``9/2/2008``, and ``2008-09-02`` have
        phone-shaped digit runs and slip through libphonenumber. The
        4-digit-year anchor in ``_looks_like_date`` rejects them while
        leaving real phones untouched.
        """
        html = (
            "<p>Published 02.09.2008. Updated 9/2/2008.</p>"
            "<p>ISO date: 2008-09-02</p>"
            "<p>Контакт: +375-29-722-84-40</p>"
        )
        contacts = list(extract_phones(html, default_regions=("BY", "RU", "US")))
        values = [c.value for c in contacts]
        raws = [c.raw for c in contacts]
        # The real phone must still come through.
        assert "+375297228440" in values
        # None of the dates should appear as phones.
        for date_raw in ("02.09.2008", "9/2/2008", "2008-09-02"):
            assert date_raw not in raws, f"date leaked as phone: {date_raw}"
        # And no canonicalized form derived from those dates either.
        for date_canonical in ("+72008", "+12008", "+3752008"):
            assert not any(v.startswith(date_canonical) for v in values), (
                f"date-derived phone leaked: prefix {date_canonical}, values {values}"
            )

    def test_svg_polygon_points_inside_script_are_not_misread(self):
        """Inline SVG (``<polygon points="86.6,50 50,86.6 …"/>``) and the
        same markup serialised as a JS string inside ``<script>`` carry long
        coordinate runs that libphonenumber otherwise claims as phones. The
        ``<script>``/``<style>``/``<svg>``/``<noscript>`` subtree drop in
        ``_normalize_html`` removes them from the text the matcher sees.
        """
        html = (
            '<html><body>'
            '<svg viewBox="0 0 100 100">'
            '<polygon points="86.6,50 50,86.6 13.4,50 50,13.4 16.05,50 17.3"/>'
            '</svg>'
            '<script>var icon = "<polygon points=\\"86.6,50 50,86.6\\"/>";</script>'
            '<style>.dot{margin:12 48 86 50}</style>'
            '<p>Real phone: +49 89 12345678</p>'
            '</body></html>'
        )
        contacts = list(extract_phones(html, default_regions=("DE", "RU", "UA")))
        values = [c.value for c in contacts]
        # Real phone surfaces.
        assert any(v.startswith("+4989") for v in values), values
        # No phantoms from polygon/style/script coordinates.
        for v in values:
            assert "86650" not in v and "508" not in v[1:5], (
                f"polygon/style digits leaked as phone: {v}"
            )

    def test_linkedin_partner_id_in_script_is_not_misread(self):
        """``_linkedin_data_partner_id = "298428"`` inside a ``<script>``
        tag must not produce a phone match — the entire script subtree is
        dropped before matching.
        """
        html = (
            '<html><body>'
            '<script type="text/javascript">'
            '_linkedin_data_partner_id = "298428";'
            '</script>'
            '<p>Contact: +1 650 555 1234</p>'
            '</body></html>'
        )
        contacts = list(extract_phones(html, default_regions=("US", "RU", "UA")))
        values = [c.value for c in contacts]
        assert "+16505551234" in values
        for v in values:
            assert "298428" not in v, f"LinkedIn partner ID leaked as phone: {v}"

    def test_datetime_stamps_do_not_leak_into_phone_matches(self):
        """``19.06.2020 / 12:48`` and similar timestamps must not surface
        any phone-like substring — neither the date, the clock, nor a
        fusion of nearby digits with the timestamp.
        """
        html = (
            "<p>Опубликовано: 19.06.2020 / 12:48</p>"
            "<p>Updated 2020-06-19 12:48:30</p>"
            "<p>Real: +375 29 722 84 40</p>"
        )
        contacts = list(extract_phones(html, default_regions=("BY", "RU", "UA")))
        values = [c.value for c in contacts]
        raws = [c.raw for c in contacts]
        assert "+375297228440" in values
        for forbidden in ("12:48", "12.48", "2020", "1248"):
            for raw in raws:
                assert forbidden not in raw, (
                    f"datetime fragment leaked as phone raw: {raw!r}"
                )

    def test_phone_split_across_line_breaks_is_recovered(self):
        """A phone broken across ``<br>``/``<span>`` boundaries (so its
        textContent contains a newline mid-number) was being truncated by
        libphonenumber, which treats newline as a stronger boundary than
        space. The whitespace-collapse pass in ``_normalize_html`` glues
        the halves back together.
        """
        # Three real-world shapes: <br>, separate <div>s, NBSP between groups.
        cases = [
            '<p>Tel: +49<br>(0)89-4424<br>1400</p>',
            '<div>Tel: +49</div><div>(0)89-4424 1400</div>',
            '<p>Tel:&nbsp;+49&nbsp;(0)89-4424&nbsp;1400</p>',
        ]
        for html in cases:
            contacts = list(extract_phones(html, default_regions=("DE",)))
            values = [c.value for c in contacts]
            assert "+498944241400" in values, (
                f"German number not recovered from {html!r}: got {values}"
            )

    def test_paren_year_cluster_not_misread_as_phone(self):
        """``(75) 2018`` and similar parenthesised-token-plus-year clusters
        (typical of press-release archive listings) must not be claimed as
        phones. The ``_PAREN_YEAR_RE`` blank handles them.
        """
        html = (
            "<p>Press releases (75) 2018 — view archive</p>"
            "<p>Real number: +49 89 12345678</p>"
        )
        contacts = list(extract_phones(html, default_regions=("DE", "RU")))
        values = [c.value for c in contacts]
        raws = [c.raw for c in contacts]
        assert any(v.startswith("+4989") for v in values), values
        for raw in raws:
            assert "(75)" not in raw and "2018" not in raw, (
                f"paren-year leaked as phone: {raw!r}"
            )

    def test_repeated_year_not_misread_as_phone(self):
        """``2012 2012`` (a year repeated, common on dated archive listings
        and footers) is an 8-digit cluster — DE region happily claims it
        as a landline. ``_REPEATED_YEAR_RE`` blanks the repeat.
        """
        html = "<p>Copyright 2012 2012 Wirecard AG</p>"
        contacts = list(extract_phones(html, default_regions=("DE", "RU")))
        for c in contacts:
            assert "2012" not in c.raw, f"repeated year leaked: {c.raw!r}"

    def test_case_number_with_slash_not_misread_as_phone(self):
        """``596/2014`` and the like (case-numbers, ordinance citations,
        German Aktenzeichen) must not be claimed as phones. The
        ``_CASE_NUMBER_RE`` blank handles them — and crucially must run
        BEFORE the slash-in-phone bridge so the bridge doesn't first turn
        ``596/2014`` into a phone-shaped ``596 2014``.
        """
        html = "<p>Cited in 596/2014 of the BaFin proceedings.</p>"
        contacts = list(extract_phones(html, default_regions=("DE", "RU")))
        values = [c.value for c in contacts]
        assert not any(v.endswith("5962014") for v in values), values
        for c in contacts:
            assert "596" not in c.raw and "2014" not in c.raw, c.raw

    def test_year_followed_by_short_numbers_not_misread_as_phone(self):
        """``2020 19`` / ``2020 06 19`` — a year leading a short-number
        cluster (typical of URL-path dates that survived tag-stripping
        without their slashes, e.g. the path ``/2020/06/19/article``
        rendered across separate elements). ``_YEAR_NUM_CLUSTER_RE``
        blanks the cluster.
        """
        for fragment in ("2020 19", "2020 06 19", "Press 2019 06 18"):
            html = f"<p>{fragment} something</p>"
            contacts = list(extract_phones(html, default_regions=("DE", "RU")))
            for c in contacts:
                assert "2020" not in c.raw and "2019" not in c.raw, (
                    f"year cluster {fragment!r} leaked: {c.raw!r}"
                )

    def test_date_with_spaces_around_separators_still_blanked(self):
        """When the date has been split across inline elements and rejoined
        with single spaces around the separators (``2020 / 06 / 19``,
        ``19 . 06 . 2020``), the relaxed ``_DATETIME_RE`` still blanks it.
        """
        for fragment in ("2020 / 06 / 19", "19 . 06 . 2020", "2020-06 - 19"):
            html = f"<p>Published {fragment}</p>"
            contacts = list(extract_phones(html, default_regions=("DE", "RU")))
            for c in contacts:
                for bit in ("2020", "06", "19"):
                    assert bit not in c.raw, (
                        f"date fragment {fragment!r} leaked: {c.raw!r}"
                    )

    def test_german_mobile_with_slash_separator_is_recovered(self):
        """``+49175/5604673`` is a real-world German convention where ``/``
        separates the prefix from the subscriber number. libphonenumber
        doesn't accept ``/`` as a separator, so the ``_PHONE_SLASH_BRIDGE_RE``
        substitution rewrites it to a space inside ``+``-anchored
        substrings before the matcher runs.
        """
        html = "<p>Tel.: +49175/5604673 (mobile)</p>"
        contacts = list(extract_phones(html, default_regions=("DE",)))
        values = [c.value for c in contacts]
        assert "+491755604673" in values, values

    def test_slash_bridge_fires_on_bare_phone_shaped_substring(self):
        """Bare ``\\d{2,4}/\\d{6,}`` clusters (no ``+`` country code) are
        phone-shaped and the bridge promotes them too — this is how the
        web-side libphonenumber-js already behaves natively, and the
        Python CLI matches that policy via the bare-slash bridge.
        Case-numbers and dates would have been blanked upstream so the
        bare bridge can't capture them.
        """
        html = "<p>Reach us on 175/5604673 anytime.</p>"
        contacts = list(extract_phones(html, default_regions=("DE",)))
        values = [c.value for c in contacts]
        assert "+491755604673" in values, values

    def test_slash_bridge_bare_does_not_promote_fractions_or_ratios(self):
        """The bare bridge requires a ``2-4 / 6+`` digit split, so short
        slash-separated tokens stay alone.
        """
        for fragment in ("1/2", "3/4", "15/30", "Score 5/7", "Page 2/10"):
            html = f"<p>{fragment} something</p>"
            contacts = list(extract_phones(html, default_regions=("DE",)))
            assert contacts == [], f"{fragment!r} promoted: {[c.value for c in contacts]}"

    def test_business_registration_with_marker_not_misread_as_phone(self):
        """Digit runs preceded by a known registration / tax / VAT marker
        (UEN, ACRA, Reg. No., HRB, VAT, USt-IdNr., INN, ИНН, EIN, CIN, …)
        are never phones. ``_REG_NUM_RE`` blanks them.
        """
        cases = [
            ("Singapore UEN 200604351", "200604351"),
            ("ACRA: 200604351", "200604351"),
            ("Reg. No. 12345678", "12345678"),
            ("Co. Reg. No. 200604351E".replace("E", ""), "200604351"),
            ("HRB 169227", "169227"),
            ("Handelsregister 169227", "169227"),
            ("VAT No. DE259047752", "259047752"),
            ("USt-IdNr. DE259047752", "259047752"),
            ("INN 7707083893", "7707083893"),
            ("ИНН 7707083893", "7707083893"),
            ("ОГРН 1027700132195", "1027700132195"),
            ("EIN: 12-3456789", "3456789"),
            ("CIN: U72200KA2003PTC031672", "031672"),
            ("NIP 5252344078", "5252344078"),
            ("KvK 34244090", "34244090"),
        ]
        for fragment, digits in cases:
            html = f"<p>Company info: {fragment}</p>"
            contacts = list(extract_phones(
                html, default_regions=("DE", "RU", "SG", "US"),
            ))
            for c in contacts:
                assert digits not in c.value, (
                    f"reg-num {fragment!r} leaked as phone {c.value!r}"
                )

    def test_singapore_uen_not_misread_on_wirecard_imprint(self):
        """Specific wirecard /asia/imprint case the user flagged:
        a Singapore UEN ``200604351-D`` (with trailing check letter)
        formerly extracted as a phone. Marker on the real page is
        ``Company Number:`` (not ``Reg. No.``), so the marker list must
        include the company-number variant too.
        """
        html = (
            "<p>Wirecard Asia Pacific Pte. Ltd.</p>"
            "<p>Company Number: 200604351-D</p>"
            "<p>Main Line: +65 6536 8846 Fax: +65 6536 3362</p>"
        )
        contacts = list(extract_phones(html, default_regions=("SG", "DE", "RU")))
        values = [c.value for c in contacts]
        assert "+6565368846" in values, values
        assert "+6565363362" in values, values
        for c in contacts:
            assert "200604351" not in c.value, (
                f"UEN leaked as phone: {c.value!r}"
            )

    def test_wkn_and_other_securities_ids_not_misread_as_phone(self):
        """German press releases often list ``ISIN`` next to ``WKN`` (6-digit
        Wertpapierkennnummer) and other securities IDs (CUSIP, SEDOL, FIGI).
        The 6-digit WKN value is short enough for libphonenumber to claim
        it as a German landline; marker-anchored blanking kills it.
        """
        html = (
            "<p>ISIN: DE0007472060 WKN: 747206 Indices: DAX, TecDAX</p>"
            "<p>CUSIP: 037833100 SEDOL: 2046251 FIGI: BBG000B9XRY4</p>"
            "<p>Tel.: +49 89 12345678</p>"
        )
        contacts = list(extract_phones(html, default_regions=("DE", "RU")))
        values = [c.value for c in contacts]
        # The real phone must still come through.
        assert any(v.startswith("+4989") for v in values), values
        # No securities-ID digit run should appear.
        for forbidden in ("747206", "037833100", "2046251"):
            for v in values:
                assert forbidden not in v, (
                    f"securities id {forbidden!r} leaked as phone: {v!r}"
                )

    def test_isin_digit_run_not_misread_as_phone(self):
        """``ISIN DE0007472060`` (12-char security identifier) leaks a
        7-digit substring that libphonenumber claims as a German landline.
        ``_ISIN_RE`` blanks the whole token regardless of marker presence.
        """
        for fragment in (
            "ISIN DE0007472060",
            "ISIN: DE0007472060 Reuters: WDI.GDE",
            "Tracked under DE0007472060 on Xetra.",   # bare ISIN, no marker
        ):
            html = f"<p>{fragment}</p>"
            contacts = list(extract_phones(html, default_regions=("DE", "RU")))
            for c in contacts:
                assert "747206" not in c.value, (
                    f"ISIN digit leaked from {fragment!r}: {c.value!r}"
                )

    def test_german_postal_address_not_misread_as_phone(self):
        """``Einsteinring 35 85609 Aschheim`` — German address inline.
        libphonenumber-DE claims the house-number + postal-code pair as
        a 7-digit landline. ``_GERMAN_POSTAL_ADDR_RE`` blanks the pair
        when followed by a capitalised city name.
        """
        html = (
            "<p>Wirecard AG, Einsteinring 35 85609 Aschheim b. München, Germany</p>"
            "<p>Phone: +49 (0)89-4424 1400</p>"
        )
        contacts = list(extract_phones(html, default_regions=("DE",)))
        values = [c.value for c in contacts]
        assert "+498944241400" in values, values
        for c in contacts:
            assert "85609" not in c.value, (
                f"German postal code leaked as phone: {c.value!r}"
            )
            assert "+4935" not in c.value, c.value

    def test_slash_bridge_and_case_number_coexist(self):
        """When a page mixes a slash-separated phone with a slash-separated
        case-number, the case-number must be blanked (no phone leaked from
        it) AND the phone must come through clean.
        """
        html = (
            "<p>Case 596/2014. Reachable on +49175/5604673 anytime.</p>"
        )
        contacts = list(extract_phones(html, default_regions=("DE",)))
        values = [c.value for c in contacts]
        assert "+491755604673" in values, values
        for c in contacts:
            assert "596" not in c.raw, c.raw

    def test_geo_coordinates_not_misread_as_phones(self):
        """Geographic coordinates like ``(37.476600, -122.144000)`` have
        the right digit profile to be claimed as German phones when ``DE``
        is in the default region list (which it always is for ``.com``
        sites — US first, DE later in the fallback chain). The ≥4-decimal
        ``_COORD_RE`` blanks them out before matching.
        """
        html = (
            "<p>Office at (37.476600, -122.144000) in Palo Alto.</p>"
            "<p>Moscow: (55.7558, 37.6173)</p>"
            "<p>Reach us at +1 (650) 555-1234</p>"
        )
        contacts = list(extract_phones(html, default_regions=("US", "RU", "DE")))
        values = [c.value for c in contacts]
        raws = [c.raw for c in contacts]
        # The real US phone must come through.
        assert "+16505551234" in values
        # No phone derived from coordinate digit groups.
        for forbidden_prefix in ("+4937476", "+49122", "+49557", "+4937617"):
            assert not any(v.startswith(forbidden_prefix) for v in values), (
                f"coordinate leaked as phone (prefix {forbidden_prefix}): {values}"
            )
        # And the raw display can't show a coordinate fragment.
        for raw in raws:
            assert "37.476600" not in raw and "-122" not in raw, (
                f"coordinate fragment in phone raw: {raw!r}"
            )

    def test_dot_formatted_us_phone_survives_coord_filter(self):
        """``555.123.4567`` is a legitimate US phone written with dot
        separators — the coord-blanking lookarounds must NOT chop it.
        """
        html = '<p>Call us at +1 555.123.4567 anytime.</p>'
        contacts = list(extract_phones(html, default_regions=("US",)))
        values = [c.value for c in contacts]
        assert "+15551234567" in values, values

    def test_google_tracking_ids_not_misread_as_phones(self):
        """Google Universal Analytics IDs like ``UA-36441709-1`` and Ads
        IDs like ``AW-1234567890`` have phone-shaped digit runs that
        ``PhoneNumberMatcher`` claims as Ukrainian numbers when the default
        region is ``UA`` (libphonenumber's region code for Ukraine, which
        is exactly what our ccTLD prioritiser puts first for ``.ua`` sites).
        ``_TRACKING_ID_RE`` blanks them out before matching runs.
        """
        html = (
            "<script>"
            "_gaq.push(['_setAccount','UA-36441709-1']);"
            "gtag('config','AW-1234567890');"
            "</script>"
            "<p>Real phone: +380 44 234 5678</p>"
        )
        contacts = list(extract_phones(html, default_regions=("UA", "RU", "US")))
        values = [c.value for c in contacts]
        # The real Ukrainian number must still come through.
        assert "+380442345678" in values
        # The tracking-ID-derived phantoms must not.
        assert "+380364417091" not in values, (
            "UA-36441709-1 leaked as a phone number"
        )
        for v in values:
            assert "1234567890" not in v, f"AW-1234567890 leaked: {v}"

    def test_two_digit_year_dates_remain_ambiguous_and_pass_through(self):
        """``02.09.08`` is genuinely ambiguous (date vs ID vs short number).
        Per the project's "completeness over filtering" rule, we only filter
        the unambiguous 4-digit-year form — analyst sorts the rest.
        """
        # We don't assert the date IS surfaced (libphonenumber might or might
        # not match a 6-digit run depending on region). We only assert the
        # filter doesn't go further than 4-digit-year dates.
        from kronikier.extractors import _looks_like_date
        assert _looks_like_date("02.09.2008")
        assert _looks_like_date("2008-09-02")
        assert _looks_like_date("9/2/2008")
        # Two-digit year: NOT filtered.
        assert not _looks_like_date("02.09.08")
        # Real phone formats: NOT filtered.
        assert not _looks_like_date("+375-29-722-84-40")
        assert not _looks_like_date("8(0162)51-12-54")
        assert not _looks_like_date("(212) 555-1234")

    def test_postal_codes_and_tax_ids_are_not_misread(self):
        """Pass 2 (bare locals + region) must stay STRICT — otherwise BY
        postal codes (225006, 224022), UNP (290506581) and OKPO codes get
        misinterpreted as phones.
        """
        html = (
            "<p>Республика Беларусь, 225006, Брестская область</p>"
            "<p>УНП 290506581 ОКПО 297459331000</p>"
            "<p>ул. Суворова, д.96/2</p>"
        )
        phones = [c.value for c in extract_phones(html, default_regions=("BY", "RU"))]
        # None of these IDs should leak into the phone results.
        for noise in ("+375225006", "+375290506581", "+375297459331000",
                      "+375224022", "+4960", "+49962"):
            assert noise not in phones, f"misread identifier: {noise}"

    def test_tel_href(self):
        html = '<a href="tel:+442071838750">London office</a>'
        phones = [c.value for c in extract_phones(html)]
        assert "+442071838750" in phones

    def test_dedup_same_number_different_formats(self):
        html = """
        <a href="tel:+74951234567">+7 (495) 123-45-67</a>
        Also: +7 495 1234567
        Also: 8 (495) 123-45-67
        """
        phones = [c.value for c in extract_phones(html, default_regions=("RU",))]
        # All four spellings collapse to the same E.164
        assert phones.count("+74951234567") == 1

    def test_no_random_number_misread(self):
        # Order number, not a phone.
        html = "<p>Order #12345 placed on 2007-01-15.</p>"
        phones = [c.value for c in extract_phones(html, default_regions=("RU",))]
        assert phones == []


# ---------------------------------------------------------------------------
# Combined / fixtures
# ---------------------------------------------------------------------------


class TestFixtures:
    def test_russian_2007_page(self, load_fixture):
        html = load_fixture("contacts_ru_2007.html")
        contacts = extract_contacts(html)
        emails = {c.value for c in contacts if c.kind == "email"}
        phones = {c.value for c in contacts if c.kind == "phone"}

        assert emails == {
            "info@romashka-llc.ru",
            "buh@romashka-llc.ru",
            "director@romashka-llc.ru",
            "support@romashka-llc.ru",
            "hello@romashka-llc.ru",
        }
        # Office, fax, mobile all present
        assert "+74951234567" in phones
        assert "+74951234568" in phones
        assert "+79165551234" in phones

    def test_us_2003_page(self, load_fixture):
        html = load_fixture("contacts_us_2003.html")
        contacts = extract_contacts(html, default_regions=("US",))
        emails = {c.value for c in contacts if c.kind == "email"}
        phones = {c.value for c in contacts if c.kind == "phone"}

        assert "hello@acmewidgets.com" in emails
        assert "webmaster@acmewidgets.com" in emails
        assert "ceo@acmewidgets.com" in emails  # cloudflare-decoded
        # Image src "logo@2x.png" must NOT be detected as email
        assert not any(".png" in e for e in emails)

        assert "+12175550192" in phones
        assert "+18005550199" in phones
        assert "+12175550193" in phones

    def test_modern_empty_page(self, load_fixture):
        html = load_fixture("empty_footer_2024.html")
        contacts = extract_contacts(html, default_regions=("US",))
        assert contacts == []


# ---------------------------------------------------------------------------
# Contact dataclass
# ---------------------------------------------------------------------------


def test_contact_is_hashable_and_frozen():
    c = Contact(kind="email", value="a@b.com", raw="a@b.com")
    {c}  # hashable
    with pytest.raises(Exception):
        c.value = "x@y.com"  # type: ignore[misc]
