"""Supermarket catalog scrapers feeding the medallion bronze layer.

Each store module (ah, jumbo, dirk, plus, spar, aldi) reverse-engineers that
retailer's API and writes raw product objects to a JSONL artifact. The
``bronze_ingest`` module then loads an artifact into ``catalog.bronze_products``.
Silver and gold transforms happen later, downstream of bronze.
"""
