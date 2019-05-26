"""prawcore.sessions: Provides prawcore.Session and prawcore.session."""
from copy import deepcopy
import logging
import random
import time

from requests.compat import urljoin
from requests.exceptions import (
    ChunkedEncodingError,
    ConnectionError,
    ReadTimeout,
)
from requests.status_codes import codes

from .auth import BaseAuthorizer
from .rate_limit import RateLimiter
from .exceptions import (
    BadJSON,
    BadRequest,
    Conflict,
    InvalidInvocation,
    NotFound,
    Redirect,
    RequestException,
    ServerError,
    SpecialError,
    TooLarge,
    UnavailableForLegalReasons,
)
from .util import authorization_error_class

log = logging.getLogger(__package__)


class RetryStrategy(object):
    """An abstract class for scheduling request retries.

    Instances of this class are immutable state machines that control both the
    number and frequency of retry attempts.

    """

    def sleep(self):
        """Sleep until we are ready to attempt the request.

        :returns: A tuple of the new state of the machine and a bool indicating
            whether to retry on failure.

        """
        sleep_seconds = self._sleep_seconds()
        if sleep_seconds is not None:
            message = "Sleeping: {:0.2f} seconds prior to retry".format(
                sleep_seconds
            )
            log.debug(message)
            time.sleep(sleep_seconds)
        new_state = self._consume_retry()
        return new_state, new_state._should_retry_on_failure()


class FiniteRetryStrategy(RetryStrategy):
    """A ``RetryStrategy`` that retries requests a finite number of times."""

    def _consume_retry(self):
        return type(self)(self._retries - 1)

    def _should_retry_on_failure(self):
        return self._retries > 0

    def _sleep_seconds(self):
        if self._retries < 3:
            base = 0 if self._retries == 2 else 2
            return base + 2 * random.random()
        return None

    def __init__(self, retries=3):
        """Initialize the strategy.

        :param retries: Number of times to attempt a request.

        """
        self._retries = retries



class Session(object):
    """The low-level connection interface to reddit's API."""

    RETRY_EXCEPTIONS = (ChunkedEncodingError, ConnectionError, ReadTimeout)
    RETRY_STATUSES = {
        520,
        522,
        codes["bad_gateway"],
        codes["gateway_timeout"],
        codes["internal_server_error"],
        codes["service_unavailable"],
    }
    STATUS_EXCEPTIONS = {
        codes["bad_gateway"]: ServerError,
        codes["bad_request"]: BadRequest,
        codes["conflict"]: Conflict,
        codes["found"]: Redirect,
        codes["forbidden"]: authorization_error_class,
        codes["gateway_timeout"]: ServerError,
        codes["internal_server_error"]: ServerError,
        codes["media_type"]: SpecialError,
        codes["not_found"]: NotFound,
        codes["request_entity_too_large"]: TooLarge,
        codes["service_unavailable"]: ServerError,
        codes["unauthorized"]: authorization_error_class,
        codes["unavailable_for_legal_reasons"]: UnavailableForLegalReasons,
        # CloudFlare status (not named in requests)
        520: ServerError,
        522: ServerError,
    }
    SUCCESS_STATUSES = {codes["created"], codes["ok"]}

    @staticmethod
    def _log_request(data, method, params, url):
        log.debug("Fetching: {} {}".format(method, url))
        log.debug("Data: {}".format(data))
        log.debug("Params: {}".format(params))

    def __init__(self, authorizer):
        """Preprare the connection to reddit's API.

        :param authorizer: An instance of :class:`Authorizer`.

        """
        if not isinstance(authorizer, BaseAuthorizer):
            raise InvalidInvocation(
                "invalid Authorizer: {}".format(authorizer)
            )
        self._authorizer = authorizer
        self._rate_limiter = RateLimiter()
        self._retry_strategy = FiniteRetryStrategy()

    def __enter__(self):
        """Allow this object to be used as a context manager."""
        return self

    def __exit__(self, *_args):
        """Allow this object to be used as a context manager."""
        self.close()

    def _do_retry(
        self,
        data,
        files,
        json,
        method,
        params,
        response,
        retry_strategy_state,
        saved_exception,
        url,
    ):
        if saved_exception:
            status = repr(saved_exception)
        else:
            status = response.status_code
        log.warning(
            "Retrying due to {} status: {} {}".format(status, method, url)
        )
        return self._request_with_retries(
            data=data,
            files=files,
            json=json,
            method=method,
            params=params,
            url=url,
            retry_strategy_state=retry_strategy_state,
        )

    def _make_request(
        self, data, files, json, method, params, should_retry_on_failure, url
    ):
        try:
            response = self._rate_limiter.call(
                self._requestor.request,
                self._set_header_callback,
                method,
                url,
                allow_redirects=False,
                data=data,
                files=files,
                json=json,
                params=params,
            )
            log.debug(
                "Response: {} ({} bytes)".format(
                    response.status_code,
                    response.headers.get("content-length"),
                )
            )
            return response, None
        except RequestException as exception:
            if not should_retry_on_failure or not isinstance(
                exception.original_exception, self.RETRY_EXCEPTIONS
            ):
                raise
            return None, exception.original_exception

    def _request_with_retries(
        self, data, files, json, method, params, url, retry_strategy_state=None
    ):
        if retry_strategy_state is None:
            retry_strategy_state = self._retry_strategy

        new_retry_strategy_state, should_retry_on_failure = retry_strategy_state.sleep()
        self._log_request(data, method, params, url)
        response, saved_exception = self._make_request(
            data, files, json, method, params, should_retry_on_failure, url
        )

        do_retry = False
        if (
            response is not None
            and response.status_code == codes["unauthorized"]
        ):
            self._authorizer._clear_access_token()
            if hasattr(self._authorizer, "refresh"):
                do_retry = True

        if should_retry_on_failure and (
            do_retry
            or response is None
            or response.status_code in self.RETRY_STATUSES
        ):
            return self._do_retry(
                data,
                files,
                json,
                method,
                params,
                response,
                new_retry_strategy_state,
                saved_exception,
                url,
            )
        elif response.status_code in self.STATUS_EXCEPTIONS:
            raise self.STATUS_EXCEPTIONS[response.status_code](response)
        elif response.status_code == codes["no_content"]:
            return
        assert (
            response.status_code in self.SUCCESS_STATUSES
        ), "Unexpected status code: {}".format(response.status_code)
        if response.headers.get("content-length") == "0":
            return ""
        try:
            return response.json()
        except ValueError:
            raise BadJSON(response)

    def _set_header_callback(self):
        if not self._authorizer.is_valid() and hasattr(
            self._authorizer, "refresh"
        ):
            self._authorizer.refresh()
        return {
            "Authorization": "bearer {}".format(self._authorizer.access_token)
        }

    @property
    def _requestor(self):
        return self._authorizer._authenticator._requestor

    def close(self):
        """Close the session and perform any clean up."""
        self._requestor.close()

    def request(
        self, method, path, data=None, files=None, json=None, params=None
    ):
        """Return the json content from the resource at ``path``.

        :param method: The request verb. E.g., get, post, put.
        :param path: The path of the request. This path will be combined with
            the ``oauth_url`` of the Requestor.
        :param data: Dictionary, bytes, or file-like object to send in the body
            of the request.
        :param files: Dictionary, mapping ``filename`` to file-like object.
        :param json: Object to be serialized to JSON in the body of the
            request.
        :param params: The query parameters to send with the request.

        Automatically refreshes the access token if it becomes invalid and a
        refresh token is available. Raises InvalidInvocation in such a case if
        a refresh token is not available.

        """
        params = deepcopy(params) or {}
        params["raw_json"] = 1
        if isinstance(data, dict):
            data = deepcopy(data)
            data["api_type"] = "json"
            data = sorted(data.items())
        url = urljoin(self._requestor.oauth_url, path)
        return self._request_with_retries(
            data=data,
            files=files,
            json=json,
            method=method,
            params=params,
            url=url,
        )


def session(authorizer=None):
    """Return a :class:`Session` instance.

    :param authorizer: An instance of :class:`Authorizer`.

    """
    return Session(authorizer=authorizer)
