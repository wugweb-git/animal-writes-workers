from aw_workers.sources import not_an_article


def test_tag_page_is_not_an_article():
    assert not_an_article("https://telanganatoday.com/tag/peta-india", "peta-india")


def test_category_and_author_pages():
    assert not_an_article("https://example.in/category/wildlife/", "Wildlife news")
    assert not_an_article("https://example.in/author/jane/", "Jane")


def test_slug_title_alone():
    assert not_an_article("https://example.in/news/123", '"peta-india"')


def test_real_story_passes():
    assert not_an_article("https://telanganatoday.com/peta-india-gifts-robotic-elephant-to-zoo", "PETA India gifts robotic elephant to Nehru Zoo") == ""
    assert not_an_article("https://example.in/news/anti-rabies-drive", "Anti-rabies drive in Pune") == ""


def test_videos_viral_posts_and_listicles_are_not_news():
    assert not_an_article("https://www.shiksha.com/science/articles/top-vet", "Top Veterinary Science Universities in India: Check List of Institutes")
    assert not_an_article("https://www.bangkokpost.com/world/1/x", "India's fat dog vote goes viral")
    assert not_an_article("https://www.msn.com/en-in/news/other/watch-x/vi-AA20fyxS", "Watch these small scared rabbits get a cozy shelter")
    assert not_an_article("https://www.britannica.com/video/x/-363460", "Video of Can We Protect Endangered Wildlife in India?")
    assert not_an_article("https://example.in/x", "Veterinary college students protest fee hike in Mhow") == ""
    assert not_an_article("https://example.in/x", "Watchman bitten by stray dog in Pune") == ""


def test_press_release_headlines_are_held():
    from aw_workers.sources import press_release_headline as pr

    assert pr("https://www.petaindia.com/blog/fir", "FIR Registered for Sexual Abuse of a Female Dog in Bhojpur to Protect ALL Females, Following PETA India Intervention")
    assert pr("https://www.prnewswire.com/news-releases/x", "Wildlife SOS announces National Sloth Bear Symposium")
    assert pr("https://www.petaindia.com/blog/x", "Over 300 Birds Rescued From Howrah to CSMT Train by PETA India") == ""
    assert pr("https://www.thehindu.com/news/x", "FIR filed after PETA India complaint, ALL accused named") == ""
