"""Avatar utils"""

from base64 import b64encode
from contextvars import ContextVar
from functools import cache as funccache
from hashlib import md5, sha256
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlparse

from django.core.cache import cache
from django.http import HttpRequest, HttpResponseNotFound
from django.templatetags.static import static
from lxml import etree  # nosec
from lxml.etree import Element, SubElement, _Element  # nosec
from requests.exceptions import ConnectionError, HTTPError, RequestException, Timeout

from authentik.lib.utils.dict import get_path_from_dict
from authentik.lib.utils.http import get_http_session
from authentik.tenants.utils import get_current_tenant

if TYPE_CHECKING:
    from authentik.core.models import User

GRAVATAR_URL = "https://www.gravatar.com"
DEFAULT_AVATAR = static("dist/assets/images/user_default.png")
AVATAR_STATUS_TTL_SECONDS = 60 * 60 * 8  # 8 Hours

SVG_XML_NS = "http://www.w3.org/2000/svg"
SVG_NS_MAP = {None: SVG_XML_NS}
# Match fonts used in web UI
SVG_FONTS = [
    "'RedHatText'",
    "'Overpass'",
    "overpass",
    "helvetica",
    "arial",
    "sans-serif",
]


def avatar_mode_none(user: User, mode: str) -> str | None:
    """No avatar"""
    return DEFAULT_AVATAR


def avatar_mode_attribute(user: User, mode: str) -> str | None:
    """Avatars based on a user attribute"""
    avatar = get_path_from_dict(user.attributes, mode[11:], default=None)
    return avatar


def avatar_mode_gravatar(user: User, mode: str) -> str | None:
    """Gravatar avatars"""

    mail_hash = sha256(user.email.lower().encode("utf-8")).hexdigest()  # nosec
    parameters = {"size": "158", "rating": "g", "default": "404"}
    gravatar_url = f"{GRAVATAR_URL}/avatar/{mail_hash}?{urlencode(parameters)}"

    return avatar_mode_url(user, gravatar_url)


def generate_colors(text: str) -> tuple[str, str]:
    """Generate colours based on `text`"""
    color = (
        int(md5(text.lower().encode("utf-8"), usedforsecurity=False).hexdigest(), 16) % 0xFFFFFF
    )  # nosec

    # Get a (somewhat arbitrarily) reduced scope of colors
    # to avoid too dark or light backgrounds
    blue = min(max((color) & 0xFF, 55), 200)
    green = min(max((color >> 8) & 0xFF, 55), 200)
    red = min(max((color >> 16) & 0xFF, 55), 200)
    bg_hex = f"{red:02x}{green:02x}{blue:02x}"
    # Contrasting text color (https://stackoverflow.com/a/3943023)
    text_hex = (
        "000" if (red * 0.299 + green * 0.587 + blue * 0.114) > 186 else "fff"  # noqa: PLR2004
    )
    return bg_hex, text_hex


@funccache
def generate_avatar_from_name(
    name: str,
    length: int = 2,
    size: int = 64,
    rounded: bool = False,
    font_size: float = 0.4375,
    bold: bool = False,
    uppercase: bool = True,
) -> str:
    """ "Generate an avatar with initials in SVG format.

    Inspired from: https://github.com/LasseRafn/ui-avatars
    """
    name_parts = name.split()
    # Only abbreviate first and last name
    if len(name_parts) > 2:  # noqa: PLR2004
        name_parts = [name_parts[0], name_parts[-1]]

    if len(name_parts) == 1:
        initials = name_parts[0][:length]
    else:
        initials = "".join([part[0] for part in name_parts[:-1]])
        initials += name_parts[-1]
        initials = initials[:length]

    bg_hex, text_hex = generate_colors(name)

    half_size = size // 2
    shape = "circle" if rounded else "rect"
    font_weight = "600" if bold else "400"

    root_element: _Element = Element(f"{{{SVG_XML_NS}}}svg", nsmap=SVG_NS_MAP)
    root_element.attrib["width"] = f"{size}px"
    root_element.attrib["height"] = f"{size}px"
    root_element.attrib["viewBox"] = f"0 0 {size} {size}"
    root_element.attrib["version"] = "1.1"

    shape = SubElement(root_element, f"{{{SVG_XML_NS}}}{shape}", nsmap=SVG_NS_MAP)
    shape.attrib["fill"] = f"#{bg_hex}"
    shape.attrib["cx"] = f"{half_size}"
    shape.attrib["cy"] = f"{half_size}"
    shape.attrib["width"] = f"{size}"
    shape.attrib["height"] = f"{size}"
    shape.attrib["r"] = f"{half_size}"

    text = SubElement(root_element, f"{{{SVG_XML_NS}}}text", nsmap=SVG_NS_MAP)
    text.attrib["x"] = "50%"
    text.attrib["y"] = "50%"
    text.attrib["style"] = (
        f"color: #{text_hex}; " "line-height: 1; " f"font-family: {','.join(SVG_FONTS)}; "
    )
    text.attrib["fill"] = f"#{text_hex}"
    text.attrib["alignment-baseline"] = "middle"
    text.attrib["dominant-baseline"] = "middle"
    text.attrib["text-anchor"] = "middle"
    text.attrib["font-size"] = f"{round(size * font_size)}"
    text.attrib["font-weight"] = f"{font_weight}"
    text.attrib["dy"] = ".1em"
    text.text = initials if not uppercase else initials.upper()

    return etree.tostring(root_element).decode()


def avatar_mode_generated(user: User, mode: str) -> str | None:
    """Wrapper that converts generated avatar to base64 svg"""
    # By default generate based off of user's display name
    name = user.name.strip()
    if name == "":
        # Fallback to username
        name = user.username.strip()
    # If we still don't have anything, fallback to `a k`
    if name == "":
        name = "a k"
    svg = generate_avatar_from_name(name)
    return f"data:image/svg+xml;base64,{b64encode(svg.encode('utf-8')).decode('utf-8')}"


_avatar_cache_prefetch: ContextVar[dict[str, object] | None] = ContextVar(
    "avatar_cache_prefetch", default=None
)
_AVATAR_SENTINEL = object()


def avatar_cache_key_for_user(user: User, mode: str) -> tuple[str, str]:
    """Compute the (hostname_available_key, image_url_key) cache keys for a user."""
    mail_hash = md5(user.email.lower().encode("utf-8"), usedforsecurity=False).hexdigest()  # nosec
    formatted_url = mode % {
        "username": user.username,
        "mail_hash": mail_hash,
        "upn": user.attributes.get("upn", ""),
    }
    hostname = urlparse(formatted_url).hostname
    return (
        f"goauthentik.io/lib/avatars/{hostname}/available",
        f"goauthentik.io/lib/avatars/{hostname}/{mail_hash}",
    )


def prefetch_avatar_cache(users, modes: str):
    """Batch-fetch all avatar cache keys for a list of users.

    Call this before serializing a list of users to avoid N+1 cache queries.
    Results are stored in request-local _avatar_cache_prefetch context."""
    keys = set()
    for user in users:
        for mode in modes.split(","):
            if mode == "gravatar" or "://" in mode:
                if mode == "gravatar":
                    mail_hash = sha256(user.email.lower().encode("utf-8")).hexdigest()
                    parameters = {"size": "158", "rating": "g", "default": "404"}
                    url_mode = f"{GRAVATAR_URL}/avatar/{mail_hash}?{urlencode(parameters)}"
                else:
                    url_mode = mode
                host_key, image_key = avatar_cache_key_for_user(user, url_mode)
                keys.add(host_key)
                keys.add(image_key)
    if keys:
        return _avatar_cache_prefetch.set(cache.get_many(list(keys)))
    return _avatar_cache_prefetch.set({})


def reset_avatar_cache_prefetch(token):
    """Reset request-local avatar prefetch cache state."""
    _avatar_cache_prefetch.reset(token)


def avatar_mode_url(user: User, mode: str) -> str | None:
    """Format url"""
    mail_hash = md5(user.email.lower().encode("utf-8"), usedforsecurity=False).hexdigest()  # nosec

    formatted_url = mode % {
        "username": user.username,
        "mail_hash": mail_hash,
        "upn": user.attributes.get("upn", ""),
    }

    hostname = urlparse(formatted_url).hostname
    cache_key_hostname_available = f"goauthentik.io/lib/avatars/{hostname}/available"

    # Use prefetched cache if available, otherwise individual lookup
    prefetch = _avatar_cache_prefetch.get()
    if prefetch is not None and cache_key_hostname_available in prefetch:
        if not prefetch[cache_key_hostname_available]:
            return None
    elif not cache.get(cache_key_hostname_available, True):
        return None

    cache_key_image_url = f"goauthentik.io/lib/avatars/{hostname}/{mail_hash}"

    if prefetch is not None and cache_key_image_url in prefetch:
        return prefetch[cache_key_image_url]
    elif prefetch is not None:
        # Key was in the prefetch batch but not found — cache miss
        pass
    else:
        cached = cache.get(cache_key_image_url, _AVATAR_SENTINEL)
        if cached is not _AVATAR_SENTINEL:
            return cached

    try:
        res = get_http_session().head(formatted_url, timeout=5, allow_redirects=True)

        if res.status_code == HttpResponseNotFound.status_code:
            cache.set(cache_key_image_url, None, timeout=AVATAR_STATUS_TTL_SECONDS)
            return None
        if not res.headers.get("Content-Type", "").startswith("image/"):
            cache.set(cache_key_image_url, None, timeout=AVATAR_STATUS_TTL_SECONDS)
            return None
        res.raise_for_status()
    except Timeout, ConnectionError, HTTPError:
        cache.set(cache_key_hostname_available, False, timeout=AVATAR_STATUS_TTL_SECONDS)
        return None
    except RequestException:
        return formatted_url

    cache.set(cache_key_image_url, formatted_url, timeout=AVATAR_STATUS_TTL_SECONDS)
    return formatted_url


def get_avatar(user: User, request: HttpRequest | None = None) -> str:
    """Get avatar with configured mode"""
    mode_map = {
        "none": avatar_mode_none,
        "initials": avatar_mode_generated,
        "gravatar": avatar_mode_gravatar,
    }
    tenant = None
    if request:
        tenant = request.tenant
    else:
        tenant = get_current_tenant()
    modes: str = tenant.avatars
    for mode in modes.split(","):
        avatar = None
        if mode in mode_map:
            avatar = mode_map[mode](user, mode)
        elif mode.startswith("attributes."):
            avatar = avatar_mode_attribute(user, mode)
        elif "://" in mode:
            avatar = avatar_mode_url(user, mode)
        if avatar:
            return avatar
    return avatar_mode_none(user, modes)
