"""False rejections of the Summariser's checks (25 Oct 2026, from the items left 'failed' on the site).

The checks still refuse what they are for: a name or number the sources do not hold, the publisher credited as the
reporter, a sentence-count-free rule set that still limits words."""

from aw_workers import merge, summary_dates, summary_rewrite, summary_rules

# ---- sentence-start words are not names ----


def test_ordinary_words_opening_a_sentence_are_not_names():
    src = "The notice asks recognised organisations to apply for renewal by 9 Oct 2026 with the fee."
    for opener in ("If", "Additional", "Because", "Designated", "Interested", "Issued", "Failure", "Compliance", "Me"):
        written = f"The notice asks organisations to apply for renewal. {opener} organisations apply late, the fee stays."
        assert not any("names not in" in p for p in merge._unsourced(written, src)), opener


def test_a_real_unsourced_name_is_still_refused_at_a_sentence_start_and_inside():
    src = "Police rescued 14 dogs in Kondhwa, Pune."
    assert "names not in the reports: Rakesh" in merge._unsourced("Rakesh rescued 14 dogs in Pune.", src)
    assert "names not in the reports: Mumbai" in merge._unsourced("Police rescued 14 dogs in Mumbai.", src)


def test_a_capital_word_mid_sentence_is_still_a_name():
    src = "Police rescued 14 dogs in Pune."
    assert any("Additional" in p for p in merge._unsourced("Police and the Additional Commissioner rescued 14 dogs.", src))


# ---- acronym and its expansion are one name ----


def test_expansion_of_an_acronym_in_the_source_is_sourced():
    src = "AWBI issued the advisory to all states on 3 Oct 2026."
    written = "The Animal Welfare Board of India issued the advisory to all states on 3 Oct 2026."
    assert merge._unsourced(written, src) == []


def test_acronym_of_a_name_spelled_out_in_the_source_is_sourced():
    src = "The Bharatiya Nagarik Suraksha Sanhita applies."
    assert merge._unsourced("Police acted under the BNSS.", src) == []


def test_a_name_that_does_not_spell_the_acronym_is_still_refused():
    src = "AWBI issued the advisory."
    assert any("Council" in p for p in merge._unsourced("The Animal Welfare Council of India issued it.", src))


def test_possessive_with_either_apostrophe():
    src = "The Bengaluru East City Corporation's Adopt Me campaign placed dogs."
    assert merge._unsourced("The Corporation’s Adopt Me campaign placed dogs.", src) == []


def test_the_issuer_counts_as_sourced_for_a_library_rewrite():
    row = {"title": "Animal Birth Control Rules, 2023", "publisher": "Animal Welfare Board of India", "summary": ""}
    text = "rules for sterilisation and vaccination of community dogs " * 3
    s = "The Animal Welfare Board of India published rules for sterilisation and vaccination of community dogs."
    hard, _ = summary_rewrite.evaluate(s, "", row, text, "library", [])
    assert not any("names not in" in p for p in hard)


# ---- number words and digits are one number ----


def test_number_in_words_in_the_source_matches_digits():
    src = "Officials said four dogs were adopted on the spot. The event saw 17 animal rescuers."
    assert merge._unsourced("Officials said 4 dogs were adopted. Seventeen animal rescuers took part.", src) == []


def test_a_wrong_number_word_is_still_refused():
    src = "The event saw 17 animal rescuers."
    assert any("numbers not in the reports: 18" in p for p in merge._unsourced("Eighteen rescuers took part.", src))


def test_leading_zero_and_dotted_dates():
    src = ("Apply on or before 09 October 2026. Earlier notice dated 15.05.2024. A dog was run over on 04-12-2002. "
           + summary_rewrite._MONTHS)  # the rewrite adds the month names to what counts as sourced
    assert merge._unsourced("Apply by 9 Oct 2026. The earlier notice was dated 15 May 2024. The dog died on 4 Dec 2002.",
                            src) == []
    assert any("31" in p for p in merge._unsourced("The study had 31 participants.", src))


def test_digits_inside_a_name_are_not_a_number():
    row = {"title": "t", "publisher": "P", "summary": ""}
    ours = "A buffalo worth Rs 21 crore died at the Pushkar fair, The Indian Express reported, citing News18."
    s = "A buffalo worth Rs 21 crore died at the Pushkar fair in Rajasthan after its health worsened."
    hard, _ = summary_rewrite.evaluate(s, ours, row, s, "news", [])
    assert not any("left out" in p for p in hard)
    assert "numbers not in the reports: 18" in merge._unsourced("Police counted 18 dogs.", "News18 covered it.")


def test_numbers_of_the_current_summary_kept_in_words():
    row = {"title": "t", "publisher": "P", "summary": ""}
    ours = "Police rescued 4 dogs in Pune."
    hard, _ = summary_rewrite.evaluate("Police rescued four dogs in Pune on the spot.", ours, row,
                                       "Police rescued four dogs in Pune on the spot.", "news", [])
    assert not any("left out" in p for p in hard)


# ---- the publisher as the story's actor ----


PETA = "PETA India"


def test_publisher_that_acted_may_be_named_and_quoted():
    title = "Illegal ram fight event cancelled following PETA India intervention in Bhopal"
    s = ("An illegal ram fight in Bhopal district was cancelled following intervention by PETA India. "
         "PETA India stated that inciting animals to fight violates the Prevention of Cruelty to Animals Act, 1960.")
    assert not summary_rules.names_publisher_as_source(s, PETA, title)
    assert not any("names the publisher" in p for p in summary_rules.problems(s, "news", publisher=PETA, context=title))
    # actor in the summary itself, even without the title
    assert not summary_rules.names_publisher_as_source(s, PETA, "")


def test_publisher_only_credited_as_reporter_is_still_refused():
    s = "Police rescued 14 dogs in Pune, The Second Paper said. The owner was booked."
    assert summary_rules.names_publisher_as_source(s, "The Second Paper", "Dogs rescued in Pune")
    s2 = "According to a report by PETA India, the circus kept a goat."
    assert summary_rules.names_publisher_as_source(s2, PETA, "Goat rescued from circus following PETA India action")
    s3 = "PETA India reported that the circus kept a goat."
    assert summary_rules.names_publisher_as_source(s3, PETA, "Goat rescued following PETA India intervention")


# ---- sentence counts are no rule ----


def test_sentence_counts_are_not_checked_and_site_values_are_ignored():
    rules = summary_rules.merged({"sections": {"news": {"min_words": 5, "max_words": 60, "min_sentences": 4,
                                                        "max_sentences": 4}}})
    assert "min_sentences" not in rules["sections"]["news"] and "max_sentences" not in rules["sections"]["news"]
    one = "Police rescued 14 dogs from an illegal breeding unit in Kondhwa, Pune, on Monday."
    many = "Police acted. Dogs were rescued. The owner was booked. A case was filed. Vets checked them. All are well."
    for text in (one, many):
        assert not any("sentences" in p for p in summary_rules.problems(text, "news", rules=rules))
    assert "sentences" not in summary_rules.rules_block("news", rules).split("The lead")[0]
    assert all("min_sentences" not in s for s in summary_rules.DEFAULTS["sections"].values())


# ---- sources read wrongly ----


def test_mojibake_is_repaired():
    raw = "Bengaluru East City Corporation’s Adopt Me—Second Innings".encode("utf-8").decode("latin-1")
    assert summary_dates.fix_mojibake(raw) == "Bengaluru East City Corporation’s Adopt Me—Second Innings"
    assert summary_dates.fix_mojibake("Plain text, café.") == "Plain text, café."


def test_a_pdf_of_glyph_codes_is_no_source():
    assert summary_rewrite.glyph_codes("/g69/g75/g100/g47/g38/g47/ " * 10)
    assert not summary_rewrite.glyph_codes("The Board directs all states to act within 30 days. " * 20)
