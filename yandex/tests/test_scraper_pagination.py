import asyncio
from unittest.mock import AsyncMock, patch

from yandex.app.scraper import (
    ListingCandidate,
    YandexMarketScraper,
    search_page_url,
)


def candidate(number: int, *, foreign: bool = False) -> ListingCandidate:
    return ListingCandidate(
        url=f"https://market.yandex.ru/product--test/{number}",
        name=f"商品 {number}",
        price=100.0,
        old_price=None,
        image="",
        card_text=("商品 карточка из-за рубежа" if foreign else "商品 карточка"),
        foreign_evidence=("из-за рубежа" if foreign else ""),
        raw={},
    )


def test_search_page_url_replaces_existing_page_and_preserves_keyword():
    first = "https://market.yandex.ru/search?text=action%20camera"
    later = "https://market.yandex.ru/search?text=action%20camera&page=7"

    assert search_page_url(first, 2).endswith("text=action+camera&page=2")
    assert search_page_url(later, 3).endswith("text=action+camera&page=3")
    assert search_page_url(later, 1).endswith("text=action+camera")


class FakeLazyPage:
    def __init__(self):
        self.metric_calls = 0
        self.scroll_calls = []

    async def evaluate(self, expression, arg=None):
        if "return {" not in expression:
            self.scroll_calls.append((expression, arg))
            return None
        self.metric_calls += 1
        metrics = (
            {"top": 0, "height": 2200, "viewport": 800, "productLinks": 1},
            {"top": 700, "height": 2600, "viewport": 800},
            {"top": 1900, "height": 2600, "viewport": 800, "productLinks": 2},
            {"top": 1900, "height": 2600, "viewport": 800},
        )
        return metrics[min(self.metric_calls - 1, len(metrics) - 1)]

    async def wait_for_function(self, *args, **kwargs):
        return True

    async def wait_for_timeout(self, _milliseconds):
        return None


def test_lazy_loader_accumulates_cards_removed_from_later_dom_snapshots():
    async def run_test():
        scraper = YandexMarketScraper()
        page = FakeLazyPage()
        # Card 1 disappears when the virtual list scrolls; card 3 only appears
        # at the bottom. All three must survive in the cumulative result.
        scraper._extract_listing_candidates = AsyncMock(
            side_effect=[
                [candidate(1)],
                [candidate(1), candidate(2)],
                [candidate(2)],
                [candidate(2), candidate(3, foreign=True)],
            ]
        )

        with patch("yandex.app.scraper.MAX_LAZY_SCROLL_STEPS", 2):
            results = await scraper._collect_lazy_listing_candidates(page)

        assert [item.url.rsplit("/", 1)[-1] for item in results] == ["1", "2", "3"]
        assert results[-1].foreign_evidence == "из-за рубежа"
        assert len(page.scroll_calls) >= 2

    asyncio.run(run_test())
