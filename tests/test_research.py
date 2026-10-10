from aw_workers.research import relevant


def test_india_dog_sterilisation_kept():
    assert relevant("Modelling sterilisation strategies for free-roaming dog populations in southern India", "")


def test_human_sterilisation_dropped():
    assert not relevant("Complications of Female Sterilization: A Hospital-Based Retrospective Study",
                        "A study at a medical college in Bihar, India, of 80 women.")
    assert not relevant("Estimating gender disparities in surgical sterilization uptake in India in 2019-20", "")
    assert not relevant("Steam sterilization: Review of autoclaves, validation, instrument packing", "About 4% of people in India need surgery.")
    assert not relevant("A Bibliometric Analysis of Lesion Sterilization and Tissue Repair in Primary Teeth", "Studies from India and Brazil.")
    assert not relevant("Clusters of postoperative endophthalmitis: diagnostic and sterilization challenges", "A tertiary eye hospital in India.")


def test_affiliation_alone_is_not_india():
    assert not relevant("Rabies in dogs: a review", "A global review.", affiliation="AIIMS, New Delhi, India")


def test_other_country_in_title_dropped():
    assert not relevant("Rabies in Morocco: A Preventable Tragedy", "Deaths in Africa and South Asia including India.")
    assert not relevant("Pencegahan Rabies di Bali melalui Pendekatan Sosio-Kultural", "Compared with India.")


def test_india_in_abstract_kept():
    assert relevant("Dog bite patterns at a rural health training centre", "A study in Bidar, Karnataka.")
