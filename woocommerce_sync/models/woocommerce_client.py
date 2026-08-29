"""Thin WooCommerce REST API adapter: connection handling, pagination and rate-limit/backoff retry. Contains no Odoo ORM logic, so it can be unit-tested and reused independently of the connector model."""

from __future__ import annotations

import logging
import time
from collections.abc import Generator
from typing import Any

import requests
from woocommerce import API

logger = logging.getLogger(__name__)


class WooCommerceClient:
    """Adapter around the 'woocommerce' REST API client used by the connector model."""

    def __init__(self, url: str, consumer_key: str, consumer_secret: str, timeout: int, user_agent: str = 'Odoo-Woocommerce Sync', test_mode: bool = False) -> None:
        self.test_mode = test_mode
        self.api = API(
            url=url,
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            version='wc/v3',
            timeout=timeout,
            user_agent=user_agent,
        )

    # Passthrough methods for direct/one-off calls (settings retrieval, single record updates, etc.)
    def get(self, endpoint: str, params: dict[str, Any] | None = None):
        return self.api.get(endpoint=endpoint, params=params)

    def put(self, endpoint: str, data: dict[str, Any]):
        return self.api.put(endpoint=endpoint, data=data)

    def post(self, endpoint: str, data: dict[str, Any]):
        return self.api.post(endpoint=endpoint, data=data)

    def delete(self, endpoint: str, params: dict[str, Any] | None = None):
        return self.api.delete(endpoint=endpoint, params=params)

    def batch(self, endpoint: str, create: list[dict[str, Any]] | None = None, update: list[dict[str, Any]] | None = None, delete: list[int] | None = None, max_retries: int = 5) -> dict[str, Any]:
        """Performs a single WooCommerce REST API 'batch' request (e.g. 'products/batch', 'products/{id}/variations/batch') with the same rate-limit/transient-error retry/backoff as 'request()'. Each of 'create'/'update' can hold up to 100 items per WooCommerce's own batch endpoint limit."""
        data = {key: value for key, value in {'create': create, 'update': update, 'delete': delete}.items() if value}
        if not data:
            return {}

        attempt = 0
        while True:
            try:
                response = self.api.post(endpoint=endpoint, data=data)
            except requests.RequestException as error:
                attempt += 1
                if attempt > max_retries:
                    logger.error(f'WooCommerce REST API batch request failed for {endpoint} after {attempt} attempts: {error}')
                    raise
                wait_seconds = min(2**attempt, 60)
                logger.warning(f'WooCommerce REST API batch request error for {endpoint} (attempt {attempt}/{max_retries}): {error}. Retrying in {wait_seconds}s.')
                time.sleep(wait_seconds)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                attempt += 1
                if attempt > max_retries:
                    logger.error(f'WooCommerce REST API batch request for {endpoint} still failing (HTTP {response.status_code}) after {attempt} attempts. Giving up.')
                    response.raise_for_status()

                retry_after = response.headers.get('Retry-After')
                wait_seconds = float(retry_after) if retry_after and retry_after.isdigit() else min(2**attempt, 60)
                logger.warning(f'WooCommerce REST API batch rate-limited/unavailable for {endpoint} (HTTP {response.status_code}, attempt {attempt}/{max_retries}). Retrying in {wait_seconds}s.')
                time.sleep(wait_seconds)
                continue

            return response.json()

    @staticmethod
    def list_chunks(items: list, chunk_size: int) -> Generator[list, Any, None]:
        """Splits 'items' into consecutive chunks of at most 'chunk_size' elements (WooCommerce batch endpoints accept up to 100 items per call)."""
        for index in range(0, len(items), chunk_size):
            yield items[index : index + chunk_size]

    def validate_connection(self) -> bool:
        """Pings the 'system_status' endpoint to confirm the REST API credentials/URL are valid and reachable."""
        try:
            response = self.api.get(endpoint='system_status')
            response.raise_for_status()

        except requests.RequestException as error:
            logger.error(f'WooCommerce REST API connection failed: {error}')
            return False

        logger.info('WooCommerce REST API connection successful')
        return True

    def request(self, endpoint: str, params: dict[str, Any] | None = None, max_retries: int = 5) -> Any:
        """Performs a single GET request with rate-limit (HTTP 429) and transient server error (5xx) retry/backoff handling."""
        attempt = 0
        while True:
            try:
                response = self.api.get(endpoint=endpoint, params=params)
            except requests.RequestException as error:
                attempt += 1
                if attempt > max_retries:
                    logger.error(f'WooCommerce REST API request failed for {endpoint} after {attempt} attempts: {error}')
                    raise
                wait_seconds = min(2**attempt, 60)
                logger.warning(f'WooCommerce REST API request error for {endpoint} (attempt {attempt}/{max_retries}): {error}. Retrying in {wait_seconds}s.')
                time.sleep(wait_seconds)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                attempt += 1
                if attempt > max_retries:
                    logger.error(f'WooCommerce REST API request for {endpoint} still failing (HTTP {response.status_code}) after {attempt} attempts. Giving up.')
                    response.raise_for_status()

                retry_after = response.headers.get('Retry-After')
                wait_seconds = float(retry_after) if retry_after and retry_after.isdigit() else min(2**attempt, 60)
                logger.warning(f'WooCommerce REST API rate-limited/unavailable for {endpoint} (HTTP {response.status_code}, attempt {attempt}/{max_retries}). Retrying in {wait_seconds}s.')
                time.sleep(wait_seconds)
                continue

            return response.json()

    def get_all_items(self, endpoint: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Fetches all pages of 'endpoint' and returns them as a single flat list."""
        if params is None:
            params = {}
        params.setdefault('per_page', 100)

        if self.test_mode:
            params['per_page'] = 10

        records_all = []
        page = 1
        while True:
            params['page'] = page
            records = self.request(endpoint=endpoint, params=params)

            if not isinstance(records, list):
                logger.error(f'WooCommerce REST API error for {endpoint}: {records}')
                break

            records_all.extend(records)

            if not records or self.test_mode:
                break

            page += 1

        return records_all

    def get_items_in_batches(self, endpoint: str, params: dict[str, Any] | None = None, batch_size: int = 10) -> Generator[list[dict[str, Any]], Any, None]:
        """Fetches 'endpoint' page by page, yielding each page as soon as it is retrieved instead of materializing the whole result set in memory."""
        if params is None:
            params = {}
        params['per_page'] = batch_size

        if self.test_mode:
            params['per_page'] = 10

        page = 1
        while True:
            params['page'] = page
            records = self.request(endpoint=endpoint, params=params)

            if not isinstance(records, list):
                logger.error(f'WooCommerce REST API error for {endpoint}: {records}')
                break

            if records:
                yield records
            else:
                break

            if self.test_mode:
                break

            page += 1
