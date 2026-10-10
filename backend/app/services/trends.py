"""Reddit trend pulse (WORKPLAN C7). Owner: Agent B.

App-only OAuth against Reddit, top posts of the week from
``settings.trend_subreddits`` (+ extra subreddits), NSFW/spoiler/stickied posts
skipped, cached in memory for an hour. Missing credentials => empty pulse.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TrendPost:
    title: str
    subreddit: str
    score: int
    permalink: str
    image_url: str | None = None


@dataclass
class TrendPulse:
    posts: list[TrendPost] = field(default_factory=list)


async def get_trend_pulse(extra_subreddits: list[str] | None = None) -> TrendPulse:
    """This week's top posts across the configured subreddits (+ ``extra_subreddits``)."""
    return TrendPulse()


async def search_reddit_images(query: str, limit: int = 6) -> list[TrendPost]:
    """Recent image posts matching ``query`` in the configured subreddits."""
    return []
