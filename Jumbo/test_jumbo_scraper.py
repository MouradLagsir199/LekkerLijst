import unittest
from unittest.mock import AsyncMock, patch

import jumbo_scraper


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class JumboRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_a_transient_graphql_error(self) -> None:
        client = AsyncMock()
        client.post.side_effect = [
            FakeResponse({"errors": [{"message": "504: Gateway Timeout"}]}),
            FakeResponse({"data": {"searchProducts": {"products": []}}}),
        ]

        with patch("jumbo_scraper.asyncio.sleep", new=AsyncMock()):
            result = await jumbo_scraper.graphql_request(
                client,
                operation_name="SearchMobileProducts",
                query="query test { ok }",
                variables={},
                retries=2,
            )

        self.assertEqual(result["data"]["searchProducts"]["products"], [])
        self.assertEqual(client.post.await_count, 2)


if __name__ == "__main__":
    unittest.main()
