"""Site resolution for footage holds: which Aware site a request names.

A false match narrows a hold to the wrong building and lets the right
building's footage age out, so these tests pin the look-alike codes the
brief warns about (HBH is Henderson Bay, HHE is Harbor Heights) and the
words that must never resolve to a site on their own.
"""
from django.test import SimpleTestCase

from apps.records import footage_sites


def codes(text: str) -> list[str]:
    return [site.code for site in footage_sites.resolve_sites(text)]


class ResolveSitesTests(SimpleTestCase):
    def test_harbor_heights_full_name_is_hhe_not_hbh(self):
        self.assertEqual(
            codes("All video from Harbor Heights Elementary on 9/22/2026"),
            ["HHE"],
        )

    def test_hhes_abbreviation_is_hhe(self):
        self.assertEqual(codes("camera footage at HHES on 2026-09-22"), ["HHE"])

    def test_henderson_bay_is_hbh(self):
        self.assertEqual(codes("video from Henderson Bay on 2026-09-22"), ["HBH"])

    def test_two_named_schools_resolve_to_both(self):
        self.assertEqual(
            codes(
                "Footage from Peninsula High School and Harbor Ridge Middle "
                "School on 2026-09-22"
            ),
            ["PHS", "HRM"],
        )

    def test_no_site_named_resolves_to_nothing(self):
        self.assertEqual(codes("all camera footage of the bus loop yesterday"), [])

    def test_matching_is_case_insensitive_for_names(self):
        self.assertEqual(codes("video at HARBOR HEIGHTS elementary"), ["HHE"])

    def test_names_match_whole_words_only(self):
        # "Purdy Elementary" inside a longer word must not match.
        self.assertEqual(codes("video at XPurdy Elementaryville"), [])

    def test_district_and_region_words_never_resolve_a_site(self):
        # Nearly every request names the district or the city; a region or
        # city alone is not a building.
        for text in (
            "Peninsula School District camera footage",
            "video recorded in Gig Harbor, WA",
            "camera footage from somewhere on the Key Peninsula",
        ):
            with self.subTest(text=text):
                self.assertEqual(codes(text), [])

    def test_key_peninsula_middle_is_kpm_and_peninsula_high_is_phs(self):
        self.assertEqual(codes("Key Peninsula Middle School video"), ["KPM"])
        self.assertEqual(codes("Peninsula High School video"), ["PHS"])

    def test_abbreviations_must_be_written_in_capitals(self):
        # "pes", "mes", "des" are ordinary words in other languages and
        # names; only the capitalized code is read as a site.
        self.assertEqual(codes("video of the mes de septiembre"), [])
        self.assertEqual(codes("video at MES"), ["MES"])

    def test_each_site_reported_once(self):
        self.assertEqual(
            codes("PHS footage, Peninsula High School gym, Peninsula High lot"),
            ["PHS"],
        )

    def test_unconfirmed_codes_are_not_in_the_alias_table(self):
        # DCR and TSD exist as Aware groups but no cited source names their
        # building; they are held only by a district-wide hold.
        table_codes = {site.code for site in footage_sites.SITES}
        self.assertNotIn("DCR", table_codes)
        self.assertNotIn("TSD", table_codes)

    def test_every_table_code_is_unique(self):
        table_codes = [site.code for site in footage_sites.SITES]
        self.assertEqual(len(table_codes), len(set(table_codes)))

    def test_site_name_lookup(self):
        self.assertEqual(
            footage_sites.site_name("HHE"), "Harbor Heights Elementary School"
        )
        self.assertEqual(footage_sites.site_name("DCR"), "DCR")
