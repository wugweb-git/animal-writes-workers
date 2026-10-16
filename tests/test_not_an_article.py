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
