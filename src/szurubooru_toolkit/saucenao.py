from __future__ import annotations

import re
from asyncio import sleep
from asyncio.exceptions import TimeoutError
from io import BytesIO
from typing import Any
from typing import Coroutine  # noqa TYP001

import tldextract
from aiohttp.client_exceptions import ContentTypeError
from loguru import logger
from pysaucenao import SauceNao as PySauceNao

from szurubooru_toolkit.config import Config


class SauceNao:
    """Handles everything related to SauceNAO and aggregating the results."""

    def __init__(self, config: Config) -> None:
        """
        Initialize the SauceNAO object.

        This method initializes a SauceNAO object and sets up the client. It uses the provided config object to
        authenticate with the SauceNAO API and set the minimum similarity for the search results. If the API token is not
        'None', it logs that it is using the SauceNao API token.

        Args:
            config (Config): The configuration object containing the SauceNao API token and other settings.
        """

        self.pysaucenao = PySauceNao(api_key=config.auto_tagger['saucenao_api_token'], min_similarity=70.0)
        if not config.auto_tagger['saucenao_api_token'] == 'None':
            logger.debug('Using SauceNAO API token')

        self.retry_attempts = 12
        self.retry_delay = 5

    def get_base_domain(self, url: str) -> str:
        """
        Extracts the base domain from a URL.

        This method extracts the base domain from a URL using the `tldextract` library. It returns the base domain as a
        string.

        Args:
            url (str): The URL from which to extract the base domain.

        Returns:
            str: The base domain extracted from the URL.
        """

        extracted = tldextract.extract(url)
        return extracted.domain

    async def get_metadata(
        self,
        content_url: str,
        image: bytes | None = None,
    ) -> tuple[dict[str, dict[str, int | None] | Any], int, int]:
        """
        Retrieve results from SauceNAO and aggregate all metadata.

        This function extracts image metadata from multiple sources such as pixiv, Danbooru, Gelbooru, Yandere,
        Konachan and Sankaku, using the SauceNAO reverse image search tool. The metadata is then collected and
        returned in a tuple format. The function also takes care of request limit issues, informing about the
        remaining short and long limit.

        Args:
            content_url (str): The URL of the szurubooru content from where metadata needs to be extracted.
                SauceNAO needs to be able to reach this URL.
            image (Optional[bytes], optional): The image data in bytes format, if available. Defaults to None.

        Returns:
            Tuple[Dict[str, Union[Dict[str, Union[int, None]], Any]], int, int]: A tuple containing a dictionary of metadata,
                short limit remaining, and long limit remaining.
                The metadata dictionary keys are domain names and the values are either another dictionary containing
                the common booru name and post_id or the result object (only with pixiv).
                The dictionary values for 'site' are source-specific strings
                and for 'post_id' are integers representing the post id from the source site.

        Raises:
            Exception: Any exceptions raised while extracting metadata or due to request limits being reached are propagated
            upwards.
        """

        response = await self.get_result(content_url, image)

        matches = {
            'donmai': None,
            'gelbooru': None,
            'yande': None,
            'konachan': None,
            'sankakucomplex': None,
            'pixiv': None,
            'pixiv_fanbox': None,
            'fanbox': None,
            'patreon': None,
            'fantia': None,
        }

        if response and response != 'Limit reached':
            site_keys = {
                'pixiv': 'pixiv',
                'donmai': 'danbooru',
                'gelbooru': 'gelbooru',
                'yande': 'yandere',
                'konachan': 'konachan',
                'sankakucomplex': 'sankaku',
                'patreon': 'patreon',
                'fanbox': 'fanbox',
                'fantia': 'fantia',
            }

            for result in response:
                if result.urls:
                    urls:list = result.urls
                    if result.source_url:
                        urls.append(result.source_url)
                    for url in urls:
                        site = self.get_base_domain(url)
                        post_id = re.findall(r'\b\d+\b', url)
                        if site in matches and not matches[site]:
                            logger.debug(f'Found result on {site.capitalize()}')
                            if site == 'pixiv':
                                # Handle pixiv.net fanbox links like: https://www.pixiv.net/fanbox/creator/3259336/post/872684
                                # This way traditional pixiv links can be added alongside the fanbox variations, in case the images are very similar.
                                if re.findall(r'/fanbox/creator/', url):
                                    if post_id and len(post_id) == 2 and not matches['fanbox']:
                                        matches['pixiv_fanbox'] = {'site': site_keys[site], 'creatorId': int(post_id[0]), 'id': int(post_id[1])}
                                elif self.get_base_domain(result.url) != site:
                                    if post_id:
                                        # Pixiv source that comes from a booru.
                                        obj = type('Dummy', (object,), {})
                                        # pixiv.py get_result expects this formatted link:
                                        obj.url = f"https://www.pixiv.net/member_illust.php?mode=medium&illust_id={post_id[0]}"
                                        obj.author_name = None
                                        matches[site] = obj
                                else:
                                    matches[site] = result
                            elif site == 'fanbox':
                                # https://www.fanbox.cc/@user
                                user1 = re.findall(r':\/\/.*fanbox\..*\/@([^\/]+)\/', url)
                                # https://user.fanbox.cc
                                # warning: user1 regex must be prioritized, otherwise "www" might match as the user for the user2.
                                user2 = re.findall(r':\/\/(?:www\.)?(.*)\.fanbox.*', url)
                                if user1 or user2:
                                    matches[site] = {'site': site_keys[site], 'user': user1[0] if user1 else user2[0], 'id': int(post_id[0])} if post_id else None
                                    # drop pixiv_fanbox if it exists because fanbox.cc is a better url link for it than pixiv.net/fanbox/
                                    matches['pixiv_fanbox'] = None
                            elif site == 'patreon' or site == 'fantia':
                                matches[site] = {'site': site_keys[site], 'id': int(post_id[0])} if post_id else None
                            elif site in site_keys:
                                matches[site] = {'site': site_keys[site], 'post_id': int(post_id[0])} if post_id else None
                            else:
                                continue
                elif result.source_url:
                    site = self.get_base_domain(url)



            logger.debug(f'Limit short: {response.short_remaining}')
            logger.debug(f'Limit long: {response.long_remaining}')

        # Even if response evaluates to False, it can still contain the limits
        try:
            short_remaining = response.short_remaining
            long_remaining = response.long_remaining
        except AttributeError:
            short_remaining = 1
            long_remaining = 1

        if response == 'Limit reached':
            long_remaining = 0

        return matches, short_remaining, long_remaining

    async def get_result(self, content_url: str, image: bytes | None = None) -> Coroutine | None:
        """
        Attempts to get a result from SauceNAO.

        This method attempts to get a result from SauceNAO by either uploading an image or using a URL. It tries to get a
        result up to a specified number of retry attempts. If an error occurs during the request, it logs the error and
        tries again. If the daily search limit is exceeded, it returns 'Limit reached'. If it cannot establish a connection
        to SauceNAO after all attempts, it logs that it is trying with the next post and returns None.

        Args:
            content_url (str): The URL of the content to retrieve.
            image (Optional[bytes], optional): The image data in bytes format, if available. Defaults to None.

        Returns:
            Optional[Coroutine]: The result from SauceNAO if it exists, 'Limit reached' if the daily search limit is
            exceeded, None otherwise.

        Raises:
            ContentTypeError: If the content type of the response is not as expected.
            TimeoutError: If a timeout occurs while trying to establish a connection to SauceNAO.
        """

        for attempt in range(self.retry_attempts):
            try:
                if image:
                    logger.debug('Trying to get result from uploaded file...')
                    response = await self.pysaucenao.from_file(BytesIO(image))
                else:
                    logger.debug(f'Trying to get result from content_url: {content_url}')
                    response = await self.pysaucenao.from_url(content_url)

                logger.debug(f'Received response {response}')
                return response

            except (ContentTypeError, TimeoutError):
                logger.debug('Could not establish connection to SauceNAO, trying again in 5s...')
                if attempt < self.retry_attempts - 1:  # no need to sleep on the last attempt
                    await sleep(self.retry_delay)

            except Exception as e:
                if 'Daily Search Limit Exceeded' in str(e):
                    return 'Limit reached'
                else:
                    if image:
                        logger.warning(f'Could not get result from SauceNAO with uploaded image "{content_url}": {e}')
                    else:
                        logger.warning(f'Could not get result from SauceNAO with image URL "{content_url}": {e}')
                    return None

        logger.debug('Could not establish connection to SauceNAO, trying with next post...')
        return None
