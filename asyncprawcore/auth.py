"""Provides Authentication and Authorization classes."""
import inspect
import time
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    List,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
)

from aiohttp import ClientRequest, ClientResponse
from aiohttp.helpers import BasicAuth
from yarl import URL

from . import const
from .codes import codes
from .exceptions import InvalidInvocation, OAuthException, ResponseException

if TYPE_CHECKING:  # pragma: no cover
    from asyncprawcore.requestor import Requestor


class BaseAuthenticator(object):
    """Provide the base authenticator object that stores OAuth2 credentials."""

    def __init__(
        self, requestor: "Requestor", client_id: str, redirect_uri: Optional[str] = None
    ) -> None:
        """Represent a single authentication to Reddit's API.

        :param requestor: An instance of :class:`.Requestor`.
        :param client_id: The OAuth2 client ID to use with the session.
        :param redirect_uri: The redirect URI exactly as specified in your OAuth
            application settings on Reddit. This parameter is required if you want to
            use the :meth:`~.Authorizer.authorize_url` method, or the
            :meth:`~.Authorizer.authorize` method of the :class:`.Authorizer` class
            (default: ``None``).

        """
        self._requestor = requestor
        self.client_id = client_id
        self.redirect_uri = redirect_uri

    async def _post(
        self, url: str, success_status: int = codes["ok"], **data
    ) -> ClientResponse:
        response = await self._requestor.request(
            "POST",
            url,
            auth=self._auth(),
            data=sorted(data.items()),
            headers={"Connection": "close"},
        )
        if response.status != success_status:
            raise ResponseException(response)
        return response

    def authorize_url(
        self, duration: str, scopes: List[str], state: str, implicit: bool = False
    ) -> str:
        """Return the URL used out-of-band to grant access to your application.

        :param duration: Either ``"permanent"`` or ``"temporary"``. ``"temporary"``
            authorizations generate access tokens that last only 1 hour. ``"permanent"``
            authorizations additionally generate a refresh token that can be
            indefinitely used to generate new hour-long access tokens. Only
            ``"temporary"`` can be specified if ``implicit`` is set to ``True``.
        :param scopes: A list of OAuth scopes to request authorization for.
        :param state: A string that will be reflected in the callback to
            ``redirect_uri``. Elements must be printable ASCII characters in the range
            ``0x20`` through ``0x7E`` inclusive. This value should be temporarily unique
            to the client for whom the URL was generated.
        :param implicit: Use the implicit grant flow (default: ``False``). This flow is
            only available for ``UntrustedAuthenticators``.

        :returns: URL to be used out-of-band for granting access to your application.

        :raises: :class:`.InvalidInvocation` if ``redirect_uri`` is not provided, if
            ``implicit`` is ``True`` and an authenticator other than
            :class:`.UntrustedAuthenticator` is used, or ``implicit`` is ``True`` and
            ``duration`` is ``"permanent"``.

        """
        if self.redirect_uri is None:
            raise InvalidInvocation("redirect URI not provided")
        if implicit and not isinstance(self, UntrustedAuthenticator):
            raise InvalidInvocation(
                "Only UntrustedAuthenticator instances can "
                "use the implicit grant flow."
            )
        if implicit and duration != "temporary":
            raise InvalidInvocation(
                "The implicit grant flow only supports temporary access tokens."
            )

        params = {
            "client_id": self.client_id,
            "duration": duration,
            "redirect_uri": self.redirect_uri,
            "response_type": "token" if implicit else "code",
            "scope": " ".join(scopes),
            "state": state,
        }
        url = self._requestor.reddit_url + const.AUTHORIZATION_PATH
        request = ClientRequest("GET", URL(url), params=params)
        return str(request.url)

    async def revoke_token(self, token: str, token_type: Optional[str] = None) -> None:
        """Ask Reddit to revoke the provided token.

        :param token: The access or refresh token to revoke.
        :param token_type: When provided, hint to Reddit what the token type is for a
            possible efficiency gain. The value can be either ``"access_token"`` or
            ``"refresh_token"``.

        """
        data = {"token": token}
        if token_type is not None:
            data["token_type_hint"] = token_type
        url = self._requestor.reddit_url + const.REVOKE_TOKEN_PATH
        await self._post(url, **data)


class TrustedAuthenticator(BaseAuthenticator):
    """Store OAuth2 authentication credentials for web, or script type apps."""

    RESPONSE_TYPE: str = "code"

    def __init__(
        self,
        requestor: "Requestor",
        client_id: str,
        client_secret: str,
        redirect_uri: Optional[str] = None,
    ) -> None:
        """Represent a single authentication to Reddit's API.

        :param requestor: An instance of :class:`.Requestor`.
        :param client_id: The OAuth2 client ID to use with the session.
        :param client_secret: The OAuth2 client secret to use with the session.
        :param redirect_uri: The redirect URI exactly as specified in your OAuth
            application settings on Reddit. This parameter is required if you want to
            use the :meth:`~.Authorizer.authorize_url` method, or the
            :meth:`~.Authorizer.authorize` method of the :class:`.Authorizer` class
            (default: ``None``).

        """
        super(TrustedAuthenticator, self).__init__(requestor, client_id, redirect_uri)
        self.client_secret = client_secret

    def _auth(self) -> BasicAuth:
        return BasicAuth(self.client_id, self.client_secret)


class UntrustedAuthenticator(BaseAuthenticator):
    """Store OAuth2 authentication credentials for installed applications."""

    def _auth(self) -> BasicAuth:
        return BasicAuth(self.client_id, "")


class BaseAuthorizer(object):
    """Superclass for OAuth2 authorization tokens and scopes."""

    AUTHENTICATOR_CLASS: Union[Tuple, Type] = BaseAuthenticator

    def __init__(self, authenticator: BaseAuthenticator) -> None:
        """Represent a single authorization to Reddit's API.

        :param authenticator: An instance of :class:`.BaseAuthenticator`.

        """
        self._authenticator = authenticator
        self._clear_access_token()
        self._validate_authenticator()

    def _clear_access_token(self) -> None:
        self._expiration_timestamp: float
        self.access_token: Optional[str] = None
        self.scopes: Optional[Set[str]] = None

    async def _request_token(self, **data) -> None:
        url = self._authenticator._requestor.reddit_url + const.ACCESS_TOKEN_PATH
        pre_request_time = time.time()
        response = await self._authenticator._post(url=url, **data)
        payload = await response.json()
        if "error" in payload:  # Why are these OKAY responses?
            raise OAuthException(
                response, payload["error"], payload.get("error_description")
            )

        self._expiration_timestamp = pre_request_time - 10 + payload["expires_in"]
        self.access_token = payload["access_token"]
        if "refresh_token" in payload:
            self.refresh_token = payload["refresh_token"]
        self.scopes = set(payload["scope"].split(" "))

    def _validate_authenticator(self) -> None:
        if not isinstance(self._authenticator, self.AUTHENTICATOR_CLASS):
            msg = "Must use an authenticator of type"
            if isinstance(self.AUTHENTICATOR_CLASS, type):
                msg += f" {self.AUTHENTICATOR_CLASS.__name__}."
            else:
                msg += (
                    f" {' or '.join([i.__name__ for i in self.AUTHENTICATOR_CLASS])}."
                )
            raise InvalidInvocation(msg)

    def is_valid(self) -> bool:
        """Return whether the :class`.Authorizer` is ready to authorize requests.

        A ``True`` return value does not guarantee that the ``access_token`` is actually
        valid on the server side.

        """
        return (
            self.access_token is not None and time.time() < self._expiration_timestamp
        )

    async def revoke(self) -> None:
        """Revoke the current Authorization."""
        if self.access_token is None:
            raise InvalidInvocation("no token available to revoke")

        await self._authenticator.revoke_token(self.access_token, "access_token")
        self._clear_access_token()


class Authorizer(BaseAuthorizer):
    """Manages OAuth2 authorization tokens and scopes."""

    def __init__(
        self,
        authenticator: BaseAuthenticator,
        *,
        post_refresh_callback: Optional[
            Callable[["Authorizer"], Awaitable[None]]
        ] = None,
        pre_refresh_callback: Optional[
            Callable[["Authorizer"], Awaitable[None]]
        ] = None,
        refresh_token: Optional[str] = None,
    ) -> None:
        """Represent a single authorization to Reddit's API.

        :param authenticator: An instance of a subclass of :class:`.BaseAuthenticator`.
        :param post_refresh_callback: When a single-argument synchronous or asynchronous
            function is passed, the function will be called prior to refreshing the
            access and refresh tokens. The argument to the callback is the
            :class:`.Authorizer` instance. This callback can be used to inspect and
            modify the attributes of the :class:`.Authorizer`.
        :param pre_refresh_callback: When a single-argument function synchronous or
            asynchronous is passed, the function will be called after refreshing the
            access and refresh tokens. The argument to the callback is the
            :class:`.Authorizer` instance. This callback can be used to inspect and
            modify the attributes of the :class:`.Authorizer`.
        :param refresh_token: Enables the ability to refresh the authorization.

        """
        super(Authorizer, self).__init__(authenticator)
        self._post_refresh_callback = post_refresh_callback
        self._pre_refresh_callback = pre_refresh_callback
        self.refresh_token = refresh_token

    async def authorize(self, code: str) -> None:
        """Obtain and set authorization tokens based on ``code``.

        :param code: The code obtained by an out-of-band authorization request to
            Reddit.

        """
        if self._authenticator.redirect_uri is None:
            raise InvalidInvocation("redirect URI not provided")
        await self._request_token(
            code=code,
            grant_type="authorization_code",
            redirect_uri=self._authenticator.redirect_uri,
        )

    async def refresh(self) -> None:
        """Obtain a new access token from the refresh_token."""
        if self._pre_refresh_callback:
            if inspect.iscoroutinefunction(self._pre_refresh_callback):
                await self._pre_refresh_callback(self)
            else:
                self._pre_refresh_callback(self)
        if self.refresh_token is None:
            raise InvalidInvocation("refresh token not provided")
        await self._request_token(
            grant_type="refresh_token", refresh_token=self.refresh_token
        )
        if self._post_refresh_callback:
            if inspect.iscoroutinefunction(self._post_refresh_callback):
                await self._post_refresh_callback(self)
            else:
                self._post_refresh_callback(self)

    async def revoke(self, only_access: bool = False) -> None:
        """Revoke the current Authorization.

        :param only_access: When explicitly set to ``True``, do not evict the refresh
            token if one is set.

        Revoking a refresh token will in-turn revoke all access tokens associated with
        that authorization.

        """
        if only_access or self.refresh_token is None:
            await super(Authorizer, self).revoke()
        else:
            await self._authenticator.revoke_token(self.refresh_token, "refresh_token")
            self._clear_access_token()
            self.refresh_token = None


class DeviceIDAuthorizer(BaseAuthorizer):
    """Manages app-only OAuth2 for 'installed' applications.

    While the ``"*"`` scope will be available, some endpoints simply will not work due
    to the lack of an associated Reddit account.

    """

    AUTHENTICATOR_CLASS = (TrustedAuthenticator, UntrustedAuthenticator)

    def __init__(
        self,
        authenticator: BaseAuthenticator,
        device_id: Optional[str] = None,
        scopes: Optional[List[str]] = None,
    ) -> None:
        """Represent an app-only OAuth2 authorization for 'installed' apps.

        :param authenticator: An instance of :class:`.UntrustedAuthenticator` or
            :class:`.TrustedAuthenticator`.
        :param device_id: A unique ID (20-30 character ASCII string) (default:
            ``None``). ``device_id`` is set to ``"DO_NOT_TRACK_THIS_DEVICE"`` when the
            default argument is used. For more information about this parameter, see:
            https://github.com/reddit/reddit/wiki/OAuth2#application-only-oauth
        :param scopes: A list of OAuth scopes to request authorization for (default:
            ``None``). The scope ``"*"`` is requested when the default argument is used.

        """
        if device_id is None:
            device_id = "DO_NOT_TRACK_THIS_DEVICE"
        super().__init__(authenticator)
        self._device_id = device_id
        self._scopes = scopes

    async def refresh(self) -> None:
        """Obtain a new access token."""
        additional_kwargs = {}
        if self._scopes:
            additional_kwargs["scope"] = " ".join(self._scopes)
        grant_type = "https://oauth.reddit.com/grants/installed_client"
        await self._request_token(
            grant_type=grant_type,
            device_id=self._device_id,
            **additional_kwargs,
        )


class ImplicitAuthorizer(BaseAuthorizer):
    """Manages implicit installed-app type authorizations."""

    AUTHENTICATOR_CLASS = UntrustedAuthenticator

    def __init__(
        self,
        authenticator: UntrustedAuthenticator,
        access_token: str,
        expires_in: int,
        scope: str,
    ) -> None:
        """Represent a single implicit authorization to Reddit's API.

        :param authenticator: An instance of :class:`.UntrustedAuthenticator`.
        :param access_token: The access_token obtained from Reddit via callback to the
            authenticator's ``redirect_uri``.
        :param expires_in: The number of seconds the ``access_token`` is valid for. The
            origin of this value was returned from Reddit via callback to the
            authenticator's redirect uri. Note, you may need to subtract an offset
            before passing in this number to account for a delay between when Reddit
            prepared the response, and when you make this function call.
        :param scope: A space-delimited string of Reddit OAuth2 scope names as returned
            from Reddit in the callback to the authenticator's redirect uri.

        """
        super(ImplicitAuthorizer, self).__init__(authenticator)
        self._expiration_timestamp = time.time() + expires_in
        self.access_token = access_token
        self.scopes = set(scope.split(" "))


class ReadOnlyAuthorizer(Authorizer):
    """Manages authorizations that are not associated with a Reddit account.

    While the ``"*"`` scope will be available, some endpoints simply will not work due
    to the lack of an associated Reddit account.

    """

    AUTHENTICATOR_CLASS = TrustedAuthenticator

    def __init__(
        self,
        authenticator: BaseAuthenticator,
        scopes: Optional[List[str]] = None,
    ) -> None:
        """Represent a ReadOnly authorization to Reddit's API.

        :param scopes: A list of OAuth scopes to request authorization for (default:
            ``None``). The scope ``"*"`` is requested when the default argument is used.

        """
        super().__init__(authenticator)
        self._scopes = scopes

    async def refresh(self) -> None:
        """Obtain a new ReadOnly access token."""
        additional_kwargs = {}
        if self._scopes:
            additional_kwargs["scope"] = " ".join(self._scopes)
        await self._request_token(grant_type="client_credentials", **additional_kwargs)


class ScriptAuthorizer(Authorizer):
    """Manages personal-use script type authorizations.

    Only users who are listed as developers for the application will be granted access
    tokens.

    """

    AUTHENTICATOR_CLASS = TrustedAuthenticator

    def __init__(
        self,
        authenticator: BaseAuthenticator,
        username: Optional[str],
        password: Optional[str],
        two_factor_callback: Optional[Callable[[], Union[Awaitable[str], str]]] = None,
        scopes: Optional[List[str]] = None,
    ) -> None:
        """Represent a single personal-use authorization to Reddit's API.

        :param authenticator: An instance of :class:`.TrustedAuthenticator`.
        :param username: The Reddit username of one of the application's developers.
        :param password: The password associated with ``username``.
        :param two_factor_callback: A synchronous or asynchronous function that returns
            OTPs (One-Time Passcodes), also known as 2FA auth codes. If this function is
            provided, asyncprawcore will call it when authenticating.
        :param scopes: A list of OAuth scopes to request authorization for (default:
            ``None``). The scope ``"*"`` is requested when the default argument is used.

        """
        super(ScriptAuthorizer, self).__init__(authenticator)
        self._password = password
        self._scopes = scopes
        self._two_factor_callback = two_factor_callback
        self._username = username

    async def refresh(self) -> None:
        """Obtain a new personal-use script type access token."""
        additional_kwargs = {}
        if self._scopes:
            additional_kwargs["scope"] = " ".join(self._scopes)
        if self._two_factor_callback:
            if inspect.iscoroutinefunction(self._two_factor_callback):
                otp = await self._two_factor_callback()
            else:
                otp = self._two_factor_callback()
            if otp:
                additional_kwargs["otp"] = otp
        await self._request_token(
            grant_type="password",
            username=self._username,
            password=self._password,
            **additional_kwargs,
        )
