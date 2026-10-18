"""Sources, one module each. A module has KEY, NAME, HOST and run(ctx, env, http) -> None (adds Facts to ctx and sets
ctx.status[KEY]). A new source is one module plus one entry in REGISTRY (and one row in the site's aw_enrich_sources)."""

from . import awbi, claim, darpan, google_places, osm, social, website, wikidata

# The order they run in: the organisation's own claim, data already held, Google (locator), then the open sources that
# can add a website lead, then the website and its social pages.
REGISTRY = {m.KEY: m for m in (claim, awbi, google_places, wikidata, osm, darpan, website, social)}
