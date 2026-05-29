"""Unit tests for email/phone extraction and deobfuscation."""

from __future__ import annotations

import pytest

from kronieker.extractors import (
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
        from kronieker.pipeline import _regions_for_domain
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
        from kronieker.pipeline import _regions_for_domain
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

    def test_two_digit_year_dates_remain_ambiguous_and_pass_through(self):
        """``02.09.08`` is genuinely ambiguous (date vs ID vs short number).
        Per the project's "completeness over filtering" rule, we only filter
        the unambiguous 4-digit-year form — analyst sorts the rest.
        """
        # We don't assert the date IS surfaced (libphonenumber might or might
        # not match a 6-digit run depending on region). We only assert the
        # filter doesn't go further than 4-digit-year dates.
        from kronieker.extractors import _looks_like_date
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
